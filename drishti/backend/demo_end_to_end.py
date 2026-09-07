"""
demo_end_to_end.py — the whole pipeline in one process, no server required.

Six scenarios, back to back:

  GROUND SENSOR BRANCH (rule engine)
    A. Corroborated CRITICAL on complete data     -> Tier 1, fires immediately
    B. Glacier-fed ward, partial sensor coverage  -> Tier 2, countdown + veto
    C. Single sparse signal                       -> Tier 3, held for review

  TWO-BRANCH FUSION (rule engine + trained XGBoost model)
    D. Satellite model fires, no ground sensors   -> Tier 2, never fully auto
    E. Both branches agree                        -> confidence combines
    F. Slope failure, satellite model silent      -> no penalty (the whole point)

D/E/F are skipped with a printed explanation if the model artifacts or xgboost
are missing, so this always runs.

Run directly: `python demo_end_to_end.py`
"""

import asyncio
from datetime import datetime, timedelta, timezone

from ingestion import WardBuffer, ingest_batch
from risk_engine import assess_ward_risk
from fusion import assess_ward_risk_fused
from meteo_store import MeteoFeatureStore
from model_service import model_service
from sample_meteo_vectors import CALM_VECTOR, WARNING_VECTOR
from alert_dispatch import AlertDispatcher, build_alert, classify_tier


def reading(sensor_id, ward_id, type_, value, unit, minutes_ago):
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "sensor_id": sensor_id,
        "ward_id": ward_id,
        "type": type_,
        "value": value,
        "unit": unit,
        "timestamp": ts.isoformat(),
    }


