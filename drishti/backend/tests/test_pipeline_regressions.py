"""
test_pipeline_regressions.py — regression tests for the five bugs fixed in the
ingestion/dispatch hardening pass, plus the two-branch fusion added on top.

Dependency-free apart from the backend's own imports. Run from `backend/`:
    python tests/test_pipeline_regressions.py      (or: pytest tests/)

Each test is named for the issue it pins down, so a future refactor that
reintroduces one of them fails loudly rather than quietly.
"""

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Tests live in backend/tests/ but the backend modules are flat in backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import alert_dispatch
from alert_dispatch import (
    AlertDispatcher,
    build_alert,
    classify_tier,
    veto_window_seconds,
)
from ingestion import WardBuffer, ingest_batch, _is_spike
from risk_engine import assess_ward_risk
from schemas import Alert, AlertStatus, AlertTier, RiskLevel, SensorType


def reading(sensor_id, ward_id, type_, value, unit, minutes_ago):
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "sensor_id": sensor_id, "ward_id": ward_id, "type": type_,
        "value": value, "unit": unit, "timestamp": ts.isoformat(),
    }


# --- Issue 1: unbounded memory growth in the dedup set -------------------

def test_dedup_set_is_bounded():
    buf = WardBuffer(dedup_capacity=50)
    batch = [reading(f"SNS_{i}", "WD_1023", "rainfall", 1.0, "mm", i % 600)
             for i in range(500)]
    ingest_batch(batch, buf)

    stats = buf.stats()
    assert stats["dedup_keys"] <= 50, stats
    # The rolling deques are capped too, so total memory is O(wards x types).
    assert stats["readings"] <= stats["series"] * buf.window


def test_dedup_still_catches_recent_duplicates():
    buf = WardBuffer(dedup_capacity=50)
    dup = reading("SNS_1", "WD_1023", "rainfall", 4.0, "mm", 5)
    result = ingest_batch([dup, dict(dup)], buf)
    assert len(result.accepted) == 1
    assert result.rejected[0]["reason"] == "duplicate_reading"


# --- Issue 2: index-based windows vs. real time --------------------------

def test_backlog_flush_is_not_read_as_a_live_spike():
    """A sensor reconnects and dumps 3-hour-old readings. Those must not be
    summed into the current-hour rainfall total."""
    buf = WardBuffer()
    backlog = [reading("SNS_1", "WD_1023", "rainfall", 50.0, "mm", 180 + i) for i in range(6)]
    fresh = [reading("SNS_1", "WD_1023", "rainfall", 5.0, "mm", m) for m in (10, 0)]
    ingest_batch(backlog + fresh, buf)

    assessment = assess_ward_risk("WD_1023", buf)
    # 300mm of stale rain is excluded; only the 10mm that actually fell recently counts.
    rain = next(s for s in assessment.signals if s.name == "rainfall")
    assert rain.value == 10.0, rain
    assert assessment.risk_level is RiskLevel.NORMAL, assessment.reasons


def test_out_of_order_arrival_is_sorted_before_computing_rise():
    """Readings are stored in arrival order; the rise must be computed in
    timestamp order."""
    buf = WardBuffer()
    # Arrives newest-first — the old code would have computed a negative rise.
    ingest_batch([
        reading("SNS_2", "WD_1023", "water_level", 1.6, "m", 1),
        reading("SNS_2", "WD_1023", "water_level", 0.3, "m", 25),
        reading("SNS_2", "WD_1023", "water_level", 0.9, "m", 12),
    ], buf)

    assessment = assess_ward_risk("WD_1023", buf)
    water = next(s for s in assessment.signals if s.name == "water_level")
    assert abs(water.value - 1.3) < 1e-6, water
    assert water.level is RiskLevel.CRITICAL


def test_stale_only_data_does_not_produce_risk_but_is_reported():
    buf = WardBuffer()
    ingest_batch([reading("SNS_3", "WD_1023", "slope_tilt", 30.0, "deg", 120)], buf)
    assessment = assess_ward_risk("WD_1023", buf)
    assert assessment.risk_level is RiskLevel.NORMAL
    assert "slope_tilt" in assessment.stale_sensor_types


# --- Issue 4: spike detection at low baselines ---------------------------

def test_low_baseline_does_not_manufacture_spikes():
    buf = WardBuffer()
    ingest_batch([reading("SNS_4", "WD_1023", "rainfall", 0.1, "mm", 30 - i)
                  for i in range(5)], buf)
    # 0.6mm on a 0.1mm baseline is 6x — but it is still just drizzle.
    assert _is_spike(buf, "WD_1023", SensorType.RAINFALL, 0.6) is False
    # 40mm on the same baseline clears both the ratio and the absolute floor.
    assert _is_spike(buf, "WD_1023", SensorType.RAINFALL, 40.0) is True


