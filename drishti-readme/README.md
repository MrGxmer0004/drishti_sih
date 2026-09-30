<div align="center">

# DRISHTI
### Disaster Risk Identification System for Hyperlocal Threat Intelligence

**Ward-level flash flood and geohazard early warning for India's hill districts.**
Fuses satellite rainfall, soil moisture, river level, slope tilt and glacier-melt signals into one risk score per ward, then decides how to alert based on how confident it is.

[![Live demo](https://img.shields.io/badge/▶_Live_demo-drishti--sih--liart.vercel.app-E0584F?style=for-the-badge)](https://drishti-sih-liart.vercel.app/)
[![Walkthrough video](https://img.shields.io/badge/🎥_Walkthrough-video-D9AE45?style=for-the-badge)](https://bit.ly/3VhVq5B)
[![Report](https://img.shields.io/badge/📄_Report-read-6F8FC7?style=for-the-badge)](https://bit.ly/4AvufEE)

**SIH 2026 · PS ID SIH26192 · Disaster Management · Team ThunderBolts! (Team ID 159702)**

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)
![XGBoost](https://img.shields.io/badge/XGBoost-rainfall_model-EB6E1F)
![Next.js](https://img.shields.io/badge/Next.js-14-000000?logo=nextdotjs&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Render-2496ED?logo=docker&logoColor=white)
![Tests](https://img.shields.io/badge/regression_tests-25-3FA179)

<img src="assets/readme/hero.jpg" alt="DRISHTI authority dashboard: two wards escalating, one Critical and one Warning, with a live veto countdown" width="100%">

<sub>The authority dashboard during a two-ward escalation. Rudraprayag Town is Critical at 92% confidence (both branches agree), so it has already been dispatched. Sonprayag is at Warning with a Tier 2 veto countdown running.</sub>

</div>

---

## At a glance

| | |
|---|---|
| 🎯 **Problem** | Himalayan flash floods give minutes of warning, and existing systems stop at basin-level guidance on a forecaster's desk. |
| 💡 **What DRISHTI adds** | **Ward-level** risk, a **projected lead time**, and the **nearest evacuation point**, pushed out as a tiered alert. |
| ⛰️ **Differentiator** | A **geohazard branch** (slope tilt, glacier-melt temperature, water level) that catches floods **not caused by rain**, such as Chamoli 2021 and the 2023 Sikkim GLOF. Rainfall-only systems (FFGS, Google Flood Hub, NWS-FLASH) cannot see these by design. |
| ⏱️ **Alerting principle** | **Inaction still warns people.** Medium-confidence alerts auto-send after a countdown unless a human cancels them. |
| 🧠 **Model** | XGBoost on 34 satellite and reanalysis features (GPM IMERG, ERA5, SRTM, HydroSHEDS). Leave-one-storm-out CV, **PR-AUC 0.93**. |
| 🏗️ **Built** | FastAPI pipeline (ingest → risk → fusion → tiered dispatch), trained and deployed model, live dashboard, 25 regression tests. |

---

## See it working

<div align="center">
<img src="assets/readme/demo-veto.gif" alt="Rainfall storm scenario: Rudraprayag Town turns Critical, a Tier 2 countdown starts, and the operator cancels it" width="90%">

<sub><b>Rainfall storm → Critical → 90 s veto window → operator cancels with a logged reason.</b> The telemetry is scripted, but it runs through the same ingestion, risk and alert path that real sensor data would take.</sub>
</div>

### Scenario 1 — Rainfall storm: Tier 2 veto and a projected lead time

<img src="assets/readme/beat1-rainfall-storm.jpg" alt="Rudraprayag Town Critical with 15 min lead time and a live veto countdown" width="100%">

- Rainfall of **80 mm in 60 min** crosses the threshold, and **88% soil saturation amplifies** the risk. The river rises 0.56 m in 26 min.
- **Lead time ≈ 15 min (band 14–16)** is a least-squares projection of when accumulation crosses the critical line, not a fixed number.
- At **73% confidence** the alert lands in **Tier 2**. It **auto-broadcasts in 1:30** unless an operator cancels it.
- A slope-tilt sensor that has gone quiet is flagged as a signal, not ignored.

### Scenario 2 — Geohazard precursor: the event rainfall models miss

<img src="assets/readme/beat2-geohazard.jpg" alt="Gaurikund Critical with near-zero rain, driven by slope tilt past critical and glacier-melt temperature rise" width="100%">

- **Gaurikund** is glacier-fed. There is **near-zero rain (0.7 mm)**, yet the ward is **Critical**.
- **Slope tilt has accelerated to 12°**, past the critical angle, and melt temperature has risen **5.5 °C over 3 h**.
- The driver is already past threshold, so the lead time reads **"impact imminent"** and the alert fires **immediately** with no veto window.
- The satellite rainfall branch **cannot observe this at all**. DRISHTI does not treat its silence as an all-clear, which is the gap behind Chamoli, Sikkim and Trishuli-class events.

### Scenario 3 — Two wards at once: both branches agree and the alert fires automatically

<img src="assets/readme/beat3-multiward-fusion.jpg" alt="Rudraprayag Town Critical at 92% confidence with the satellite model at WATCH, Sonprayag at Warning" width="100%">

- In Rudraprayag Town, the **ground sensors and the satellite ML model agree** (model score 0.57 → WATCH). Combined confidence rises from **0.73 to 0.92**, which is **Tier 1**, so the alert is **dispatched instantly**. Corroboration bought back the veto window's minutes.
- Sonprayag reaches **Warning** in parallel and gets its own Tier 2 countdown.

<table>
<tr>
<td width="60%"><img src="assets/readme/alerts-and-override.jpg" alt="Alerts and override tab showing Tier 1 dispatched and Tier 2 veto open"></td>
<td width="40%"><img src="assets/readme/demo-controls.jpg" alt="Demo control panel with rehearsed scenario triggers"></td>
</tr>
<tr>
<td><sub><b>Alerts & override:</b> an audit log of every alert with its tier, status and time. Veto windows sit on the left.</sub></td>
<td><sub><b>Demo controls</b> (<code>/demo-controls.html</code>): rehearsed triggers, clearly labelled as not live sensors.</sub></td>
</tr>
</table>

> **Try it:** open the [live demo](https://drishti-sih-liart.vercel.app/). With no backend configured, it runs a built-in simulator that includes a Tier 2 countdown you can veto.

---

## How it works

<img src="assets/readme/architecture-overview.jpg" alt="DRISHTI architecture: meteo-hydro and geohazard sources feed the ingestion layer, the risk engine, tiered alert dispatch, SACHET, the citizen warning and the dashboard" width="100%">

```mermaid
flowchart LR
    subgraph A["Branch A · Satellite / reanalysis"]
        S1[GPM IMERG rain<br/>ERA5 weather<br/>SRTM terrain<br/>HydroSHEDS] --> M[XGBoost model<br/>34 features]
    end
    subgraph B["Branch B · Ground IoT sensors"]
        S2[Rainfall · Soil moisture<br/>Water level · Slope tilt<br/>Temperature] --> I[Ingestion<br/>validate · normalise units<br/>dedupe · flag spikes] --> R[Rule engine<br/>incl. geohazard signals]
    end
    M --> F{{Fusion<br/>risk = max of both<br/>confidence combined}}
    R --> F
    F --> T{Confidence tier}
    T -->|≥ 0.85| T1[Tier 1<br/>send now]
    T -->|0.55 – 0.85| T2[Tier 2<br/>countdown → auto-send<br/>human can only CANCEL]
    T -->|below 0.55| T3[Tier 3<br/>dashboard only<br/>human must approve]
    T1 & T2 & T3 --> D[Dashboard + CAP alert → SACHET]
```

**Why two branches instead of one model?** The ML model sees 34 satellite features. The ground sensors produce 5 readings. There is no honest mapping between the two, so DRISHTI fuses their **verdicts**, not their inputs:

| Situation | Confidence | Why |
|---|---|---|
| Both branches elevated | Combined as independent evidence (cap 0.95) | Corroboration can promote an alert to Tier 1 |
| Satellite model only | −0.10 | A satellite-only call **never** fires fully automatically |
| Ground only, but the model had fresh data and disagreed | −0.10 | Fresh satellite data that sees nothing counts as evidence against |
| Ground only, driven by slope, water level or melt | **No penalty** | The rainfall model cannot see these, so its silence is not evidence. This rule is pinned by a test. |

**Safety rules in the dispatcher**
- A **Critical** assessment never sits in Tier 3. It is bumped to Tier 2, so someone has to actively kill it.
- The veto window is `min(90 s, 20% of lead time)`. If that comes out under 30 s, the window is skipped and the alert fires immediately, so the countdown never eats the lead time it exists to protect.
- A veto that arrives too late returns "already sent" instead of an error, so the operator is not misled.

<details>
<summary><b>Full system design (module-level)</b></summary>
<br>
<img src="assets/readme/system-design.jpg" alt="Module-level system design: ingestion and state, risk intelligence, alert operations and the operations UI" width="100%">
</details>

---

## Model results

<img src="assets/readme/model-operating-points.png" alt="Recall versus false-alarm rate curve with the WARNING and WATCH operating points marked" width="85%">

| Tier | Threshold | Recall | Precision | False-alarm rate* |
|---|---:|---:|---:|---:|
| **WARNING** | 0.93 | 69.7% | 92.1% | 14.4% |
| **WATCH** | 0.22 | 93.2% | 87.0% | 29.8% |

Pooled **PR-AUC 0.931**, against a flood base rate near 0.05%.
<sub>*False-alarm rate is measured on a **permanently reserved near-miss holdout**: the heaviest-rain days that did **not** flood, which are the hardest realistic negatives. Validation is leave-one-storm-out CV across 9 storm groups from 2019–2022 in Uttarakhand.</sub>

**Why two thresholds:** a single cut-off would give no signal at all for about 30% of real events. WATCH and WARNING mirror the US NWS Flash Flood Watch/Warning pattern. Numbers are read live from [`final_model_results.json`](drishti/ml/artifacts/final_model_results.json) and exposed at `GET /model/info`, so what runs is what is reported.

---

## How DRISHTI compares

| Capability | FFGS (India) | Google Flood Hub | US NWS-FLASH | **DRISHTI** |
|---|:---:|:---:|:---:|:---:|
| Rainfall-triggered floods | ✅ | ✅ | ✅ | ✅ |
| Validated on historical events | ✅ | ✅ | ✅ | ✅ |
| ML-driven risk scoring | ❌ | ✅ | ❌ | ✅ |
| High-resolution input | ❌ | ❌ | ✅ | ✅ |
| **Non-rainfall geohazards** (slope, glacier, GLOF) | ❌ | ❌ | ❌ | ✅ |
| **Ward-level alert + SACHET dispatch** | ❌ | ❌ | ❌ | ✅ |

DRISHTI **extends FFGS rather than replacing it**. FFGS guidance is one of its inputs, and alerts are rendered as CAP 1.2 messages for India's SACHET cell-broadcast network, which reaches basic phones without internet.

---

## What's built vs. designed

We state our scope up front so it doesn't come as a surprise.

| Component | Status |
|---|---|
| Rainfall ML model (XGBoost, 34 features) | ✅ Trained and served by the API |
| Ground-sensor rule engine (5 signals, incl. geohazard) | ✅ Working |
| Fusion, confidence scoring, lead-time projection | ✅ Working |
| Tiered dispatch, veto countdowns, retries, audit log | ✅ Working (channels log instead of sending SMS) |
| Authority dashboard (map, ward detail, sensor health, alerts) | ✅ Deployed on Vercel |
| CAP 1.2 alert export (`/alerts/{id}/cap`) | ✅ Working |
| Geohazard **ML** model (InSAR, seismic, glacier inventory) | 🟡 Designed; the rule branch covers it today |
| SACHET live integration, LoRaWAN sensor mesh | 🟡 Designed |

Known limitations, including in-memory state, no authentication on veto yet, coarse slope data, and why confidence is population-level, are listed in full in the **[engineering README](drishti/README.md#known-limitations)**.

---

## Run it locally

```bash
# Backend: FastAPI, model loads automatically
cd drishti/backend
pip install -r requirements.txt
python demo_end_to_end.py                  # six scenarios end-to-end, no server
python tests/test_pipeline_regressions.py  # 25 regression tests
uvicorn main:app --port 8000

# Frontend: Next.js dashboard
cd drishti/frontend
npm install
NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev   # omit the variable for simulator mode
```

Then open `http://localhost:3000/demo-controls.html` and trigger a scenario. Deployment (Render for the backend, Vercel for the frontend) is covered in [`docs/DEPLOYMENT.md`](drishti/docs/DEPLOYMENT.md).

<details>
<summary><b>API surface</b></summary>

```
POST /ingest                      ground sensor readings
POST /wards/{id}/meteo            satellite feature vector for a ward
GET  /wards                       all wards with current risk
GET  /wards/{id}/risk             full assessment: both branches, lead time, tier
GET  /dashboard/sensor-health     per-sensor reporting / silent / missing
GET  /model/info                  loaded model, operating points, caveats
GET  /alerts/active | /history    alert queue and audit log
GET  /alerts/{id}/cap             CAP 1.2 XML for SACHET
POST /alerts/{id}/veto|approve|dismiss
WS   /ws/alerts                   live alert lifecycle events
```
</details>

---

## Repository map

```
drishti/
├── backend/    FastAPI: ingestion, rule engine, model service, fusion, alert dispatch, demo scenarios, tests
├── frontend/   Next.js authority dashboard + /demo-controls.html
├── ml/         Training script, two-tier scorer, trained booster, CV results
├── docs/       Project briefing, deployment guide, model feature contract
├── DEMO_SCRIPT.md   3–4 minute judge walkthrough
└── README.md        Full engineering reference (every file, every design decision)
```

---

## Grounding

**Case studies:** Chamoli (Feb 2021), Sikkim GLOF (Oct 2023), Himachal (Jul 2023), Trishuli glacier collapse, Nepal (2026), Blatten, Switzerland (2025).
**Policy alignment:** NDMA National Disaster Management Plan 2019 · National GLOF Risk Mitigation Programme · SACHET / CAP.
**Data:** GPM IMERG · ERA5 · SRTM · HydroSHEDS / HydroRIVERS · NASA SMAP · Sentinel-1 InSAR (via GEE) · USGS seismic · ICIMOD / RGI glacier inventory · Bhukosh landslide inventory.

<div align="center">
<sub>Built by <b>ThunderBolts!</b> for Smart India Hackathon 2026</sub>
</div>
