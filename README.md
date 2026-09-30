# DRISHTI

**Disaster Risk Identification System for Hyperlocal Threat Intelligence**

Ward-level flash flood risk scoring and tiered alerting for hilly regions.
Built for SIH problem statement 26192.

DRISHTI does not replace India's Flash Flood Guidance System — it extends it.
FFGS is one input among several. What DRISHTI adds is the missing last mile
(ward-level risk, lead time, actionable alerts) plus a detection capability
FFGS and every comparable system worldwide currently lacks: flash floods
triggered by something other than rainfall.

---

## What's in here

Three deployable pieces plus documentation.

```
drishti/
├── backend/     FastAPI service — ingestion, risk, fusion, tiered alerting
├── frontend/    Next.js authority dashboard (deploys to Vercel)
├── ml/          Trained XGBoost model, its artifacts, and the scorer
├── docs/        Briefing, deployment guide, model feature contract
├── Dockerfile   Backend container (Render/Railway/Fly)
└── render.yaml  Render blueprint for the backend
```

### `backend/` — the API service

| File | What it does |
|---|---|
| `main.py` | The FastAPI app. Every route lives here. Start with this file. |
| `config.py` | Every environment-dependent value: model paths, feature flags, CORS, staleness limits. |
| `schemas.py` | The data contracts — `SensorReading`, `Alert`, risk levels, alert tiers and statuses, canonical units, valid ranges. Everything else is built against these. |
| `unit_conversion.py` | Converts incoming sensor units (inches, cm, °F…) to canonical units so nothing downstream thinks about units. |
| `ingestion.py` | Validation, range checks, deduplication, spike flagging, and `WardBuffer` — the in-memory rolling store the risk engine reads from. |
| `ward_config.py` | Ward metadata: name, evacuation point, glacier-fed flag, baseline lead time, dashboard map position. Mock data; needs the real ward registry. |
| `risk_engine.py` | **Branch B.** Rule-based scoring of the five ground sensor types, including the water-level, slope-tilt and glacier-melt signals no rainfall model can see. Also produces the confidence score. |
| `model_service.py` | **Branch A.** Loads the trained XGBoost model, scores a satellite feature vector, returns `WARNING`/`WATCH`/`NONE` plus a confidence. Degrades cleanly if the model is missing. |
| `meteo_store.py` | Holds the latest satellite feature vector per ward, waiting for the next assessment. Separate from `WardBuffer` on purpose. |
| `fusion.py` | **Where the two branches meet.** Combines the rule verdict and the model verdict into one `RiskAssessment`. This is the integration point — see below. |
| `alert_dispatch.py` | Tier routing, veto countdowns, async background dispatch, retries, per-channel failure reporting. |
| `sample_meteo_vectors.py` | Three feature vectors that land in each model tier, for demos and smoke tests. Read its docstring before trusting the numbers. |
| `demo_end_to_end.py` | Six scenarios through the whole pipeline, no server needed. `python demo_end_to_end.py` |
| `tests/test_pipeline_regressions.py` | 25 tests, each named for the bug it pins down. `python tests/test_pipeline_regressions.py` |
| `requirements.txt` | Python dependencies, with notes on which are optional and why. |

### `frontend/` — the authority dashboard

| File | What it does |
|---|---|
| `components/DrishtiDashboard.jsx` | The entire dashboard: ward risk map, ward detail drawer, sensor health, alert log, and the live veto/approve/dismiss controls. Also contains the demo simulator. |
| `app/page.jsx` | Next.js route that renders it. |
| `app/layout.jsx` | HTML shell and metadata. |
| `app/globals.css` | Page shell only — the dashboard styles itself inline. |
| `.env.example` | The one variable that matters: `NEXT_PUBLIC_API_BASE`. |

### `ml/` — the model

