"""
risk_engine.py — GROUND-SENSOR risk assessment (Branch B).

Rule-based, by design and for now. It scores the five ground sensor types —
rainfall, soil moisture, water level, slope tilt, temperature — including the
water-level, slope-tilt and glacier-melt signals that no rainfall model can
observe. That makes it the code path behind DRISHTI's actual differentiator,
not merely scaffolding.

RELATIONSHIP TO THE TRAINED MODEL
The XGBoost model is NOT a drop-in replacement for this function. It runs on
34 satellite/reanalysis features (GPM/ERA5/SRTM/HydroSHEDS) and covers
rainfall-triggered floods only — a different measurement space and a different
hazard class. It lives in `model_service.py`, and `fusion.py` combines the two
verdicts into one `RiskAssessment` without merging their inputs.

So this file is no longer a placeholder waiting to be deleted. What IS still
placeholder here: the threshold constants below, and the confidence weights,
which matter more because they decide whether a human gets a veto window.
Recalibrate both against real ground-sensor data when it exists.

Anything replacing this function must keep the signature
(ward_id, WardBuffer, now=None) -> RiskAssessment and must populate
`confidence`. The tiering in `alert_dispatch.py` is driven entirely by that
number; a bare risk level with no confidence dumps every alert into one tier.

A NORMAL ward with zero fresh readings from any expected sensor gets a null
`estimated_lead_time_minutes` (`lead_time_basis="no_active_signal"`) rather
than a terrain-baseline number — there is no risk to project a countdown
toward, so a minute figure there would be fabricated, not conservative.

Current rules (deliberately simple, tune the thresholds as real data
comes in):
  - Rainfall risk: rainfall accumulated in the last RAINFALL_WINDOW_MINUTES
  - Soil saturation risk: high soil moisture means less capacity to absorb
    further rain -> amplifies rainfall risk
  - Water level risk: rise from the trough within WATER_LEVEL_WINDOW_MINUTES
  - Glacier-melt risk (glacier-fed wards only): sustained temperature rise
    is treated as a contributing signal FFGS misses entirely
  - Slope tilt: any FRESH reading past threshold is an independent landslide signal

Every window is defined in MINUTES OF WALL-CLOCK TIME, not list positions.
Index-based windows (`readings[-6:]`) silently assume a uniform reporting
cadence; a sensor that reconnects after an outage and flushes a backlog of
old readings would otherwise be read as a live spike.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from schemas import NormalizedReading, RiskLevel, SensorType, RISK_LEVEL_ORDER
from ingestion import WardBuffer
from ward_config import get_ward_info

# --- Tunable thresholds (placeholder values — recalibrate against real data) ---
RAINFALL_WATCH_MM = 30.0
RAINFALL_WARNING_MM = 60.0
RAINFALL_CRITICAL_MM = 100.0

SOIL_SATURATION_HIGH = 80.0       # % — amplifies rainfall risk by one level

WATER_LEVEL_RISE_WARNING_M = 0.5  # rise within WATER_LEVEL_WINDOW_MINUTES
WATER_LEVEL_RISE_CRITICAL_M = 1.2

SLOPE_TILT_WARNING_DEG = 5.0
SLOPE_TILT_CRITICAL_DEG = 10.0

GLACIER_TEMP_RISE_WARNING_C = 3.0  # sustained rise, glacier-fed wards only

# --- Time windows (minutes). These are the thresholds' denominators: changing
# a window changes what its threshold means, so move them together. ---
RAINFALL_WINDOW_MINUTES = 60
WATER_LEVEL_WINDOW_MINUTES = 30
SOIL_FRESHNESS_MINUTES = 30
SLOPE_FRESHNESS_MINUTES = 30
GLACIER_TEMP_WINDOW_MINUTES = 180

# Readings timestamped slightly ahead of us (clock skew) are still usable.
# Ingestion already rejects anything more than 5 minutes in the future.
CLOCK_SKEW_TOLERANCE_MINUTES = 5

_LEVEL_ORDER = RISK_LEVEL_ORDER


def _bump(level: RiskLevel, steps: int = 1) -> RiskLevel:
    idx = min(_LEVEL_ORDER.index(level) + steps, len(_LEVEL_ORDER) - 1)
    return _LEVEL_ORDER[idx]


def _max_level(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=_LEVEL_ORDER.index)


def _aware(ts: datetime) -> datetime:
    """Treat naive timestamps as UTC so comparisons never blow up."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _window(
    readings: List[NormalizedReading], minutes: float, now: datetime
) -> List[NormalizedReading]:
    """Readings inside the last `minutes`, sorted oldest -> newest.

    Sorting matters as much as filtering: the buffer stores readings in
    ARRIVAL order, and a backlog flush arrives out of chronological order.
    """
    cutoff = now - timedelta(minutes=minutes)
    horizon = now + timedelta(minutes=CLOCK_SKEW_TOLERANCE_MINUTES)
    inside = [r for r in readings if cutoff <= _aware(r.timestamp) <= horizon]
    inside.sort(key=lambda r: _aware(r.timestamp))
    return inside


