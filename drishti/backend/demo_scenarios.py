"""
demo_scenarios.py -- pre-built scenarios you can trigger from the dashboard
for judge-facing demos.

Add these to the FastAPI app by importing the router and including it:

    # in main.py, near the other includes:
    from demo_scenarios import router as demo_router
    app.include_router(demo_router)

Then hit these endpoints (or wire a button in the dashboard header):

    GET  /demo/scenarios              -> list available scenarios
    POST /demo/trigger/{scenario_id}  -> inject one live

Each scenario POSTs ~18 readings per sensor over a 90-minute pre-roll AND
(where relevant) a satellite feature vector, so BOTH branches of the risk
engine get real input and the dashboard charts render a full curve the moment
it fires -- no faking the display, no manual state edits, just real ingestion.

Scenarios shipped:
  rainfall_storm         -- rising rainfall + saturating soil + rising river.
                            Both branches elevate; Critical via soil
                            amplification, lead time trend-projected.
  geohazard_creep        -- Chamoli-style: near-zero rainfall, slope tilt
                            accelerating past the critical angle, sustained
                            melt-temperature rise on a glacier-fed ward. The
                            rainfall model correctly stays at NONE.
  multi_ward_escalation  -- Sonprayag -> Warning and Rudraprayag Town ->
                            Critical at the same time, so the map and the
                            alert list populate with two tiers at once.
  reset                  -- Clear everything (for a clean second demo run).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException

# These imports assume this file lives in drishti/backend/ alongside main.py.
# Adjust if you place it elsewhere.
from ingestion import WardBuffer, ingest_batch
from sample_meteo_vectors import WATCH_VECTOR
from schemas import SensorType
from ward_config import WARD_REGISTRY

log = logging.getLogger("drishti.demo")
router = APIRouter(prefix="/demo", tags=["demo"])


# ---------------------------------------------------------------------------
# Wiring: these module-level references get set from main.py at import time.
# ---------------------------------------------------------------------------
_buffer: Optional[WardBuffer] = None
_meteo_store = None            # main.py's meteo_store
_reassess_and_dispatch = None  # main.py's helper that re-assesses + fires alerts
_reset_alerts = None           # main.py's dispatcher.clear_demo_state (optional)


def wire(buffer, meteo_store, reassess_and_dispatch, reset_alerts=None):
    """Called once from main.py's startup to hand us the shared state."""
    global _buffer, _meteo_store, _reassess_and_dispatch, _reset_alerts
    _buffer = buffer
    _meteo_store = meteo_store
    _reassess_and_dispatch = reassess_and_dispatch
    _reset_alerts = reset_alerts


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

SCENARIOS: Dict[str, Dict] = {
    "rainfall_storm": {
        "label": "Rainfall storm (Rudraprayag Town)",
        "description": "Rising rainfall + saturating soil + rising river. Both "
                       "branches light up; fires as a Tier 2 veto with a "
                       "trend-projected lead time.",
        "wards": ["WD_1023"],
    },
    "geohazard_creep": {
        "label": "Geohazard precursor (Gaurikund, glacier-fed)",
        "description": "Near-zero rainfall, slope tilt accelerating past the "
                       "critical angle, sustained melt-temperature rise. The "
                       "rainfall model correctly stays at NONE — the ground "
                       "geohazard branch alone drives it Critical.",
        "wards": ["WD_1044"],
    },
    "multi_ward_escalation": {
        "label": "Two wards escalating (Warning + Critical)",
        "description": "Sonprayag ramps to Warning while Rudraprayag Town goes "
                       "Critical — the map, both risk tiers and the alert list "
                       "populate at once.",
        "wards": ["WD_2011", "WD_1023"],
    },
    "reset": {
        "label": "Reset (clear all injected data)",
        "description": "Wipes the ward buffer and meteo store so a fresh demo "
                       "starts from Normal.",
        "wards": [],
    },
}

# Readings PER SENSOR across the pre-roll window. Dense enough that the moment a
# scenario fires, GET /wards/{id}/history/{type} returns a full smooth curve and
# risk_engine has >= 3 fresh points in every driver window to fit a trend.
_STEPS = 18
_DURATION_MIN = 90


