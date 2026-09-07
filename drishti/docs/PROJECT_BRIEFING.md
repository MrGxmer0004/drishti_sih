# DRISHTI — Complete Project Briefing

You are being brought onto an ongoing Smart India Hackathon (SIH) project called
DRISHTI. This document gives you the full context from scratch — the problem, the
solution, the architecture, the data pipeline, the current status, and the known
open issues. Read it fully before assisting. Where you give advice, prioritize
technical honesty over optimism: this team values direct feedback and has repeatedly
chosen to fix root causes rather than paper over them.

---

## 1. THE PROBLEM

SIH problem statement: **Flash Flood Prediction System for Hilly Regions using
Multi-Source Data** (Software category).

Hilly/Himalayan regions in India face landslides and flash floods that strike with
very little warning. India's existing operational tool is the **Flash Flood Guidance
System (FFGS)**, which does rainfall-driven hydrological modeling at basin/regional
scale and stops once a forecaster issues a general warning. It has two structural
gaps:
1. It produces coarse, non-localized guidance — not village/ward-level, actionable
   warnings with lead time.
2. It is fundamentally rainfall-based, so it **cannot detect floods triggered by
   anything other than rainfall** — e.g. glacier collapse, moraine failure, or
   rock-ice avalanche.

## 2. THE POSITIONING (important — this frames everything)

DRISHTI does **not replace FFGS — it extends it.** FFGS output is one input among
several. DRISHTI adds the missing "last mile": hyperlocal (ward-level) risk scoring,
lead-time estimation, and actionable tiered alerts — PLUS a detection capability FFGS
and every other major flood-forecasting system worldwide currently lacks:
non-rainfall, geohazard-triggered flood detection.