@dataclass
class SignalResult:
    """One contributing risk signal, with the numbers behind it.

    Carrying `value`/`threshold`/`samples` out of the rule functions is what
    lets the confidence score (and the dashboard) explain itself instead of
    just asserting a level.
    """

    name: str
    level: RiskLevel = RiskLevel.NORMAL
    value: Optional[float] = None
    threshold: Optional[float] = None
    samples: int = 0
    anomalous_samples: int = 0
    detail: str = ""
    available: bool = False   # was there any fresh data for this signal at all?


@dataclass
class RiskAssessment:
    ward_id: str
    risk_level: RiskLevel
    estimated_lead_time_minutes: Optional[int]
    reasons: List[str]
    confidence: float = 1.0
    confidence_factors: List[str] = field(default_factory=list)
    signals: List[SignalResult] = field(default_factory=list)
    data_completeness: float = 0.0
    stale_sensor_types: List[str] = field(default_factory=list)
    assessed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # --- Lead-time projection (see project_lead_time) ---
    # `estimated_lead_time_minutes` above is None when the ward has no active
    # risk signal at all (see `no_active_signal` basis below) — the alert path
    # already treats None as "no veto-window budget to size" (alert_dispatch's
    # veto_window_seconds), and NORMAL wards never reach that path anyway.
    lead_time_band: Optional[List[int]] = None   # [min, max] from trend uncertainty
    lead_time_driver: Optional[str] = None       # signal the projection is based on
    lead_time_basis: str = "terrain_baseline"    # terrain_baseline | trend_projection
                                                 # | insufficient_history | trend_flat
                                                 # | no_active_signal
    impact_imminent: bool = False                # driver already past its CRITICAL threshold

    # Filled in by fusion.py when the meteo-hydro ML branch has an opinion on
    # this ward. Left as an "unavailable" dict when the model is off, missing,
    # or has no fresh feature vector — never silently omitted, so the dashboard
    # can always say WHY the model contributed nothing.
    model_branch: Dict[str, Any] = field(
        default_factory=lambda: {"available": False, "reason": "fusion not applied"}
    )


def _rainfall_signal(readings: List[NormalizedReading], now: datetime) -> SignalResult:
    window = _window(readings, RAINFALL_WINDOW_MINUTES, now)
    sig = SignalResult(name="rainfall", samples=len(window), available=bool(window))
    if not window:
        return sig

    total = sum(r.value for r in window)
    sig.value = round(total, 2)
    sig.anomalous_samples = sum(1 for r in window if r.is_anomalous)

    if total >= RAINFALL_CRITICAL_MM:
        sig.level, sig.threshold = RiskLevel.CRITICAL, RAINFALL_CRITICAL_MM
    elif total >= RAINFALL_WARNING_MM:
        sig.level, sig.threshold = RiskLevel.WARNING, RAINFALL_WARNING_MM
    elif total >= RAINFALL_WATCH_MM:
        sig.level, sig.threshold = RiskLevel.WATCH, RAINFALL_WATCH_MM
    else:
        sig.threshold = RAINFALL_WATCH_MM

    sig.detail = (
        f"{sig.value}mm accumulated over the last {RAINFALL_WINDOW_MINUTES}min "
        f"({len(window)} readings)"
    )
    return sig