def test_tilt_spikes_are_detected_on_negative_baselines():
    """Old code bailed out whenever the average was <= 0, which disabled spike
    detection entirely for downslope tilt."""
    buf = WardBuffer()
    ingest_batch([reading("SNS_5", "WD_1023", "slope_tilt", -2.0, "deg", 30 - i)
                  for i in range(5)], buf)
    assert _is_spike(buf, "WD_1023", SensorType.SLOPE_TILT, -9.0) is True
    assert _is_spike(buf, "WD_1023", SensorType.SLOPE_TILT, -2.6) is False


# --- Issue 5: confidence tiering -----------------------------------------

def test_tier_boundaries():
    assert classify_tier(RiskLevel.WARNING, 0.90)[0] is AlertTier.TIER_1_AUTO
    assert classify_tier(RiskLevel.WARNING, 0.70)[0] is AlertTier.TIER_2_VETO
    assert classify_tier(RiskLevel.WARNING, 0.30)[0] is AlertTier.TIER_3_REVIEW
    # CRITICAL is never left as dashboard-only.
    assert classify_tier(RiskLevel.CRITICAL, 0.10)[0] is AlertTier.TIER_2_VETO


def test_veto_window_never_eats_short_lead_times():
    assert veto_window_seconds(60) == 90   # 20% of an hour, capped at the 90s default
    assert veto_window_seconds(10) == 90   # 20% of 600s = 120s -> still capped at 90
    assert veto_window_seconds(5) == 60    # 20% of 300s — the fraction cap binds here
    assert veto_window_seconds(2) == 0     # too short to hold at all -> fire immediately


def test_short_lead_time_promotes_tier2_to_immediate():
    from risk_engine import RiskAssessment
    assessment = RiskAssessment(
        ward_id="WD_1044", risk_level=RiskLevel.CRITICAL,
        estimated_lead_time_minutes=2, reasons=[], confidence=0.7,
    )
    alert = build_alert(assessment)
    assert alert.tier is AlertTier.TIER_1_AUTO
    assert "no room for a veto window" in alert.tier_reason


def _alert(ward="WD_1023", level=RiskLevel.CRITICAL, confidence=0.7, veto_seconds=None):
    from risk_engine import RiskAssessment
    return build_alert(
        RiskAssessment(ward_id=ward, risk_level=level, estimated_lead_time_minutes=40,
                       reasons=[], confidence=confidence),
        veto_seconds=veto_seconds,
    )


async def _with_dispatcher(fn, **kwargs):
    d = AlertDispatcher(**kwargs)
    await d.start()
    try:
        return await fn(d)
    finally:
        await d.stop(drain_timeout=2.0)


def test_veto_during_window_prevents_dispatch():
    sent = []
    original = dict(alert_dispatch._SENDERS)

    async def record(alert):
        sent.append(alert.alert_id)

    alert_dispatch._SENDERS.update({k: record for k in original})
    try:
        async def body(d):
            alert = _alert(veto_seconds=5)
            await d.submit(alert)
            assert alert.status is AlertStatus.PENDING_VETO
            outcome = await d.veto(alert.alert_id, actor="tester")
            assert outcome.ok
            await asyncio.sleep(0.2)
            assert alert.status is AlertStatus.VETOED
            assert sent == [], "a vetoed alert must never reach a channel"
        asyncio.run(_with_dispatcher(body, flush_pending_on_stop=False))
    finally:
        alert_dispatch._SENDERS.update(original)


def test_inaction_dispatches_the_alert():
    async def body(d):
        alert = _alert(veto_seconds=1)
        await d.submit(alert)
        await asyncio.sleep(1.6)
        assert alert.status is AlertStatus.DISPATCHED, alert.status
        # A veto after the fact must fail honestly rather than pretend.
        late = await d.veto(alert.alert_id, actor="tester")
        assert late.ok is False
        assert "too late" in late.detail
    asyncio.run(_with_dispatcher(body))


def test_tier3_sends_nothing_without_approval():
    async def body(d):
        alert = _alert(level=RiskLevel.WARNING, confidence=0.2)
        assert alert.tier is AlertTier.TIER_3_REVIEW
        await d.submit(alert)
        await asyncio.sleep(0.5)
        assert alert.status is AlertStatus.AWAITING_REVIEW
        assert alert.dispatched_at is None
        await d.approve(alert.alert_id, actor="tester")
        await asyncio.sleep(0.3)
        assert alert.status is AlertStatus.DISPATCHED
    asyncio.run(_with_dispatcher(body))