| File | What it does |
|---|---|
| `train_flash_flood_model.py` | The training run. Its docstring records what was tried and rejected — read it before proposing an improvement. |
| `inference_two_tier_scorer.py` | `FloodScorer` — loads the booster, applies both thresholds. The backend imports this directly, so there is one copy, not two. |
| `generate_feature_contract.py` | Regenerates the feature range table from the model. Run after every retrain. |
| `artifacts/flash_flood_model_FINAL_v2.json` | The trained booster. |
| `artifacts/final_model_results.json` | CV results, threshold sweep, both operating points, feature list. The API reads its thresholds from here. |
| `artifacts/oof_*.npy` | Out-of-fold scores and labels, for optional calibration. |
| `README.md` | Operating points, where the confidence number comes from, calibration, scope limits. |

### `docs/`

| File | What it does |
|---|---|
| `PROJECT_BRIEFING.md` | Full project context — problem, positioning, architecture, data pipeline, open issues. |
| `DEPLOYMENT.md` | How to deploy, and why the backend cannot go on Vercel. |
| `MODEL_FEATURE_CONTRACT.md` | The range of every model feature, and the traps. Required reading before writing the upstream ingest job. |
| `BACKEND_ORIGINAL_NOTES.md` | The original backend notes, kept for reference. |

---

## How the model is integrated

The trained model is **not** a drop-in replacement for `risk_engine.py`, even
though the original design anticipated one. They operate on different data:

- The model takes **34 satellite/reanalysis features** — GPM IMERG rainfall,
  ERA5 weather, SRTM terrain, HydroSHEDS hydrology, on a 0.1° grid.
- The rule engine takes **five ground sensor readings** — rainfall, soil
  moisture, water level, slope tilt, temperature, from IoT nodes.

There is no defensible mapping from five ground sensors to 34 satellite
features, so the integration does not attempt one. `fusion.py` combines the two
**verdicts** while keeping their inputs separate.

**Risk level** is the maximum of the two. Neither branch can see what the other
sees, so a signal from either is real.

**Confidence** — the number that decides whether a human gets a veto window —
follows four rules:

| Situation | Confidence | Effect |
|---|---|---|
| Both branches elevated | Combined as independent evidence, capped at 0.95 | Corroboration can promote to Tier 1 |
| Model only | Model confidence − 0.10 | A satellite-only call **never** fires fully automatically |
| Rule only, model saw fresh data and disagreed | Rule confidence − 0.10 | Fresh satellite data seeing nothing is evidence against |
| Rule only, driven by a signal the model cannot observe | **No penalty** | See below |

That last row is the one that matters most. The rainfall model has no
water-level, slope-tilt or seismic input. A glacier collapse or slope failure
is invisible to it by construction. Penalising the geohazard branch for the
rainfall model's silence would suppress exactly the events DRISHTI exists to
catch — three of the four recent Himalayan disasters in the briefing. There is
a test named for it:
`test_quiet_model_does_not_penalise_a_geohazard_call`.

Worked example from `demo_end_to_end.py`, scenario E: ground sensors alone give
CRITICAL at 0.60 confidence, which is Tier 2. The satellite model agrees. Fused
confidence rises to 0.94, which is Tier 1 — it fires immediately instead of
waiting out a countdown. The corroboration bought lead time.

**Every fusion constant is a guess**, in the same sense as the thresholds in
`risk_engine.py`. They need re-deriving against a validation set where both
branches were live simultaneously. That dataset does not exist yet.

---

## Alert tiering

Alerts are routed by **confidence**, not by risk level.

| Tier | Confidence | Behaviour |
|---|---|---|
| 1 | ≥ 0.85 | Dispatched immediately. No human in the loop. |
| 2 | 0.55 – 0.85 | Countdown starts, then **auto-dispatches**. A human can cancel, not approve. |
| 3 | < 0.55 | Dashboard only. Nothing sends without explicit approval. |

Tier 2 is a veto, not an approval gate. The Nepal case had single-digit-minute
lead times, so "wait for a human to say yes" is not survivable. Inaction has to
resolve toward warning people.