def _water_level_signal(readings: List[NormalizedReading], now: datetime) -> SignalResult:
    window = _window(readings, WATER_LEVEL_WINDOW_MINUTES, now)
    sig = SignalResult(name="water_level", samples=len(window), available=bool(window))
    if len(window) < 2:
        return sig

    # Rise measured from the LOWEST point in the window, not the oldest reading:
    # if the window opens mid-recession, "newest minus oldest" can be negative
    # while the river is in fact climbing fast right now.
    trough = min(r.value for r in window)
    rise = window[-1].value - trough
    span_min = (_aware(window[-1].timestamp) - _aware(window[0].timestamp)).total_seconds() / 60

    sig.value = round(rise, 3)
    sig.anomalous_samples = sum(1 for r in window if r.is_anomalous)

    if rise >= WATER_LEVEL_RISE_CRITICAL_M:
        sig.level, sig.threshold = RiskLevel.CRITICAL, WATER_LEVEL_RISE_CRITICAL_M
    elif rise >= WATER_LEVEL_RISE_WARNING_M:
        sig.level, sig.threshold = RiskLevel.WARNING, WATER_LEVEL_RISE_WARNING_M
    else:
        sig.threshold = WATER_LEVEL_RISE_WARNING_M

    sig.detail = f"{sig.value}m rise over {span_min:.0f}min ({len(window)} readings)"
    return sig


def _slope_signal(readings: List[NormalizedReading], now: datetime) -> SignalResult:
    window = _window(readings, SLOPE_FRESHNESS_MINUTES, now)
    sig = SignalResult(name="slope_tilt", samples=len(window), available=bool(window))
    if not window:
        return sig

    latest = abs(window[-1].value)
    sig.value = round(latest, 2)
    sig.anomalous_samples = 1 if window[-1].is_anomalous else 0

    if latest >= SLOPE_TILT_CRITICAL_DEG:
        sig.level, sig.threshold = RiskLevel.CRITICAL, SLOPE_TILT_CRITICAL_DEG
    elif latest >= SLOPE_TILT_WARNING_DEG:
        sig.level, sig.threshold = RiskLevel.WARNING, SLOPE_TILT_WARNING_DEG
    else:
        sig.threshold = SLOPE_TILT_WARNING_DEG

    sig.detail = f"latest tilt {sig.value}deg (fresh within {SLOPE_FRESHNESS_MINUTES}min)"
    return sig


def _glacier_melt_signal(
    readings: List[NormalizedReading], glacier_fed: bool, now: datetime
) -> SignalResult:
    sig = SignalResult(name="glacier_melt")
    if not glacier_fed:
        return sig

    window = _window(readings, GLACIER_TEMP_WINDOW_MINUTES, now)
    sig.samples = len(window)
    sig.available = bool(window)
    if len(window) < 2:
        return sig

    rise = window[-1].value - window[0].value
    sig.value = round(rise, 2)
    sig.threshold = GLACIER_TEMP_RISE_WARNING_C
    sig.anomalous_samples = sum(1 for r in window if r.is_anomalous)
    if rise >= GLACIER_TEMP_RISE_WARNING_C:
        sig.level = RiskLevel.WATCH  # contributing signal, not decisive alone

    sig.detail = f"{sig.value}C rise over the last {GLACIER_TEMP_WINDOW_MINUTES}min"
    return sig


# --- Confidence ---------------------------------------------------------
# Confidence answers "how much should a human trust this call?", which is a
# different question from "how bad is it?". It drives the alert tiering in
# alert_dispatch.py: corroborated signals on complete, fresh, non-anomalous
# data fire automatically; a lone signal on partial data waits for a human.

CONFIDENCE_BASE = 0.35
CONFIDENCE_PER_CORROBORATING_SIGNAL = 0.15
CONFIDENCE_MAX_CORROBORATION_BONUS = 0.30
CONFIDENCE_COMPLETENESS_WEIGHT = 0.20
CONFIDENCE_STRONG_MARGIN_BONUS = 0.15   # driver >= 1.5x its threshold
CONFIDENCE_MODEST_MARGIN_BONUS = 0.08   # driver >= 1.2x its threshold
CONFIDENCE_SPARSE_PENALTY = 0.15        # driving signal has < 3 samples
CONFIDENCE_ANOMALY_PENALTY = 0.20       # driving window contains flagged readings
CONFIDENCE_FLOOR = 0.05
CONFIDENCE_CEILING = 0.99
MIN_SAMPLES_FOR_FULL_CONFIDENCE = 3