# ---------------------------------------------------------------------------
# Reading builders
# ---------------------------------------------------------------------------

def _mk_reading(ward: str, kind: SensorType, value: float, unit: str,
                mins_ago: float, idx: int) -> dict:
    return {
        "sensor_id": f"SNS_DEMO_{kind.value.upper()}_{ward}",
        "ward_id": ward,
        "type": kind.value,
        "value": value,
        "unit": unit,
        "timestamp": (
            datetime.now(timezone.utc) - timedelta(minutes=mins_ago)
        ).isoformat(),
    }


def _ramp(i: int, n: int, lo: float, hi: float, curve: float = 1.0) -> float:
    """lo -> hi across steps 0..n-1. curve > 1 accelerates toward the end."""
    if n <= 1:
        return hi
    return lo + (hi - lo) * (i / (n - 1)) ** curve


def _series(ward: str, kind: SensorType, unit: str, lo: float, hi: float,
            curve: float = 1.0, n: int = _STEPS,
            duration_min: int = _DURATION_MIN) -> List[dict]:
    """A single sensor's smooth pre-roll: `n` readings from `duration_min` ago
    up to now, values ramping lo -> hi. Timestamps stay float-precise so no two
    readings collide (which the dedup step would drop)."""
    step = duration_min / (n - 1)
    return [
        _mk_reading(ward, kind, round(_ramp(i, n, lo, hi, curve), 3), unit,
                    duration_min - i * step, i)
        for i in range(n)
    ]


def _rainfall_storm_readings(ward: str, n: int = _STEPS) -> List[dict]:
    """Rain accumulation climbs toward (but not past) the critical figure, soil
    crosses the saturation line, the river rises. -> Critical via soil
    amplification, with rainfall still short of its own critical so the lead
    time is a real projection, not 'imminent'."""
    return (
        _series(ward, SensorType.RAINFALL,      "mm",  1.5, 10.5, curve=1.5, n=n)
        + _series(ward, SensorType.SOIL_MOISTURE, "%",  70.0, 88.0, curve=1.2, n=n)
        + _series(ward, SensorType.WATER_LEVEL,  "m",   0.80, 1.85, curve=2.2, n=n)
    )


def _geohazard_creep_readings(ward: str, n: int = _STEPS) -> List[dict]:
    """Chamoli-style: essentially dry, slope tilt accelerating past the 10 deg
    critical cut, melt temperature rising ~5 C over the window."""
    return (
        _series(ward, SensorType.RAINFALL,     "mm",  0.1, 0.7,  curve=1.0, n=n)
        + _series(ward, SensorType.SLOPE_TILT,   "deg", 1.0, 12.0, curve=2.6, n=n)
        + _series(ward, SensorType.TEMPERATURE,  "C",   3.0, 8.5,  curve=1.0, n=n)
    )


def _warning_ward_readings(ward: str, n: int = _STEPS) -> List[dict]:
    """Moderate storm: rainfall accumulation and river rise both reach WARNING
    but soil stays below the saturation line, so it does not amplify to
    Critical."""
    return (
        _series(ward, SensorType.RAINFALL,      "mm",  1.0, 10.0, curve=1.3, n=n)
        + _series(ward, SensorType.SOIL_MOISTURE, "%",  62.0, 74.0, curve=1.0, n=n)
        + _series(ward, SensorType.WATER_LEVEL,  "m",   0.70, 1.80, curve=2.5, n=n)
    )


def _rainfall_storm_meteo_vector() -> Dict[str, float]:
    """Satellite/reanalysis vector for the rainfall-storm ward.

    Reuses sample_meteo_vectors.WATCH_VECTOR, which is reverse-engineered from
    the deployed booster's own split thresholds and scores ~0.57 -> WATCH tier.
    That makes the ML branch a corroborating WATCH alongside the ground
    sensors, without being a full satellite WARNING. For a real historical
    event use the parquet replay path instead.
    """
    return dict(WATCH_VECTOR)