Name: **DRISHTI** = Disaster Risk Identification System for Hyperlocal Threat
Intelligence. (Sanskrit/Hindi "drishti" = vision/foresight — the metaphor is "seeing
the threats FFGS cannot.")

## 3. WHY THE GEOHAZARD BRANCH MATTERS (the core differentiator)

Research into four recent Himalayan-belt flash flood disasters:
- **Nepal-Tibet, Trishuli River, Aug 2026**: glacier collapse formed a landslide dam
  that later burst; 600+ dead, 1,900+ missing. No rainfall trigger.
- **Sikkim GLOF, Oct 2023**: permafrost/moraine collapse into South Lhonak Lake →
  glacial lake outburst flood down the Teesta.
- **Chamoli, Uttarakhand, Feb 2021**: rock-ice avalanche off a glacierized peak, in
  winter, no rainfall.
- **Himachal Pradesh, Jul 2023**: classic monsoon cloudburst — the ONE category
  standard rainfall systems ARE built for.

**Finding: 3 of 4 major recent Himalayan flash flood disasters were NOT
rainfall-triggered.** Global alternatives to FFGS (Google Flood Hub, US NWS-FLASH,
France's AIGA, Germany's SINFONY, Italy's Flood-PROOFS) were also checked — none
detect glacier/slope collapse either. This is a genuine global blind spot, which is
DRISHTI's defensible novelty.

Proof it's detectable: the 2025 Blatten, Switzerland disaster — authorities tracked a
glacier accelerating (~10 m/day) via deformation monitoring and evacuated in time (1
death, vs 200+ at Chamoli where no such monitoring existed).

## 4. SYSTEM ARCHITECTURE (top to bottom)

Two parallel data-source branches feed one fused pipeline:

**Branch A — Meteo-Hydro** (rainfall-triggered floods):
- Rainfall (IMD / Open-Meteo), river discharge (India-WRIS/CWC), soil moisture (NASA
  SMAP), FFGS output (external govt system, integration point).

**Branch B — Geohazard** (non-rainfall floods — the differentiator):
- Satellite SAR/InSAR deformation (Sentinel-1 via Google Earth Engine), seismic
  mass-movement detection (USGS), glacier/moraine inventory (ICIMOD/RGI), historical
  landslide inventory (Bhukosh/Bhuvan). EOS-05 (ISRO geostationary imaging satellite,
  launched Sept 2026, 42m resolution, ~5-10 min revisit) is a designed future optical
  input — not yet operational, framed as an integration point, not a live feed.
- IoT (optional/pluggable): local water-level, slope-tilt, and low-cost seismic
  sensors via LoRaWAN mesh + solar power — framed as redundant local sensing (answers
  the Nepal case where official gauges were destroyed mid-event).

Both branches flow into:
- **Ingestion & Normalization Layer** → validates, normalizes units, deduplicates,
  flags anomalies, stores time-series per ward.
- **Local Risk Engine** → ML fusion, outputs ward-level risk score, risk level,
  confidence, estimated lead time.
- **Alert Dispatch (tiered, minimal human-in-loop)**:
    - Tier 1 (high confidence): auto-fire instantly.
    - Tier 2 (medium confidence): auto-fires after a short human VETO countdown —
      a dead-man's switch, NOT an approval gate. Default action is "send," so speed
      doesn't depend on a human being available. This is a key design point (Nepal's
      lead time was single-digit minutes — a human-approval step is too slow).
    - Tier 3 (low confidence): flagged for human review only, no auto-dispatch.
- **SACHET integration**: rather than building its own SMS gateway, DRISHTI emits
  CAP-formatted (Common Alerting Protocol) alerts into SACHET, India's existing
  NDMA/C-DOT national alert platform (19+ languages, cell broadcast). Consistent with
  "extend, don't replace" at the dissemination layer too.
- **Outputs**: Citizen Warning (actionable, localized, with evacuation point) +
  Authority Dashboard (ward risk map, ward detail, sensor health panel, alert log
  with override/veto controls).

## 5. TEAM & TECH

5 members: team lead (product/frontend/backend), a second frontend/backend dev, a
GIS/remote-sensing member, an ML member. Backend is FastAPI + Python; frontend is
Next.js/React/TypeScript/Tailwind (dashboard). Diagrams live in Eraser. Model is
XGBoost.

## 6. BACKEND (built, working prototype)

A FastAPI service ties ingestion → risk assessment → alerting. Key files:
- `schemas.py` — SensorReading/Alert contracts, canonical units, valid ranges.
- `ingestion.py` — validation, unit conversion, dedup, spike-flagging, in-memory
  WardBuffer (prototype store).
- `risk_engine.py` — CURRENTLY a rule-based placeholder with a clean swap point:
  `assess_ward_risk(ward_id, buffer) -> RiskAssessment`. The trained ML model
  replaces the body of this one function; nothing downstream changes.
- `alert_dispatch.py` — builds Alert objects, fans out to sms/push/dashboard stubs.
- `main.py` — endpoints: POST /ingest, GET /wards/{id}/risk, etc.
- `ward_config.py` — ward metadata incl. `glacier_fed` flag and evacuation points.

Known backend TODOs already identified: unbounded `_seen_keys` dedup set (memory
leak), index-based vs time-based risk windows, sync dispatch blocking under real
load, spike threshold breaking at near-zero baselines, and the tiered/veto alert
logic not yet implemented in code (only designed).

## 7. THE ML DATA PIPELINE — THIS IS WHERE MOST WORK HAS GONE

The rainfall branch runs on a satellite/reanalysis pipeline: **GPM_IMERG_V07
rainfall + ERA5 weather + SRTM terrain**, gridded to 0.1° cells over Uttarakhand,
3-hour bins, 2018-2024.

### Bugs found and fixed (in order):
1. **Inverted-label bug**: an early synthetic dataset (`synthetic_train.parquet`)
   contained ZERO real positives — all real rows were negatives, all positives were
   10 jittered copies of already-diluted positive rows. Correlation(rain, label) was
   **-0.38**, so the model learned "no rain → flood." ROOT CAUSE: anchor-and-perturb
   synthetic generation (vary 1-2 vars around a fixed template) makes every untouched
   variable a spurious class shortcut. **Never generate synthetic data this way.**
2. **Label dilution**: the original `flash_flood_label` was applied to every grid
   cell and 3-hour bin across an entire district-day, so only ~1% of "positive" rows
   actually had heavy rain (median 7.6mm). Fixed by relabeling per-cell: a row is
   positive only if rain_24h is in the top 15% of that day AND ≥50mm AND on
   flood-prone terrain. Dropped 133,920 diluted positives to **3,696 real ones** at
   0.0486% prevalence, with median rain 68.9mm (pos) vs 1.4mm (neg).
3. **Terrain bug**: `slope_deg` was miscomputed (np.gradient given 0.1°-cell spacing
   ~11,100m against a native-resolution SRTM array — off by ~370x), producing either
   near-0° or near-90° slopes. `terrain_ruggedness` was literally `slope_deg/90` — an
   exact duplicate. Fixed: slope recomputed at grid scale, ruggedness replaced with a
   real Terrain Ruggedness Index (Riley et al.). process_data.py patched to take
   spacing from the raster's affine transform for future runs.

### Two still-open issues (be honest about these):
- **Slope is still coarse.** The raw SRTM .hgt tiles were unavailable, so slope was
  recomputed at 0.1° (~11km) grid baseline — median ~1.4° vs the physically real
  ~28° (Dimri et al.). An 11km baseline averages away the gorge-scale relief that
  actually drives flash floods, which is why terrain contributes little. **Recovering
  the raw .hgt tiles and re-running is the single highest-value remaining task** — it
  unlocks both real slope AND meaningful hydrological features (see below).
- **Partial circularity.** The label is defined by a rule over rain_24h, and the
  model gets rain_24h as a feature — so strong scores partly reflect the model
  recovering the labeling rule (rain features were ~90% of importance before the
  hard-negative fix). Frame results as "identifies heavy-rain flood signatures," not
  "predicts floods from scratch." Ground-truth causal validation against actual
  flooded locations is a stated next step, not a done thing.

### The hard-negative fix (most recent, worked well):
The model leaned ~90% on rainfall because its training negatives were mostly dry days
— it rarely saw "heavy rain, no flood," so it never learned what else (terrain,
drainage) separates flood from non-flood under heavy rain. Fix: deliberately
oversample "near-miss" negatives (top-5% rainfall days that never flooded). Result:
false-alarm rate on near-miss days dropped 21% → 16.7%, rain's share of decisions
68% → 54%, terrain's share 1.5% → 8.7%. The model started using terrain — the
intended mechanism.

## 8. CURRENT DATA FILES (what's canonical)

- **`model_dataset_v3.parquet`** — THE current training file. 7,606,656 rows, 3,696
  real positives, corrected terrain, plus a `neg_tier` column tagging each non-flood
  row as `near_miss` / `mid` / `easy` for reproducible hard-negative sampling. Train
  on THIS.
- `model_dataset_v2.parquet` — prior version, no neg_tier. Superseded by v3.
- `synthetic_augmentation_optional.parquet` — OPTIONAL supplement only, built by
  independent per-feature inverse-CDF sampling. Never merge into the headline result,
  never put in CV folds; its rows are internally inconsistent by design.
- `excluded_anomalous_events.parquet` — the two excluded event-days (2019-06-21,
  2019-06-02) with reasons.
- DO NOT use `synthetic_train.parquet` — it has the inverted-label bug.

Note: ERA5 columns have NaN in ~0.1% of rows (none in positives). LEAVE AS NaN —
XGBoost handles missingness natively; do not impute (would invent fake weather).

## 9. MODEL TRAINING METHODOLOGY (non-negotiable rules)

- XGBoost with `scale_pos_weight` for the ~0.05% imbalance. Do NOT oversample the
  real data itself (tiered negative sampling handles hard negatives separately).
- **Leave-one-EVENT-out cross-validation, grouped by event-day** — NEVER a random
  row split. Grid cells within one event-day are spatially/temporally correlated; a
  random split leaks and inflates scores. Fold structure groups the Sept-2019 events
  and pools tiny events into train-only.
- **Never report raw accuracy as a headline.** At 0.05% prevalence, "always predict
  no-flood" scores 99.95%. Report precision, recall, F1 on the positive class, and
  PR-AUC. Report precision at 80/85/90% recall so the team can pick the operating
  point for the tiered alert design.
- Current results: pooled PR-AUC ~0.92 (26x over base rate), precision ~0.88/0.87/0.85
  at 80/85/90% recall. A "94% accuracy" figure has been mentioned — treat accuracy
  cautiously per the above; PR-AUC and false-alarm rate are the honest metrics.
- Tiered negative sampling distorts the base rate the model sees, so raw predicted
  probabilities are no longer calibrated. Any deployment threshold MUST be
  recalibrated on a validation set blending event-day + held-out near-miss days
  before its precision is trusted.

## 10. SCOPE BOUNDARY (critical — don't conflate the two branches)

The GPM/ERA5/SRTM pipeline above is the **rainfall-triggered branch ONLY.** It has no
water-level, slope-tilt, or seismic data, so it structurally cannot represent the
geohazard branch — DRISHTI's actual differentiator. The geohazard branch is a
separate model on separate (currently synthetic, ground-sensor-style) data with
different variables. They are two models feeding one fused risk engine, NOT
alternatives. Do not try to merge or reconcile their datasets.

## 11. HOW TO HELP

- Push for honesty over optimism; flag circularity, leakage, and overclaiming.
- The highest-value open technical task is recovering raw SRTM tiles → real slope →
  real hydrological features (distance-to-stream, upstream catchment area, drainage
  density). These are DEM-derived, so they're gated on fixing the DEM first.
- Next priorities: bake the near-miss false-alarm check into the eval pipeline
  permanently; recalibrate the operating threshold on blended validation data; apply
  narrow monotonic constraints (rain features only, not soil/pressure/wind).
- For the pitch: the strongest, most defensible claims are (a) the geohazard branch
  closing a global blind spot, (b) the tiered veto-based alerting for sub-10-minute
  lead times, (c) extending FFGS + SACHET rather than replacing them, and (d) the
  deliberate hard-negative training so the model learns terrain, not just rain.