def _score_confidence(
    signals: List[SignalResult], driver: Optional[SignalResult], completeness: float
) -> tuple[float, List[str]]:
    factors: List[str] = []
    score = CONFIDENCE_BASE
    factors.append(f"base {CONFIDENCE_BASE:+.2f}")

    elevated = [s for s in signals if s.level is not RiskLevel.NORMAL]
    corroborating = max(len(elevated) - 1, 0)
    if corroborating:
        bonus = min(
            corroborating * CONFIDENCE_PER_CORROBORATING_SIGNAL,
            CONFIDENCE_MAX_CORROBORATION_BONUS,
        )
        score += bonus
        factors.append(f"{len(elevated)} independent signals agree {bonus:+.2f}")
    elif elevated:
        factors.append("single uncorroborated signal +0.00")

    completeness_bonus = CONFIDENCE_COMPLETENESS_WEIGHT * completeness
    score += completeness_bonus
    factors.append(f"sensor coverage {completeness:.0%} {completeness_bonus:+.2f}")

    if driver is not None and driver.value is not None and driver.threshold:
        ratio = abs(driver.value) / abs(driver.threshold)
        if ratio >= 1.5:
            score += CONFIDENCE_STRONG_MARGIN_BONUS
            factors.append(f"{driver.name} at {ratio:.1f}x threshold "
                           f"{CONFIDENCE_STRONG_MARGIN_BONUS:+.2f}")
        elif ratio >= 1.2:
            score += CONFIDENCE_MODEST_MARGIN_BONUS
            factors.append(f"{driver.name} at {ratio:.1f}x threshold "
                           f"{CONFIDENCE_MODEST_MARGIN_BONUS:+.2f}")

    if driver is not None:
        if driver.samples < MIN_SAMPLES_FOR_FULL_CONFIDENCE:
            score -= CONFIDENCE_SPARSE_PENALTY
            factors.append(f"only {driver.samples} reading(s) behind {driver.name} "
                           f"{-CONFIDENCE_SPARSE_PENALTY:+.2f}")
        if driver.anomalous_samples:
            score -= CONFIDENCE_ANOMALY_PENALTY
            factors.append(f"{driver.anomalous_samples} spike-flagged reading(s) in "
                           f"{driver.name} window {-CONFIDENCE_ANOMALY_PENALTY:+.2f}")

    score = max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, score))
    return round(score, 3), factors


def estimate_lead_time(ward_id: str, level: RiskLevel) -> int:
    """Terrain-baseline FALLBACK for lead time, in minutes.

    Shrinks the ward's terrain-based baseline as risk escalates. This is not a
    propagation model — no routing, no travel-time. `project_lead_time()` below
    is the real estimate; this is only used when there is no usable trend
    (fresh ward, flat/declining driver) or when the ML branch escalates a level
    the ground sensors have no trend for.
    """
    base = get_ward_info(ward_id)["baseline_lead_time_minutes"]
    return {
        RiskLevel.NORMAL: base,
        RiskLevel.WATCH: int(base * 0.75),
        RiskLevel.WARNING: int(base * 0.5),
        RiskLevel.CRITICAL: max(int(base * 0.25), 5),
    }[level]


# --- Real lead-time projection -------------------------------------------
# Project the dominant driver's recent trend forward to the moment it crosses
# its CRITICAL threshold, and report a band from the slope's uncertainty.

# What "impact" means for each driver, and in what quantity the projection runs.
#   rainfall     -> 60-min accumulation (mm), same figure _rainfall_signal reports
#   water_level  -> rise above the window trough (m), same as _water_level_signal
#   slope_tilt   -> latest absolute tilt (deg), same as _slope_signal
_DRIVER_CRITICAL_TARGET: Dict[str, float] = {
    "rainfall": RAINFALL_CRITICAL_MM,
    "water_level": WATER_LEVEL_RISE_CRITICAL_M,
    "slope_tilt": SLOPE_TILT_CRITICAL_DEG,
}
_DRIVER_WINDOW_MINUTES: Dict[str, int] = {
    "rainfall": RAINFALL_WINDOW_MINUTES,
    "water_level": WATER_LEVEL_WINDOW_MINUTES,
    "slope_tilt": SLOPE_FRESHNESS_MINUTES,
}
_MIN_TREND_SAMPLES = 3
_MIN_TREND_SPAN_MINUTES = 8.0
_LEAD_TIME_CAP_MINUTES = 600


@dataclass
class LeadTimeEstimate:
    minutes: Optional[int]
    driver: Optional[str] = None
    band: Optional[List[int]] = None
    basis: str = "terrain_baseline"
    impact_imminent: bool = False


