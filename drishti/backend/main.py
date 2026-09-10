"""
main.py — the DRISHTI API service.

Wires the whole chain together:

    POST /ingest            ground sensor readings  ->  WardBuffer      (Branch B)
    POST /wards/{id}/meteo  satellite feature vector ->  MeteoStore     (Branch A)
                                        |
                                   fusion.py
                                        |
                             RiskAssessment (level + confidence)
                                        |
                              alert_dispatch.py (Tier 1/2/3)
                                        |
                            sms / push / dashboard  +  WS /ws/alerts

Run locally:
    uvicorn main:app --reload --port 8000

ENDPOINTS
    POST /ingest                        batch-ingest raw ground sensor readings;
                                        re-assesses affected wards and hands any
                                        alerts to the background dispatcher
                                        (returns immediately)
    POST /wards/{id}/meteo              push one satellite/reanalysis feature vector
    GET  /wards                         ward list + current risk (dashboard map)
    GET  /wards/{id}/risk               full assessment: level, confidence, per-signal
                                        breakdown, ML branch verdict, tier it would fire as
    GET  /wards/{id}/history/{type}     recent normalized readings
    GET  /dashboard/sensor-health       per-sensor last-seen / reporting state
    GET  /model/info                    what model is loaded, its thresholds, its caveats
    GET  /alerts/active                 Tier 2 countdowns + Tier 3 items awaiting review
    GET  /alerts/history                past alerts, newest first
    GET  /alerts/{id}                   single alert
    POST /alerts/{id}/veto              cancel a Tier 2 alert during its countdown
    POST /alerts/{id}/approve           dispatch a Tier 3 alert a human accepted
    POST /alerts/{id}/dismiss           reject a Tier 3 alert
    WS   /ws/alerts                     live alert lifecycle events
    GET  /health                        liveness + buffer/model state

DEPLOYMENT NOTE — READ BEFORE PUSHING THIS ANYWHERE
This service is stateful on purpose. The WardBuffer, the meteo store, the
pending-alert registry and the Tier 2 countdowns all live in process memory,
and `/ws/alerts` holds a long-lived WebSocket. It therefore needs a
long-running container (Render / Railway / Fly.io / a VM), NOT a serverless
platform. On serverless the Tier 2 dead-man's-switch — the design's whole
point — silently stops working: the function freezes after returning a
response, so the countdown never fires. See docs/DEPLOYMENT.md.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import config
from schemas import (
    Alert,
    AlertActionRequest,
    AlertActionResult,
    IngestResult,
    SensorType,
)
from ingestion import WardBuffer, ingest_batch
from risk_engine import RiskAssessment
from fusion import assess_ward_risk_fused
from model_service import model_service
from meteo_store import meteo_store
from ward_config import (
    WARD_REGISTRY,
    expected_sensor_types,
    get_ward_info,
    known_ward_ids,
)
from alert_dispatch import AlertDispatcher, alert_to_cap_xml, build_alert, classify_tier
from demo_scenarios import router as demo_router, wire as wire_demo

# Single shared in-memory state for the prototype. Replace with a proper store
# (and dependency-injected access) before this goes past demo stage.
buffer = WardBuffer()
dispatcher = AlertDispatcher()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Loading the booster here rather than at import time keeps a missing or
    # broken model artifact from turning into an import error that takes the
    # whole API down. `model_service.load()` never raises.
    model_service.load()
    await dispatcher.start()
    try:
        yield
    finally:
        await dispatcher.stop()


app = FastAPI(
    title="DRISHTI — Flash Flood Prediction System",
    description=(
        "Hyperlocal flash-flood risk scoring and tiered alerting for hilly regions. "
        "Extends FFGS rather than replacing it."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def assess(ward_id: str) -> RiskAssessment:
    """Every risk read in this service goes through here, so the ground-sensor
    branch and the ML branch can never get out of sync between endpoints."""
    return assess_ward_risk_fused(
        ward_id, buffer, meteo_store=meteo_store, model_service=model_service
    )


async def reassess_and_dispatch(ward_id: str) -> RiskAssessment:
    """Re-run fusion for one ward and hand any resulting alert to the
    dispatcher. Mirrors the inline logic in POST /ingest; used by the
    demo-scenario router (demo_scenarios.py)."""
    assessment = assess(ward_id)
    alert = build_alert(assessment)
    if alert is not None:
        await dispatcher.submit(alert)
    return assessment


# Demo-scenario router — injects pre-built telemetry through real ingestion so
# the dashboard reacts exactly as it would to live data. See demo_scenarios.py.
wire_demo(buffer, meteo_store, reassess_and_dispatch, dispatcher.clear_demo_state)
app.include_router(demo_router)


# --- Ingestion ------------------------------------------------------------


@app.post("/ingest", response_model=IngestResult)
async def ingest(readings: List[dict]) -> IngestResult:
    """Ingest ground sensor readings and hand off any alerts WITHOUT waiting
    for them to send.

    `submit()` only schedules — the actual SMS/push calls happen on background
    workers, so a slow gateway can never back-pressure sensor ingestion.
    """
    result = ingest_batch(readings, buffer)

    affected_wards = {r.ward_id for r in result.accepted}
    for ward_id in affected_wards:
        alert = build_alert(assess(ward_id))
        if alert is not None:
            await dispatcher.submit(alert)

    return result


class MeteoFeaturePayload(BaseModel):
    """One ward's satellite/reanalysis feature vector.

    `features` keys must match the trained feature names — `GET /model/info`
    returns the exact list. Unknown keys are ignored; absent ones become NaN,
    which XGBoost handles natively. Both are reported back so a broken upstream
    job shows up instead of quietly producing a confident all-clear.
    """

    features: Dict[str, Optional[float]]
    observed_at: Optional[datetime] = Field(
        None, description="When the observation window closed (UTC). Defaults to now."
    )
    source: str = Field("unspecified", description="e.g. gpm_imerg_v07+era5")


@app.post("/wards/{ward_id}/meteo")
async def push_meteo_features(ward_id: str, payload: MeteoFeaturePayload):
    """Accept a meteo-hydro feature vector, re-assess the ward, and dispatch any
    resulting alert — the Branch A equivalent of `POST /ingest`.

    Returns the verdict immediately so the upstream job can log what its data
    produced, rather than posting into a void.
    """
    cleaned = {k: v for k, v in payload.features.items() if v is not None}
    record = meteo_store.put(
        ward_id, cleaned, observed_at=payload.observed_at, source=payload.source
    )

    assessment = assess(ward_id)
    alert = build_alert(assessment)
    if alert is not None:
        await dispatcher.submit(alert)

    return {
        "ward_id": ward_id,
        "accepted_features": len(cleaned),
        "observed_at": record.observed_at.isoformat(),
        "risk_level": assessment.risk_level.value,
        "confidence": assessment.confidence,
        "model_branch": assessment.model_branch,
        "alert_id": alert.alert_id if alert else None,
    }


# --- Wards ----------------------------------------------------------------


@app.get("/wards")
def list_wards():
    """Ward list with current risk — backs the dashboard's ward map.

    `lat`/`lon` here are the schematic MAP POSITION percentages from
    ward_config, not geography; `latitude`/`longitude` carry the real
    coordinates once the GIS layer lands. Both are returned under their honest
    names, with the short aliases kept for the dashboard's existing marker
    placement.
    """
    out = []
    for ward_id in known_ward_ids():
        info = get_ward_info(ward_id)
        a = assess(ward_id)
        out.append(
            {
                "ward_id": ward_id,
                "name": info["name"],
                "risk": a.risk_level.value,          # dashboard alias
                "risk_level": a.risk_level.value,
                "confidence": a.confidence,
                "lead": a.estimated_lead_time_minutes,   # dashboard alias
                "estimated_lead_time_minutes": a.estimated_lead_time_minutes,
                "lead_time_band": a.lead_time_band,
                "lead_time_driver": a.lead_time_driver,
                "lead_time_basis": a.lead_time_basis,
                "impact_imminent": a.impact_imminent,
                "glacier": info["glacier_fed"],      # dashboard alias
                "glacier_fed": info["glacier_fed"],
                "evac": info["evacuation_point"],    # dashboard alias
                "evacuation_point": info["evacuation_point"],
                "lat": info["map_y"],                # schematic map %, not geography
                "lon": info["map_x"],
                "map_x": info["map_x"],
                "map_y": info["map_y"],
                "latitude": info["latitude"],
                "longitude": info["longitude"],
                "model_available": a.model_branch.get("available", False),
            }
        )
    return out


@app.get("/wards/{ward_id}/risk")
def ward_risk(ward_id: str):
    """Full assessment for one ward, including which alert tier it would fire
    as and what each branch contributed."""
    a = assess(ward_id)
    info = get_ward_info(ward_id)
    tier, tier_reason = classify_tier(a.risk_level, a.confidence)
    return {
        "ward_id": a.ward_id,
        "name": info["name"],
        "risk_level": a.risk_level.value,
        "confidence": a.confidence,
        "confidence_factors": a.confidence_factors,
        "would_dispatch_as": tier.value,
        "tier_reason": tier_reason,
        "estimated_lead_time_minutes": a.estimated_lead_time_minutes,
        "lead_time_band": a.lead_time_band,
        "lead_time_driver": a.lead_time_driver,
        "lead_time_basis": a.lead_time_basis,
        "impact_imminent": a.impact_imminent,
        "evacuation_point": info["evacuation_point"],
        "glacier_fed": info["glacier_fed"],
        "reasons": a.reasons,
        "data_completeness": a.data_completeness,
        "stale_sensor_types": a.stale_sensor_types,
        "model_branch": a.model_branch,
        "assessed_at": a.assessed_at.isoformat(),
        "signals": [
            {
                "name": s.name,
                "level": s.level.value,
                "value": s.value,
                "threshold": s.threshold,
                "samples": s.samples,
                "anomalous_samples": s.anomalous_samples,
                "available": s.available,
                "detail": s.detail,
            }
            for s in a.signals
        ],
    }


@app.get("/wards/{ward_id}/history/{sensor_type}")
def ward_history(ward_id: str, sensor_type: SensorType):
    readings = buffer.recent(ward_id, sensor_type)
    if not readings:
        raise HTTPException(status_code=404, detail="no data for this ward/sensor type")
    return [r.model_dump(mode="json") for r in readings]


# --- Dashboard support ----------------------------------------------------


@app.get("/dashboard/sensor-health")
def sensor_health(ward_id: Optional[str] = None):
    """Sensor-health panel: live sensors, plus expected sensors that are NOT
    reporting at all.

    Live sensors still inside WardBuffer's rolling window are listed with
    `status` "reporting" or "silent" (seen, but not within
    SENSOR_SILENT_AFTER_MINUTES). Expected sensor types (see
    ward_config.expected_sensor_types) with no live sensor at all are appended
    with `status` "missing" — so a node destroyed mid-event, once its last
    reading rotates out of the buffer, shows as a gap rather than vanishing.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=config.SENSOR_SILENT_AFTER_MINUTES)
    out = []
    covered: set[tuple[str, str]] = set()
    for sensor_id, reading in sorted(buffer.known_sensors(ward_id).items()):
        ts = reading.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        reporting = ts >= cutoff
        covered.add((reading.ward_id, reading.type.value))
        out.append(
            {
                "sensor_id": sensor_id,
                "ward_id": reading.ward_id,
                "ward_name": get_ward_info(reading.ward_id)["name"],
                "type": reading.type.value,
                "last_seen": ts.isoformat(),
                "reporting": reporting,
                "status": "reporting" if reporting else "silent",
                "expected": reading.type.value in expected_sensor_types(reading.ward_id),
                "last_value": reading.value,
                "unit": reading.unit,
            }
        )

    scope = [ward_id] if ward_id is not None else known_ward_ids()
    for w_id in scope:
        for s_type in expected_sensor_types(w_id):
            if (w_id, s_type) in covered:
                continue
            out.append(
                {
                    "sensor_id": f"{w_id}:{s_type}",
                    "ward_id": w_id,
                    "ward_name": get_ward_info(w_id)["name"],
                    "type": s_type,
                    "last_seen": None,
                    "reporting": False,
                    "status": "missing",
                    "expected": True,
                    "last_value": None,
                    "unit": None,
                }
            )
    return out


