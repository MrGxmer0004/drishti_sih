"""
demo_scenarios.py -- pre-built scenarios you can trigger from the dashboard
for judge-facing demos.

Add these to the FastAPI app by importing the router and including it:

    # in main.py, near the other includes:
    from demo_scenarios import router as demo_router
    app.include_router(demo_router)

Then hit these endpoints (or wire a button in the dashboard header):

    POST /demo/scenarios              -> list available scenarios
    POST /demo/trigger/{scenario_id}  -> inject one live

Each scenario POSTs sensor readings AND (where relevant) a satellite feature
vector, so BOTH branches of the risk engine get real input. The result is a
demo where the dashboard reacts the same way it would to real telemetry --
no faking the display, no manual state edits, just real ingestion.

Scenarios shipped:
  rainfall_storm   -- Himachal-2023-style: rising rainfall + saturated soil +
                       rising water level. Trips the rainfall model into
                       WARNING territory on the rainfall branch.
  geohazard_creep  -- Chamoli-style precursor: near-zero rainfall + slope
                       tilt escalating + sustained temperature rise on a
                       glacier-fed ward. Exercises the rule-based geohazard
                       path (rainfall model correctly stays quiet).
  reset            -- Clear everything (for a clean second demo run).
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
from schemas import SensorType
from ward_config import WARD_REGISTRY

log = logging.getLogger("drishti.demo")
router = APIRouter(prefix="/demo", tags=["demo"])


# ---------------------------------------------------------------------------
# Wiring: these two module-level references get set from main.py at startup.
# See "how to wire" at the bottom of this file.
# ---------------------------------------------------------------------------
_buffer: Optional[WardBuffer] = None
_meteo_store = None            # main.py's meteo_store
_reassess_and_dispatch = None  # main.py's helper that re-assesses + fires alerts


def wire(buffer, meteo_store, reassess_and_dispatch):
    """Called once from main.py's startup to hand us the shared state."""
    global _buffer, _meteo_store, _reassess_and_dispatch
    _buffer = buffer
    _meteo_store = meteo_store
    _reassess_and_dispatch = reassess_and_dispatch


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

SCENARIOS: Dict[str, Dict] = {
    "rainfall_storm": {
        "label": "Rainfall storm (Rudraprayag Town)",
        "description": "Rising rainfall + saturated soil + rising water level. "
                       "Trips the rainfall ML branch into WARNING territory.",
        "ward_id": "WD_1023",
        "duration_minutes": 90,
        "steps": 6,      # readings per sensor over the duration
    },
    "geohazard_creep": {
        "label": "Geohazard precursor (Gaurikund, glacier-fed)",
        "description": "Near-zero rainfall + slope tilt accelerating + sustained "
                       "temperature rise. Exercises the rule-based geohazard "
                       "path; rainfall model correctly stays quiet.",
        "ward_id": "WD_1044",
        "duration_minutes": 60,
        "steps": 4,
    },
    "reset": {
        "label": "Reset (clear all injected data)",
        "description": "Wipes the ward buffer and meteo store so a fresh demo "
                       "starts from Normal.",
    },
}


# ---------------------------------------------------------------------------
# Reading builders (per scenario)
# ---------------------------------------------------------------------------

def _mk_reading(ward: str, kind: SensorType, value: float, unit: str,
                mins_ago: int, idx: int) -> dict:
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


def _rainfall_storm_readings(ward: str, duration_min: int, n: int) -> List[dict]:
    """Escalating rainfall crossing a critical threshold. Ground-truth pattern:
    ~5 mm/step early, ~25-40 mm/step late. Soil already saturated. Water rising.
    """
    out: List[SensorReading] = []
    step = duration_min // (n - 1)
    for i in range(n):
        mins_ago = duration_min - i * step
        rain = 5 + i * 6              # 5, 11, 17, 23, 29, 35 mm/step
        soil = 78 + i * 2.5           # 78% -> 90%
        water = 1.0 + i * 0.28        # 1.0 -> 2.4 m
        out.append(_mk_reading(ward, SensorType.RAINFALL,       rain,  "mm", mins_ago, i))
        out.append(_mk_reading(ward, SensorType.SOIL_MOISTURE,  soil,  "%",  mins_ago, i))
        out.append(_mk_reading(ward, SensorType.WATER_LEVEL,    water, "m",  mins_ago, i))
    return out


def _geohazard_creep_readings(ward: str, duration_min: int, n: int) -> List[dict]:
    """Chamoli-style: rainfall near zero, slope tilt accelerating, sustained
    temperature rise. Ends past the risk_engine slope_tilt >= 5 deg and >= 3 C
    sustained temperature-rise thresholds for glacier-fed wards.
    """
    out: List[SensorReading] = []
    step = duration_min // (n - 1)
    for i in range(n):
        mins_ago = duration_min - i * step
        rain = 0.2 + i * 0.1                        # essentially dry
        # tilt: slow creep 1.5 -> 3.0 -> then jumps 6, 11 (past the 5/10 deg cuts)
        tilt = [1.5, 3.0, 6.5, 11.5][i] if i < 4 else 12.0
        temp = 4.0 + i * 1.4                        # +4 C rise over the window
        out.append(_mk_reading(ward, SensorType.RAINFALL,     rain, "mm",  mins_ago, i))
        out.append(_mk_reading(ward, SensorType.SLOPE_TILT,   tilt, "deg", mins_ago, i))
        out.append(_mk_reading(ward, SensorType.TEMPERATURE,  temp, "C",   mins_ago, i))
    return out


