# ML — meteo-hydro branch (Branch A)

The trained rainfall-triggered flash flood model, its artifacts, and the
scorer the API loads.

## Files

| File | What it is |
|---|---|
| `train_flash_flood_model.py` | The training run that produced the current model. Its docstring is the record of what was tried and rejected — read it before proposing an improvement that has already been tested. |
| `inference_two_tier_scorer.py` | `FloodScorer`: loads the booster, applies the two thresholds, returns `WARNING` / `WATCH` / `NONE`. Also `fit_calibrator()` for optional isotonic calibration. The backend imports this file directly rather than keeping its own copy. |
| `generate_feature_contract.py` | Regenerates the range table in `docs/MODEL_FEATURE_CONTRACT.md` from the model artifact. Run after every retrain. |
| `artifacts/flash_flood_model_FINAL_v2.json` | The booster. ~370 KB, committed on purpose — the API needs it at runtime. |
| `artifacts/final_model_results.json` | CV results, the full threshold sweep, both operating points, the feature list. The backend reads its thresholds and false-alarm rates from here, so retraining updates the API's behaviour without a code change. |
| `artifacts/oof_predictions.npy` / `oof_labels.npy` | Out-of-fold scores and labels. Only used to fit calibration. |

Training data (`model_dataset_v5.parquet` and friends) is **not** in this repo.
It is multi-GB and lives with whoever runs training. `.gitignore` excludes
`*.parquet` so it cannot be committed by accident.

## The two operating points

Both come from the same threshold sweep, at no extra compute:

| Tier | Threshold | Recall | Precision | False alarm rate |
|---|---:|---:|---:|---:|
| WARNING | 0.93 | 69.7% | 92.1% | 14.4% |
| WATCH | 0.22 | 93.2% | 87.0% | 29.8% |

Pooled PR-AUC is 0.931, against a base rate near 0.05%.

A single threshold would mean roughly 30% of real events produce no signal at
all. Two tiers is the standard operational pattern (compare the NWS Flash
Flood Watch versus Warning) and was the highest-value change available without
retraining or new data.

## How the API turns a score into a confidence

`alert_dispatch.classify_tier` routes on confidence, so this number decides
whether a human gets a veto window. `backend/model_service.py` computes it as:

```
confidence = 1 - false_alarm_rate_at_that_threshold
```

giving 0.856 for WARNING and 0.702 for WATCH, read live from
`final_model_results.json`.

**Why not the precision column, which looks like the more natural choice.**
Precision is prevalence-dependent, and it was computed on a re-balanced
negative sample rather than at the true ~0.05% base rate. Reporting 92.1% as
"how often a warning is right in the field" would overstate it, possibly
badly. The false-alarm rate is measured on the permanently reserved near-miss
holdout — top-rainfall days that never flooded, the hardest realistic
negatives available — and is the more transportable of the two.

**It is still a population-level number.** Every WARNING carries the same
confidence. Nothing here distinguishes a score of 0.94 from one of 0.999.
Per-instance confidence needs calibration.

## Calibration — off by default, and why

`DRISHTI_CALIBRATE=1` fits isotonic regression from the OOF arrays at startup,
which makes the displayed probability mean something rather than being a
ranking score.

It is off because **the deployed thresholds were selected on raw scores.**
Isotonic regression is monotonic, so the ordering survives, but 0.93 on a
calibrated scale is not the same operating point as 0.93 on a raw one. Turning
calibration on without re-running the sweep silently changes which events fire
and invalidates the recall and FAR numbers above.

If you want calibrated probabilities, do it in this order:

1. Fit the calibrator on the OOF arrays.
2. Re-run the threshold sweep against calibrated scores.
3. Pick new thresholds at the same FAR tolerance.
4. Write those into `final_model_results.json`.
5. Only then set `DRISHTI_CALIBRATE=1`.

Also add `scikit-learn` to `backend/requirements.txt` — it is commented out
there precisely because calibration is off.

## Scope — what this model does not do

It covers **rainfall-triggered floods only**. It has no water-level, slope-tilt
or seismic input, so glacier collapse, moraine failure and rock-ice avalanche
are invisible to it by construction. Three of the four recent Himalayan
disasters in the briefing fall into that blind spot.

That is not a defect in this model; it is the reason the geohazard branch
exists. `backend/fusion.py` encodes the consequence: when the driving signal is
one this model cannot observe, its silence carries no weight.

## Two honest caveats to keep in the pitch

**Partial circularity.** The label is defined by a rule over `rain_24h`, and
the model receives `rain_24h` as a feature. Strong scores partly reflect the
model recovering the labelling rule. Frame results as "identifies heavy-rain
flood signatures", not "predicts floods from scratch". Ground-truth causal
validation against actual flooded locations is a stated next step, not a done
thing.

**Slope is still coarse.** Slope was recomputed at 0.1° (~11 km) baseline,
giving a median near 1.4° against a physically real ~28°. The model's own split
thresholds show it: `slope_deg` splits stop at 8.07. Recovering the raw SRTM
`.hgt` tiles remains the highest-value open task — and note that when it is
done, **this model must be retrained**, because real slopes would sit off the
edge of every slope split it has.