@app.get("/model/info")
def model_info():
    """What ML model is actually loaded, its operating points, and its stated
    limits. Exposed as an endpoint so the dashboard (and anyone reviewing this)
    can check what is running instead of trusting a slide."""
    return {
        **model_service.info(),
        "meteo_store": meteo_store.stats(),
        "wards_with_features": meteo_store.wards_with_features(),
    }


# --- Alerts ---------------------------------------------------------------


@app.get("/alerts/active", response_model=List[Alert])
def active_alerts(ward_id: Optional[str] = None):
    """Alerts still awaiting an outcome — Tier 2 countdowns and Tier 3 reviews.

    Each carries `veto_deadline` (absolute UTC) plus a `seconds_remaining`
    convenience value; the dashboard should count down from the deadline so a
    slow response doesn't desync the timer.
    """
    return dispatcher.active(ward_id)


@app.get("/alerts/history", response_model=List[Alert])
def alert_history(ward_id: Optional[str] = None, limit: int = 100):
    return dispatcher.history(ward_id, limit)


@app.get("/alerts/{alert_id}", response_model=Alert)
def get_alert(alert_id: str):
    alert = dispatcher.get(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="unknown alert id")
    return alert


@app.get("/alerts/{alert_id}/cap")
def get_alert_cap(alert_id: str):
    """The alert rendered as CAP 1.2 XML — the SACHET / NDMA wire format.

    `status` is "Exercise" in the payload: DRISHTI is not wired to a live
    public broadcaster, and the XML says so rather than implying otherwise.
    """
    alert = dispatcher.get(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="unknown alert id")
    return Response(content=alert_to_cap_xml(alert), media_type="application/xml")