def rule(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def show_assessment(assessment):
    print(f"  risk level      : {assessment.risk_level.value.upper()}")
    print(f"  confidence      : {assessment.confidence:.2f}")
    print(f"  lead time       : {assessment.estimated_lead_time_minutes} min")
    print(f"  sensor coverage : {assessment.data_completeness:.0%}"
          + (f"  (missing: {', '.join(assessment.stale_sensor_types)})"
             if assessment.stale_sensor_types else ""))
    print("  reasons:")
    for r in assessment.reasons:
        print(f"    - {r}")
    print("  confidence breakdown:")
    for f in assessment.confidence_factors:
        print(f"    - {f}")


def show_alert(alert):
    print(f"  -> tier   : {alert.tier.value}")
    print(f"     why    : {alert.tier_reason}")
    print(f"     status : {alert.status.value}")
    if alert.veto_window_seconds:
        print(f"     window : {alert.veto_window_seconds}s "
              f"(auto-sends at {alert.veto_deadline:%H:%M:%S} UTC unless cancelled)")


async def scenario_a(dispatcher):
    """Heavy rain + rising water + slope movement, every sensor reporting."""
    rule("A. Corroborated CRITICAL, full sensor coverage  ->  Tier 1 (auto-fire)")
    ward = "WD_1023"
    buf = WardBuffer()
    batch = []
    for i, mins in enumerate([50, 40, 30, 20, 10, 0]):
        batch.append(reading("SNS_101", ward, "rainfall", 30 + i * 3, "mm", mins))
        batch.append(reading("SNS_102", ward, "soil_moisture", 88, "%", mins))
        batch.append(reading("SNS_103", ward, "water_level", 0.4 + i * 0.3, "m", mins))
        batch.append(reading("SNS_104", ward, "slope_tilt", 11.5, "deg", mins))

    result = ingest_batch(batch, buf)
    print(f"  ingested: {len(result.accepted)} accepted, {len(result.rejected)} rejected")

    assessment = assess_ward_risk(ward, buf)
    show_assessment(assessment)

    alert = build_alert(assessment)
    show_alert(alert)
    await dispatcher.submit(alert)
    await asyncio.sleep(0.3)
    print(f"     final  : {alert.status.value} "
          f"(channels: {', '.join(alert.channels)})")


async def scenario_b(dispatcher):
    """Glacier-fed ward: rain + melt signal, but no water-level or tilt sensors."""
    rule("B. Glacier ward, partial coverage  ->  Tier 2 (countdown, human can veto)")
    ward = "WD_1044"
    buf = WardBuffer()
    batch = []
    for i, mins in enumerate([50, 40, 30, 20, 10, 0]):
        batch.append(reading("SNS_209", ward, "rainfall", 10 + i * 15, "mm", mins))
        batch.append(reading("SNS_210", ward, "soil_moisture", 85, "%", mins))
        batch.append(reading("SNS_211", ward, "temperature", 5 + i * 1.2, "C", mins))

    result = ingest_batch(batch, buf)
    print(f"  ingested: {len(result.accepted)} accepted, {len(result.rejected)} rejected")

    assessment = assess_ward_risk(ward, buf)
    show_assessment(assessment)

    # B1: a human cancels it in time.
    print("\n  --- B1: operator vetoes during the countdown ---")
    alert = build_alert(assessment, veto_seconds=6)  # shortened for the demo
    show_alert(alert)
    await dispatcher.submit(alert)

    for _ in range(3):
        await asyncio.sleep(1)
        print(f"     countdown: {alert.seconds_remaining:.0f}s remaining — nothing sent yet")

    outcome = await dispatcher.veto(alert.alert_id, actor="ops.sdma.rk", reason="ground team reports no rise")
    print(f"     VETO -> ok={outcome.ok} :: {outcome.detail}")

    # B2: nobody touches it — the dead-man's-switch fires.
    print("\n  --- B2: nobody responds; the switch fires on its own ---")
    alert2 = build_alert(assessment, veto_seconds=3)
    await dispatcher.submit(alert2)
    print(f"     status: {alert2.status.value}, {alert2.seconds_remaining:.0f}s remaining")
    await asyncio.sleep(4)
    print(f"     status: {alert2.status.value} at {alert2.dispatched_at:%H:%M:%S} UTC — "
          f"sent without any human action")

    # A veto arriving after dispatch must fail honestly.
    late = await dispatcher.veto(alert2.alert_id, actor="ops.sdma.rk")
    print(f"     late VETO -> ok={late.ok} :: {late.detail}")


async def scenario_c(dispatcher):
    """One sensor, two readings, nothing corroborating it."""
    rule("C. Single sparse signal  ->  Tier 3 (flagged for review, nothing sent)")
    ward = "WD_2001"  # not in the registry — falls back to default ward metadata
    buf = WardBuffer()
    batch = [
        reading("SNS_301", ward, "rainfall", 32, "mm", 20),
        reading("SNS_301", ward, "rainfall", 33, "mm", 5),
    ]
    ingest_batch(batch, buf)

    assessment = assess_ward_risk(ward, buf)
    show_assessment(assessment)

    alert = build_alert(assessment)
    show_alert(alert)
    await dispatcher.submit(alert)
    await asyncio.sleep(0.2)
    print(f"     nothing dispatched. status stays '{alert.status.value}' until a human acts.")

    outcome = await dispatcher.approve(alert.alert_id, actor="ops.sdma.rk", reason="confirmed by field call")
    await asyncio.sleep(0.3)
    print(f"     APPROVE -> ok={outcome.ok}; status now '{alert.status.value}'")


def _meteo(ward, vector, minutes_ago=5):
    store = MeteoFeatureStore()
    store.put(
        ward,
        vector,
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        source="demo fixture (see sample_meteo_vectors.py)",
    )
    return store


def _show_model_branch(assessment):
    mb = assessment.model_branch
    if not mb.get("available"):
        print(f"  model branch    : unavailable — {mb.get('reason')}")
        return
    if mb.get("false_alarm_rate") is None:
        print(f"  model branch    : {mb['tier']} — raw score {mb['raw_score']}, "
              f"below both thresholds")
        return
    print(f"  model branch    : {mb['tier']} (raw score {mb['raw_score']}, "
          f"threshold {mb['threshold']}, FAR {mb['false_alarm_rate']:.1%})")


async def scenario_d(dispatcher):
    """A ward with no working sensors — exactly the Nepal failure mode, where
    the gauges were destroyed mid-event. The satellite branch is all there is."""
    rule("D. Satellite model fires, zero ground sensors  ->  Tier 2 (veto window)")
    ward = "WD_2087"
    empty = WardBuffer()

    assessment = assess_ward_risk_fused(
        ward, empty, meteo_store=_meteo(ward, WARNING_VECTOR),
        model_service=model_service,
    )
    show_assessment(assessment)
    _show_model_branch(assessment)

    alert = build_alert(assessment)
    show_alert(alert)
    print("     note   : a satellite-only call never reaches Tier 1 — nothing on the")
    print("              ground confirmed it, so a human always gets a window to kill it.")
    await dispatcher.submit(alert)
    await asyncio.sleep(0.2)


async def scenario_e(dispatcher):
    """Rain gauges and the satellite model see the same storm."""
    rule("E. Both branches agree  ->  independent corroboration raises confidence")
    ward = "WD_1023"
    buf = WardBuffer()
    batch = []
    for i, mins in enumerate([50, 40, 30, 20, 10, 0]):
        batch.append(reading("SNS_101", ward, "rainfall", 22 + i * 2, "mm", mins))
        batch.append(reading("SNS_102", ward, "soil_moisture", 86, "%", mins))
    ingest_batch(batch, buf)

    ground_only = assess_ward_risk(ward, buf)
    fused = assess_ward_risk_fused(
        ward, buf, meteo_store=_meteo(ward, WARNING_VECTOR), model_service=model_service,
    )
    print(f"  ground sensors alone : {ground_only.risk_level.value.upper()} "
          f"@ confidence {ground_only.confidence:.2f} "
          f"-> {classify_tier(ground_only.risk_level, ground_only.confidence)[0].value}")
    print(f"  fused with the model : {fused.risk_level.value.upper()} "
          f"@ confidence {fused.confidence:.2f} "
          f"-> {classify_tier(fused.risk_level, fused.confidence)[0].value}")
    _show_model_branch(fused)
    print("  confidence breakdown:")
    for f in fused.confidence_factors:
        print(f"    - {f}")

    alert = build_alert(fused)
    show_alert(alert)
    await dispatcher.submit(alert)
    await asyncio.sleep(0.2)


async def scenario_f(dispatcher):
    """The differentiator. Slope tilt says a hillside is moving; the rainfall
    model sees a dry day and says nothing. It has no tilt input — its silence
    is not evidence. This is the Chamoli / Sikkim / Trishuli class of event."""
    rule("F. Slope failure with the rainfall model silent  ->  no confidence penalty")
    ward = "WD_1044"
    buf = WardBuffer()
    ingest_batch(
        [reading("SNS_211", ward, "slope_tilt", 13.0, "deg", m) for m in (20, 12, 4, 0)]
        + [reading("SNS_212", ward, "rainfall", 0.2, "mm", m) for m in (20, 10, 0)],
        buf,
    )

    fused = assess_ward_risk_fused(
        ward, buf, meteo_store=_meteo(ward, CALM_VECTOR), model_service=model_service,
    )
    show_assessment(fused)
    _show_model_branch(fused)

    alert = build_alert(fused)
    show_alert(alert)
    print("     note   : a naive fusion would let the quiet rainfall model talk this")
    print("              down. It cannot see slope tilt, so it gets no vote here.")
    await dispatcher.submit(alert)
    await asyncio.sleep(0.2)


async def main():
    dispatcher = AlertDispatcher(workers=2)
    await dispatcher.start()
    try:
        await scenario_a(dispatcher)
        await scenario_b(dispatcher)
        await scenario_c(dispatcher)

        model_service.load()
        if model_service.available:
            await scenario_d(dispatcher)
            await scenario_e(dispatcher)
            await scenario_f(dispatcher)
        else:
            rule("D/E/F. Two-branch fusion — SKIPPED")
            print(f"  {model_service.reason_unavailable}")
            print("  Install xgboost and check ml/artifacts/, then re-run.")

        rule("Alert log")
        for a in dispatcher.history():
            actor = f" by {a.acted_by}" if a.acted_by else ""
            print(f"  {a.generated_at:%H:%M:%S}  {a.ward_id:9} {a.risk_level.value:8} "
                  f"conf={a.confidence:.2f}  {a.tier.value:14} -> {a.status.value}{actor}")
    finally:
        await dispatcher.stop()


if __name__ == "__main__":
    asyncio.run(main())