def _scenario_injections(scenario_id: str):
    """[(ward_id, [reading dicts], meteo_vector | None), ...] for a scenario.

    rainfall_storm stays ground-only (no vector) so it lands in Tier 2 with a
    visible veto countdown and a trend-projected lead time. The "both branches
    agree" case is carried by multi_ward_escalation's Critical ward.
    """
    if scenario_id == "rainfall_storm":
        return [("WD_1023", _rainfall_storm_readings("WD_1023"), None)]
    if scenario_id == "geohazard_creep":
        # no meteo vector on purpose: the rainfall model must stay at NONE
        return [("WD_1044", _geohazard_creep_readings("WD_1044"), None)]
    if scenario_id == "multi_ward_escalation":
        return [
            ("WD_2011", _warning_ward_readings("WD_2011"), None),
            ("WD_1023", _rainfall_storm_readings("WD_1023"), _rainfall_storm_meteo_vector()),
        ]
    return []


# ---------------------------------------------------------------------------
# Router endpoints
# ---------------------------------------------------------------------------

@router.get("/scenarios")
async def list_scenarios():
    """List available demo scenarios."""
    return [
        {"id": sid, "label": s["label"], "description": s["description"],
         "wards": s.get("wards", [])}
        for sid, s in SCENARIOS.items()
    ]


@router.post("/trigger/{scenario_id}")
async def trigger_scenario(scenario_id: str):
    """Inject a scenario's readings + (where relevant) satellite feature vectors
    into the running backend through the real ingestion pipeline, then re-assess
    and dispatch each affected ward."""
    if _buffer is None or _reassess_and_dispatch is None:
        raise HTTPException(500, "demo router not wired -- call wire() from main.py startup")

    if SCENARIOS.get(scenario_id) is None:
        raise HTTPException(404, f"unknown scenario '{scenario_id}'")

    if scenario_id == "reset":
        _buffer.clear()
        if _meteo_store is not None:
            _meteo_store.clear()
        if _reset_alerts is not None:
            await _reset_alerts()
        return {"ok": True, "scenario": scenario_id, "wards": []}

    injections = _scenario_injections(scenario_id)
    if not injections:
        raise HTTPException(400, f"scenario '{scenario_id}' has no injector defined")

    results = []
    for ward, readings, meteo_vector in injections:
        if ward not in WARD_REGISTRY:
            raise HTTPException(
                500, f"scenario references unknown ward '{ward}' -- check ward_config.py"
            )
        # Same path as POST /ingest: validation, unit conversion, dedup, spikes.
        ingest_batch(readings, _buffer)
        if meteo_vector is not None and _meteo_store is not None:
            _meteo_store.put(
                ward, meteo_vector, datetime.now(timezone.utc), source="demo_scenario"
            )
        assessment = await _reassess_and_dispatch(ward)
        log.info("scenario %s: ward=%s readings=%d meteo=%s risk=%s",
                 scenario_id, ward, len(readings), meteo_vector is not None,
                 getattr(assessment, "risk_level", "?"))
        results.append({
            "ward_id": ward,
            "readings_injected": len(readings),
            "meteo_vector_posted": meteo_vector is not None,
            "risk_level": getattr(assessment, "risk_level", None),
            "confidence": getattr(assessment, "confidence", None),
            "estimated_lead_time_minutes": getattr(
                assessment, "estimated_lead_time_minutes", None
            ),
            "impact_imminent": getattr(assessment, "impact_imminent", None),
        })

    return {"ok": True, "scenario": scenario_id, "wards": results}


# ---------------------------------------------------------------------------
# HOW TO WIRE (in main.py):
# ---------------------------------------------------------------------------
# from demo_scenarios import router as demo_router, wire as wire_demo
#
# # near the existing lifespan / startup:
# @app.on_event("startup")
# async def _startup():
#     wire_demo(buffer, meteo_store, reassess_and_dispatch)
#
# # register the router:
# app.include_router(demo_router)
#
# `reassess_and_dispatch` should be the existing async helper you already use
# inside POST /ingest and POST /wards/{id}/meteo to re-run risk assessment and
# submit any resulting alert. If that helper isn't factored out yet, extract
# the couple of lines from the ingest handler into an async function first.
# ---------------------------------------------------------------------------