@app.post("/alerts/{alert_id}/veto", response_model=AlertActionResult)
async def veto_alert(alert_id: str, body: AlertActionRequest):
    """Cancel a Tier 2 alert mid-countdown.

    Returns 200 with `ok=false` when the veto arrived too late — that is a real
    outcome the operator needs to see, not an error to swallow.
    """
    result = await dispatcher.veto(alert_id, body.actor, body.reason)
    if not result.ok and result.status is None:
        raise HTTPException(status_code=404, detail=result.detail)
    return result


@app.post("/alerts/{alert_id}/approve", response_model=AlertActionResult)
async def approve_alert(alert_id: str, body: AlertActionRequest):
    result = await dispatcher.approve(alert_id, body.actor, body.reason)
    if not result.ok and result.status is None:
        raise HTTPException(status_code=404, detail=result.detail)
    return result


@app.post("/alerts/{alert_id}/dismiss", response_model=AlertActionResult)
async def dismiss_alert(alert_id: str, body: AlertActionRequest):
    result = await dispatcher.dismiss(alert_id, body.actor, body.reason)
    if not result.ok and result.status is None:
        raise HTTPException(status_code=404, detail=result.detail)
    return result


@app.websocket("/ws/alerts")
async def alerts_feed(websocket: WebSocket):
    """Push alert lifecycle events (queued / pending_veto / vetoed / dispatched).

    The dashboard should render Tier 2 countdowns from `veto_deadline` in each
    payload rather than polling.
    """
    await websocket.accept()
    queue = dispatcher.subscribe()
    try:
        await websocket.send_json(
            {
                "event": "snapshot",
                "alerts": [a.model_dump(mode="json") for a in dispatcher.active()],
            }
        )
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    finally:
        dispatcher.unsubscribe(queue)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "buffer": buffer.stats(),
        "meteo_store": meteo_store.stats(),
        "model_available": model_service.available,
        "model_reason": model_service.reason_unavailable,
        "wards_configured": len(WARD_REGISTRY),
    }
