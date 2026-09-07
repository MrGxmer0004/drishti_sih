# Flash Flood — Sensor Ingestion & Alert System

Prototype for the "last mile" of PS 26192: takes raw sensor payloads,
validates/normalizes them, runs a (currently rule-based) risk assessment
per ward, and generates + dispatches **confidence-tiered** alerts.

## Files
- `schemas.py` — SensorReading / Alert data contracts, canonical units, valid ranges, alert tier + status enums
- `unit_conversion.py` — converts arbitrary incoming units to canonical units
- `ingestion.py` — validation, dedup (bounded), spike-flagging, `WardBuffer` (in-memory store)
- `ward_config.py` — mock ward metadata (evacuation point, glacier-fed flag)
- `risk_engine.py` — **rule-based placeholder** for the trained model; also produces the confidence score — see swap point below
- `alert_dispatch.py` — tier routing, veto countdowns, async background dispatch to sms/push/dashboard stubs
- `main.py` — FastAPI app: ingest, risk, alert log, veto/approve, websocket feed
- `demo.py` — runs all three alert tiers end-to-end with synthetic data, no server needed
- `test_fixes.py` — regression tests for the issues fixed in this pass

## Run it
```bash
pip install fastapi uvicorn pydantic
python demo.py                         # end-to-end, shows Tier 1/2/3 including a live veto
python test_fixes.py                   # regression tests
uvicorn main:app --reload --port 8000  # or run as a service
```

## Alert tiering (the dead-man's-switch)

Alerts are routed by the risk engine's **confidence**, not by risk level:

| Tier | Confidence | Behaviour |
|---|---|---|
| 1 | `>= 0.85` | Dispatched immediately. No human in the loop. |
| 2 | `0.55 – 0.85` | Countdown starts; **auto-dispatches when it expires**. A human can CANCEL, not approve. |
| 3 | `< 0.55` | Dashboard only. Nothing is sent until a human explicitly approves. |

Tier 2 is a veto, not an approval, because the Nepal case had lead times too
short for "wait for a human to say yes" to be survivable. Inaction has to
resolve toward warning people.

Two policy rules worth knowing about (both are constants in `alert_dispatch.py`):
- **`CRITICAL_NEVER_REVIEW_ONLY`** — a CRITICAL assessment never sits in Tier 3,
  even at low confidence. It drops to Tier 2 so someone still has to actively
  kill it.
- **Veto window sizing** — the window is `min(90s, 20% of estimated lead time)`.
  If that lands under 30s, no window is offered at all and the alert is
  promoted to immediate dispatch. The countdown must never eat the lead time
  it exists to protect.

`veto_deadline` on the alert is an absolute UTC instant, so the dashboard can
run its own countdown without trusting a duration that went stale in transit.

## API

```
POST /ingest                      batch-ingest readings; returns immediately (dispatch is backgrounded)
GET  /wards/{id}/risk             risk level, confidence, per-signal breakdown, which tier it would fire as
GET  /wards/{id}/history/{type}   recent normalized readings
GET  /alerts/active               Tier 2 countdowns + Tier 3 items awaiting review
GET  /alerts/history              past alerts, newest first
POST /alerts/{id}/veto            cancel a Tier 2 alert mid-countdown   {"actor": "...", "reason": "..."}
POST /alerts/{id}/approve         dispatch a reviewed Tier 3 alert
POST /alerts/{id}/dismiss         reject a Tier 3 alert
WS   /ws/alerts                   live alert lifecycle events
```

A veto that arrives too late returns **200 with `ok: false`** and the current
status, not an error. The operator needs to see "it already went out" rather
than a failure that looks like a network problem.

## Swapping in the trained model
Everything routes through `risk_engine.assess_ward_risk(ward_id, buffer, now=None) -> RiskAssessment`.
Once the model is ready, replace the body of that function and keep returning a
`RiskAssessment`. Nothing in `alert_dispatch.py` or `main.py` needs to change.

**The model must also produce a calibrated `confidence`.** Tiering is driven
entirely by that number — a model that returns a bare risk level will dump
every alert into one tier.

## Design notes / what this covers from the brief
- **Validation**: schema shape, timestamp freshness, physically-plausible value
  ranges per sensor type, unsupported units rejected explicitly.
- **Normalization**: all values converted to canonical units before anything downstream sees them.
- **Glacier gap**: `ward_config.glacier_fed` + sustained temperature-rise detection in
  `risk_engine._glacier_melt_signal` — the FFGS blind spot.
- **Evacuation plan gap**: every `WARNING`/`CRITICAL` alert carries a concrete
  `evacuation_point` from ward metadata.
- **Time-based windows**: every risk rule filters by wall-clock time, and sorts
  by timestamp before computing rises, so an out-of-order backlog flush from a
  reconnecting sensor can't be read as a live event.
- **Explainability**: assessments carry per-signal values, thresholds and sample
  counts, plus a line-by-line confidence breakdown — useful for the dashboard
  and for defending a tier decision after the fact.
- Rejected readings are never silently dropped — `IngestResult.rejected` carries
  a reason per item.

## Known placeholders to revisit
- `WardBuffer` is in-memory only — swap for TimescaleDB/InfluxDB before real deployment.
- **The pending-alert registry and dispatch queue are also in-memory.** A crash
  loses Tier 2 countdowns and queued alerts. Graceful shutdown fires pending
  Tier 2 alerts (a restart is not a human veto), but a hard kill does not. Back
  both with Redis or a DB table + scheduled sweep before this is trusted.
- `risk_engine.py` thresholds are guesses — recalibrate against the historical
  landslide/rainfall data once the data-prep team's dataset lands. The
  confidence weights are guesses too, and matter more, since they decide
  whether a human gets a say.
- Channel senders in `alert_dispatch.py` just log — wire up real SMS/push
  providers with `httpx.AsyncClient`. Retry/backoff and per-channel failure
  reporting are already in place around them.
- `ward_config.py` has two sample wards — needs the real ward registry.
- No auth on the veto/approve endpoints yet. `actor` is self-reported. That
  needs to be a real authenticated identity before anyone relies on the audit trail.