def _rainfall_storm_meteo_vector() -> Dict[str, float]:
    """The 34-feature vector for the rainfall-storm scenario.

    NOTE: these values are hand-calibrated to score in the WARNING band of the
    trained model. They are NOT drawn from a specific historical event -- if
    you want that, use the parquet replay script (replay_historical.py) instead.
    """
    return {
        # Terrain (Rudraprayag Town-ish)
        "elevation_m": 620.0, "slope_deg": 18.4,
        "aspect_sin": 0.71, "aspect_cos": -0.71, "terrain_ruggedness": 420.0,
        "flood_prone_terrain": 1.0,
        # Rainfall windows -- storm pattern
        "rain_intensity_3h": 12.0, "rain_3h": 36.0, "rain_6h": 62.0,
        "rain_12h": 95.0, "rain_24h": 128.0,
        "rain_48h": 155.0, "rain_72h": 175.0,
        "rain_5d": 190.0, "rain_7d": 210.0, "rain_14d": 245.0,
        "max_intensity_24h": 14.0, "wet_fraction_7d": 0.65,
        "rain_anomaly_24h": 113.0,   # 128 - climatological 15
        # ERA5 weather
        "temperature_2m_c": 18.0, "dewpoint_2m_c": 16.5,
        "humidity_pct": 92.0, "pressure_hpa": 948.0,
        "wind_speed_10m": 4.2, "wind_direction_10m": 195.0,
        "soil_moisture_m3m3": 0.42, "era5_precip_3h_mm": 34.0,
        # Hydrology
        "flow_accumulation": 4.2e5, "twi": 8.9,
        "dist_to_stream_m": 85.0, "nearest_stream_order": 5.0,
        "nearest_stream_flow_order": 4.0,
        "drainage_density_km_per_km2": 1.6, "dist_to_confluence_m": 1200.0,
    }


# ---------------------------------------------------------------------------
# Router endpoints
# ---------------------------------------------------------------------------

@router.get("/scenarios")
async def list_scenarios():
    """List available demo scenarios."""
    return [
        {"id": sid, "label": s["label"], "description": s["description"]}
        for sid, s in SCENARIOS.items()
    ]


@router.post("/trigger/{scenario_id}")
async def trigger_scenario(scenario_id: str):
    """Inject a scenario's readings + (if applicable) satellite feature vector
    into the running backend, then re-assess and dispatch."""
    if _buffer is None or _reassess_and_dispatch is None:
        raise HTTPException(500, "demo router not wired -- call wire() from main.py startup")

    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise HTTPException(404, f"unknown scenario '{scenario_id}'")

    if scenario_id == "reset":
        _buffer.clear()
        if _meteo_store is not None:
            _meteo_store.clear()
        return {"ok": True, "scenario": scenario_id, "readings": 0, "meteo_posted": False}

    ward = scenario["ward_id"]
    if ward not in WARD_REGISTRY:
        raise HTTPException(500, f"scenario references unknown ward '{ward}' -- check ward_config.py")

    # Build readings
    if scenario_id == "rainfall_storm":
        readings = _rainfall_storm_readings(ward, scenario["duration_minutes"], scenario["steps"])
        meteo_vector = _rainfall_storm_meteo_vector()
    elif scenario_id == "geohazard_creep":
        readings = _geohazard_creep_readings(ward, scenario["duration_minutes"], scenario["steps"])
        meteo_vector = None      # geohazard scenario deliberately keeps rainfall model quiet
    else:
        raise HTTPException(400, f"scenario '{scenario_id}' has no injector defined")

    # Push readings in through the real ingestion pipeline (validation, unit
    # conversion, dedup, spike-flagging) -- the same path as POST /ingest.
    ingest_batch(readings, _buffer)

    # Push meteo vector into the store, if any
    if meteo_vector is not None and _meteo_store is not None:
        _meteo_store.put(ward, meteo_vector, datetime.now(timezone.utc), source="demo_scenario")

    # Re-assess and let the alert path decide what to fire
    assessment = await _reassess_and_dispatch(ward)

    log.info("scenario %s triggered: ward=%s readings=%d meteo=%s risk=%s",
             scenario_id, ward, len(readings), meteo_vector is not None,
             getattr(assessment, "risk_level", "?"))

    return {
        "ok": True,
        "scenario": scenario_id,
        "ward_id": ward,
        "readings_injected": len(readings),
        "meteo_vector_posted": meteo_vector is not None,
        "assessed_risk_level": getattr(assessment, "risk_level", None),
        "assessed_confidence": getattr(assessment, "confidence", None),
    }


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