Two policies worth knowing:

- A **CRITICAL** assessment never sits in Tier 3, even at low confidence. It
  drops to Tier 2, so someone still has to actively kill it.
- The veto window is `min(90s, 20% of estimated lead time)`. If that lands
  under 30s, no window is offered and the alert is promoted to immediate
  dispatch. The countdown must never eat the lead time it exists to protect.

---

## Running it

```bash
# Backend
cd backend
pip install -r requirements.txt
python demo_end_to_end.py                 # six scenarios, no server
python tests/test_pipeline_regressions.py # 25 tests
uvicorn main:app --reload --port 8000     # the service

# Frontend
cd frontend
npm install
npm run dev                               # demo mode by default
```

The dashboard runs in demo mode with a built-in simulator unless
`NEXT_PUBLIC_API_BASE` is set — including a live Tier 2 countdown you can
actually veto. Deployment details in `docs/DEPLOYMENT.md`.

---

## API

```
POST /ingest                      ground sensor readings; returns immediately
POST /wards/{id}/meteo            one satellite feature vector for a ward
GET  /wards                       ward list + current risk (dashboard map)
GET  /wards/{id}/risk             full assessment, both branches, tier it would fire as
GET  /wards/{id}/history/{type}   recent normalized readings
GET  /dashboard/sensor-health     per-sensor last-seen / reporting state
GET  /model/info                  what model is loaded, its operating points, its limits
GET  /alerts/active               Tier 2 countdowns + Tier 3 items awaiting review
GET  /alerts/history              past alerts, newest first
POST /alerts/{id}/veto            cancel a Tier 2 alert mid-countdown
POST /alerts/{id}/approve         dispatch a reviewed Tier 3 alert
POST /alerts/{id}/dismiss         reject a Tier 3 alert
WS   /ws/alerts                   live alert lifecycle events
GET  /health                      liveness, buffer state, model state
```

A veto that arrives too late returns **200 with `ok: false`** and the current
status, not an error. The operator needs to see "it already went out" rather
than something that looks like a network problem.

`/model/info` exists so anyone can check what is actually running instead of
trusting a slide. It includes the model's stated caveats.

---

## Known limitations

Kept here rather than buried, because several of them affect how results should
be described.

**System**

- All state is in-process memory: ward buffer, meteo store, pending alerts,
  Tier 2 countdowns. A crash loses them. Graceful shutdown fires pending Tier 2
  alerts — a restart is not a human veto — but a hard kill does not.
- No authentication on veto/approve/dismiss. `actor` is self-reported, so the
  audit trail is currently a suggestion.
- Channel senders log only. No SMS or push actually leaves the building. The
  retry, timeout and failure reporting around them are real.
- One worker only. The alert registry is per-process.
- `risk_engine.py` thresholds and confidence weights are guesses. The
  confidence weights matter more, since they decide whether a human gets a say.
- Sensor health only sees sensors still inside the rolling window. A node
  destroyed mid-event eventually vanishes rather than showing as silent —
  fixing that needs a registry of expected sensors.

**Model**

- Covers rainfall-triggered floods only. The geohazard branch is a separate
  model on separate data, not yet trained.
- Partial circularity: the label is defined by a rule over `rain_24h` and the
  model receives `rain_24h` as a feature. Describe results as "identifies
  heavy-rain flood signatures", not "predicts floods from scratch".
- Slope is coarse (~11 km baseline, median 1.4° against a real ~28°).
  Recovering the raw SRTM tiles is the highest-value open task — and the model
  must be **retrained** afterwards, since real slopes sit outside every slope
  split it currently has.
- Raw scores are not calibrated probabilities. Confidence is a
  population-level figure; every WARNING carries the same one.
- Out-of-range features fail silently. See
  `docs/MODEL_FEATURE_CONTRACT.md` — this is the most likely way the
  integration breaks in production.