def test_repeat_assessments_do_not_restart_countdowns():
    async def body(d):
        first = _alert(veto_seconds=30)
        assert await d.submit(first) is not None
        # Same ward, same severity, moments later — must not open a second countdown.
        assert await d.submit(_alert(veto_seconds=30)) is None
        assert len(d.active()) == 1
        # Escalation always gets through, and supersedes the pending one.
        escalated = _alert(level=RiskLevel.CRITICAL, confidence=0.9)
        first.risk_level = RiskLevel.WARNING  # pretend the pending one was lower
        assert await d.submit(escalated) is not None
    asyncio.run(_with_dispatcher(body, flush_pending_on_stop=False))


# --- Issue 3: dispatch must not block ingestion --------------------------

def test_submit_returns_before_slow_channels_finish():
    original = dict(alert_dispatch._SENDERS)

    async def slow(alert):
        await asyncio.sleep(1.0)

    alert_dispatch._SENDERS.update({k: slow for k in original})
    try:
        async def body(d):
            started = time.perf_counter()
            for i in range(20):
                await d.submit(_alert(ward=f"WD_{i}", confidence=0.95))
            submit_elapsed = time.perf_counter() - started
            # 20 alerts x 3 channels x 1s of network time, yet submission is instant.
            assert submit_elapsed < 0.2, submit_elapsed
            return submit_elapsed
        asyncio.run(_with_dispatcher(body, workers=4))
    finally:
        alert_dispatch._SENDERS.update(original)


def test_channels_fan_out_concurrently():
    original = dict(alert_dispatch._SENDERS)

    async def slow(alert):
        await asyncio.sleep(0.4)

    alert_dispatch._SENDERS.update({k: slow for k in original})
    try:
        async def body(d):
            alert = _alert(confidence=0.95)  # CRITICAL -> 3 channels
            started = time.perf_counter()
            await d.submit(alert)
            while alert.status is not AlertStatus.DISPATCHED:
                await asyncio.sleep(0.02)
            elapsed = time.perf_counter() - started
            # ~0.4s if concurrent, ~1.2s if the channels were serialised.
            assert elapsed < 0.8, elapsed
        asyncio.run(_with_dispatcher(body))
    finally:
        alert_dispatch._SENDERS.update(original)


def test_one_failing_channel_does_not_block_the_others():
    original = dict(alert_dispatch._SENDERS)

    async def boom(alert):
        raise RuntimeError("gateway down")

    alert_dispatch._SENDERS["sms"] = boom
    alert_dispatch.SEND_BACKOFF_SECONDS = 0.01
    try:
        async def body(d):
            alert = _alert(confidence=0.95)
            await d.submit(alert)
            for _ in range(200):
                if alert.status in (AlertStatus.PARTIALLY_DISPATCHED, AlertStatus.DISPATCHED):
                    break
                await asyncio.sleep(0.02)
            assert alert.status is AlertStatus.PARTIALLY_DISPATCHED, alert.status
            assert alert.failed_channels == ["sms"]
        asyncio.run(_with_dispatcher(body))
    finally:
        alert_dispatch._SENDERS.update(original)
        alert_dispatch.SEND_BACKOFF_SECONDS = 0.5


# --- Two-branch fusion (rule engine + meteo ML model) --------------------
# These use a stub model rather than the real booster, so the combination
# rules are pinned down deterministically and the tests run without xgboost.

from fusion import MODEL_DISAGREEMENT_PENALTY, MODEL_ONLY_PENALTY, assess_ward_risk_fused
from meteo_store import MeteoFeatureStore
from model_service import ModelAssessment


class _StubModel:
    """Stands in for MeteoModelService with a fixed verdict."""

    def __init__(self, tier="NONE", confidence=0.0, available=True):
        self.available = available
        self.reason_unavailable = "" if available else "stubbed off"
        self._verdict = ModelAssessment(
            available=True,
            tier=tier,
            risk_level={"WARNING": RiskLevel.WARNING, "WATCH": RiskLevel.WATCH}.get(
                tier, RiskLevel.NORMAL
            ),
            raw_score=0.5,
            confidence=confidence,
            threshold=0.93,
            false_alarm_rate=round(1 - confidence, 3),
            detail=f"stub {tier}",
        )

    def score_features(self, features, observed_at=None, now=None):
        return self._verdict


def _store_with_features(ward="WD_1023", minutes_ago=0):
    store = MeteoFeatureStore()
    store.put(
        ward,
        {"rain_24h": 80.0, "slope_deg": 12.0},
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        source="test",
    )
    return store


def _rainy_buffer(ward="WD_1023"):
    buf = WardBuffer()
    ingest_batch(
        [reading("SNS_R", ward, "rainfall", 24.0, "mm", m) for m in (40, 30, 20, 10)], buf
    )
    return buf