def _linfit(points: List[tuple]) -> tuple:
    """Least-squares slope (value units per minute) and its standard error.

    `points` is [(minutes_from_start, value), ...]. Pure-Python; no numpy.
    """
    n = len(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    if n <= 2:
        return slope, 0.0
    resid = [y - (my + slope * (x - mx)) for x, y in zip(xs, ys)]
    s2 = sum(r * r for r in resid) / (n - 2)
    se = (s2 / sxx) ** 0.5
    return slope, se


def _driver_trend_points(
    driver_name: str, readings: List[NormalizedReading], now: datetime
) -> tuple:
    """(current_value, [(minutes_from_start, value), ...]) for the driver,
    tracked in the SAME quantity its signal function reports. Returns
    (None, []) if there isn't enough fresh history to fit a trend."""
    win_minutes = _DRIVER_WINDOW_MINUTES.get(driver_name)
    if win_minutes is None:
        return None, []
    window = _window(readings, win_minutes, now)
    if len(window) < _MIN_TREND_SAMPLES:
        return None, []
    t0 = _aware(window[0].timestamp)
    span = (_aware(window[-1].timestamp) - t0).total_seconds() / 60.0
    if span < _MIN_TREND_SPAN_MINUTES:
        return None, []

    trough = min(r.value for r in window) if driver_name == "water_level" else 0.0
    pts: List[tuple] = []
    accum = 0.0
    for r in window:
        m = (_aware(r.timestamp) - t0).total_seconds() / 60.0
        if driver_name == "rainfall":
            accum += r.value
            pts.append((m, accum))
        elif driver_name == "water_level":
            pts.append((m, r.value - trough))
        else:  # slope_tilt
            pts.append((m, abs(r.value)))
    current = pts[-1][1]
    return current, pts


def project_lead_time(
    ward_id: str,
    driver_name: Optional[str],
    readings: List[NormalizedReading],
    level: RiskLevel,
    now: datetime,
) -> LeadTimeEstimate:
    """Minutes until the driver crosses its CRITICAL threshold at its current
    rate of change, with a band from the slope's standard error.

    Degrades honestly: no elevated driver, or a driver with no CRITICAL target
    (glacier melt), or too little history, or a flat/declining trend -> the
    terrain baseline, tagged with why.
    """
    baseline = estimate_lead_time(ward_id, level)
    target = _DRIVER_CRITICAL_TARGET.get(driver_name) if driver_name else None
    if driver_name is None or target is None:
        return LeadTimeEstimate(minutes=baseline, driver=driver_name, basis="terrain_baseline")

    current, pts = _driver_trend_points(driver_name, readings, now)
    if current is None:
        return LeadTimeEstimate(
            minutes=baseline, driver=driver_name, basis="insufficient_history"
        )
    if current >= target:
        return LeadTimeEstimate(
            minutes=0, driver=driver_name, basis="trend_projection", impact_imminent=True
        )

    slope, se = _linfit(pts)
    if slope <= 0:
        return LeadTimeEstimate(minutes=baseline, driver=driver_name, basis="trend_flat")

    remaining = target - current

    def _clamp(m: float) -> int:
        return int(max(0, min(_LEAD_TIME_CAP_MINUTES, round(m))))

    fast = slope + se
    slow = max(slope - se, slope * 0.25)  # keep the optimistic edge finite
    band = sorted([_clamp(remaining / fast), _clamp(remaining / slow)])
    return LeadTimeEstimate(
        minutes=_clamp(remaining / slope),
        driver=driver_name,
        band=band,
        basis="trend_projection",
    )


def assess_ward_risk(
    ward_id: str, buffer: WardBuffer, now: Optional[datetime] = None
) -> RiskAssessment:
    """Assess a ward's current flash-flood risk.

    `now` is injectable so tests (and replayed historical data) can evaluate
    windows against a fixed clock instead of wall time.
    """
    now = _aware(now) if now is not None else datetime.now(timezone.utc)
    ward_info = get_ward_info(ward_id)
    reasons: List[str] = []

    rainfall = buffer.recent(ward_id, SensorType.RAINFALL)
    soil = buffer.recent(ward_id, SensorType.SOIL_MOISTURE)
    water = buffer.recent(ward_id, SensorType.WATER_LEVEL)
    slope = buffer.recent(ward_id, SensorType.SLOPE_TILT)
    temp = buffer.recent(ward_id, SensorType.TEMPERATURE)

    rain_sig = _rainfall_signal(rainfall, now)
    water_sig = _water_level_signal(water, now)
    slope_sig = _slope_signal(slope, now)
    glacier_sig = _glacier_melt_signal(temp, ward_info["glacier_fed"], now)

    if rain_sig.level is not RiskLevel.NORMAL:
        reasons.append(f"rainfall accumulation risk: {rain_sig.level.value} — {rain_sig.detail}")

    # Saturated soil amplifies rainfall risk by one level — but only on a FRESH
    # soil reading. A moisture value from six hours ago says nothing about the
    # ground's capacity to absorb the rain falling right now.
    soil_window = _window(soil, SOIL_FRESHNESS_MINUTES, now)
    soil_sig = SignalResult(
        name="soil_moisture",
        samples=len(soil_window),
        available=bool(soil_window),
        value=round(soil_window[-1].value, 2) if soil_window else None,
        threshold=SOIL_SATURATION_HIGH,
    )
    if (
        soil_window
        and soil_window[-1].value >= SOIL_SATURATION_HIGH
        and rain_sig.level is not RiskLevel.NORMAL
    ):
        rain_sig.level = _bump(rain_sig.level)
        soil_sig.detail = f"soil at {soil_sig.value}% — amplifying rainfall risk one level"
        reasons.append(f"soil saturation amplifying rainfall risk ({soil_sig.value}%)")

    if water_sig.level is not RiskLevel.NORMAL:
        reasons.append(f"water level rise risk: {water_sig.level.value} — {water_sig.detail}")

    if slope_sig.level is not RiskLevel.NORMAL:
        reasons.append(f"slope instability risk: {slope_sig.level.value} — {slope_sig.detail}")

    if glacier_sig.level is not RiskLevel.NORMAL:
        reasons.append(
            f"sustained glacier-melt temperature signal (FFGS blind spot) — {glacier_sig.detail}"
        )

    # soil_moisture is an amplifier, not a standalone signal, so it is excluded
    # from corroboration counting (it can't "agree" on its own).
    scoring_signals = [rain_sig, water_sig, slope_sig, glacier_sig]
    overall = _max_level(*(s.level for s in scoring_signals))

    # Data completeness: which sensor types this ward SHOULD have fresh data
    # from. Temperature only counts for glacier-fed wards.
    expected = [rain_sig, soil_sig, water_sig, slope_sig]
    if ward_info["glacier_fed"]:
        expected.append(glacier_sig)
    fresh = [s for s in expected if s.available]
    completeness = len(fresh) / len(expected) if expected else 0.0
    stale = [s.name for s in expected if not s.available]
    if stale:
        reasons.append(f"no fresh data from: {', '.join(stale)}")

    elevated = [s for s in scoring_signals if s.level is not RiskLevel.NORMAL]
    driver = max(elevated, key=lambda s: _LEVEL_ORDER.index(s.level)) if elevated else None
    confidence, confidence_factors = _score_confidence(scoring_signals, driver, completeness)

    if not reasons:
        reasons.append("no elevated signals")

    # A ward with no active risk level and zero fresh readings from any expected
    # sensor has no trend to project and no elevated signal to size a terrain
    # baseline against — the ward isn't "at some risk with thin history", it is
    # simply not reporting. Any minute figure here would be fabricated, so
    # report no lead time at all rather than falling back to terrain_baseline.
    no_active_signal = overall is RiskLevel.NORMAL and completeness == 0.0
    if no_active_signal:
        lead = LeadTimeEstimate(minutes=None, driver=None, basis="no_active_signal")
    else:
        driver_readings = {
            "rainfall": rainfall,
            "water_level": water,
            "slope_tilt": slope,
        }.get(driver.name if driver else None, [])
        lead = project_lead_time(
            ward_id, driver.name if driver else None, driver_readings, overall, now
        )
        if lead.basis == "trend_projection" and not lead.impact_imminent:
            reasons.append(
                f"lead time ~{lead.minutes}min (band {lead.band[0]}–{lead.band[1]}min) "
                f"projected from the {lead.driver} trend"
            )
        elif lead.impact_imminent:
            reasons.append(f"{lead.driver} is already past its critical threshold — impact imminent")

    return RiskAssessment(
        ward_id=ward_id,
        risk_level=overall,
        estimated_lead_time_minutes=lead.minutes,
        reasons=reasons,
        confidence=confidence,
        confidence_factors=confidence_factors,
        signals=[rain_sig, soil_sig, water_sig, slope_sig, glacier_sig],
        data_completeness=round(completeness, 3),
        stale_sensor_types=stale,
        assessed_at=now,
        lead_time_band=lead.band,
        lead_time_driver=lead.driver,
        lead_time_basis=lead.basis,
        impact_imminent=lead.impact_imminent,
    )