def test_fusion_without_features_is_exactly_the_rule_result():
    buf = _rainy_buffer()
    rule_only = assess_ward_risk("WD_1023", buf)
    fused = assess_ward_risk_fused(
        "WD_1023", buf, meteo_store=MeteoFeatureStore(), model_service=_StubModel("WARNING", 0.9)
    )
    assert fused.risk_level is rule_only.risk_level
    assert fused.confidence == rule_only.confidence
    assert fused.model_branch["available"] is False


def test_model_only_warning_raises_the_level_but_still_gets_a_veto_window():
    """A satellite-only call must never fire fully automatically — no ground
    sensor confirmed it for this ward."""
    empty = WardBuffer()
    fused = assess_ward_risk_fused(
        "WD_1023", empty,
        meteo_store=_store_with_features(),
        model_service=_StubModel("WARNING", confidence=0.856),
    )
    assert fused.risk_level is RiskLevel.WARNING
    assert abs(fused.confidence - (0.856 - MODEL_ONLY_PENALTY)) < 1e-6
    assert classify_tier(fused.risk_level, fused.confidence)[0] is AlertTier.TIER_2_VETO


def test_agreement_between_branches_raises_confidence_above_either_one():
    buf = _rainy_buffer()
    rule_only = assess_ward_risk("WD_1023", buf)
    fused = assess_ward_risk_fused(
        "WD_1023", buf,
        meteo_store=_store_with_features(),
        model_service=_StubModel("WARNING", confidence=0.856),
    )
    assert fused.confidence > rule_only.confidence
    assert fused.confidence > 0.856
    assert fused.confidence <= 0.95  # the independence cap


def test_quiet_model_penalises_a_rainfall_call():
    buf = _rainy_buffer()
    rule_only = assess_ward_risk("WD_1023", buf)
    assert rule_only.risk_level is not RiskLevel.NORMAL
    fused = assess_ward_risk_fused(
        "WD_1023", buf,
        meteo_store=_store_with_features(),
        model_service=_StubModel("NONE"),
    )
    assert abs(fused.confidence - (rule_only.confidence - MODEL_DISAGREEMENT_PENALTY)) < 1e-6


def test_quiet_model_does_not_penalise_a_geohazard_call():
    """The whole point of the geohazard branch: a rainfall model has no
    water-level or slope-tilt input, so its silence about a slope failure is
    not evidence against one."""
    buf = WardBuffer()
    ingest_batch(
        [reading("SNS_T", "WD_1023", "slope_tilt", 12.0, "deg", m) for m in (20, 10, 0)], buf
    )
    rule_only = assess_ward_risk("WD_1023", buf)
    assert rule_only.risk_level is RiskLevel.CRITICAL
    fused = assess_ward_risk_fused(
        "WD_1023", buf,
        meteo_store=_store_with_features(),
        model_service=_StubModel("NONE"),
    )
    assert fused.confidence == rule_only.confidence, fused.confidence_factors
    assert any("outside what a rainfall model can observe" in f
               for f in fused.confidence_factors)


def test_stale_feature_vector_is_ignored():
    empty = WardBuffer()
    fused = assess_ward_risk_fused(
        "WD_1023", empty,
        meteo_store=_store_with_features(minutes_ago=48 * 60),
        model_service=_StubModel("WARNING", 0.9),
    )
    # The store hands it over, but score_features is what enforces the age
    # limit — the stub skips that, so check the real service does it.
    from model_service import MeteoModelService
    import config as _cfg
    svc = MeteoModelService()
    svc.load()
    if not svc.available:
        return  # xgboost/artifacts absent in this environment
    verdict = svc.score_features(
        {f: 0.0 for f in svc.features},
        observed_at=datetime.now(timezone.utc)
        - timedelta(minutes=_cfg.METEO_MAX_AGE_MINUTES + 10),
    )
    assert verdict.available is False
    assert "old" in verdict.reason_unavailable


def test_real_model_scores_a_full_feature_vector():
    """Smoke test against the actual artifact, skipped if it isn't installed."""
    from model_service import MeteoModelService
    svc = MeteoModelService()
    svc.load()
    if not svc.available:
        return
    verdict = svc.score_features({f: 1.0 for f in svc.features})
    assert verdict.available is True
    assert verdict.tier in {"WARNING", "WATCH", "NONE"}
    assert 0.0 <= verdict.raw_score <= 1.0
    assert verdict.missing_features == []


def test_mostly_empty_feature_vector_is_refused_rather_than_scored():
    from model_service import MeteoModelService
    svc = MeteoModelService()
    svc.load()
    if not svc.available:
        return
    verdict = svc.score_features({svc.features[0]: 1.0})
    assert verdict.available is False
    assert "missing" in verdict.reason_unavailable


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)

    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
