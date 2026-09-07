"""
config.py — every environment-dependent value in the backend, in one place.

Nothing here is secret. It exists so that deploying to a container platform
means setting env vars rather than editing source.

Env vars:
  DRISHTI_MODEL_PATH        path to flash_flood_model_FINAL_v2.json
  DRISHTI_MODEL_RESULTS     path to final_model_results.json (holds the thresholds)
  DRISHTI_ENABLE_MODEL      "1"/"true" to load the XGBoost branch (default: on)
  DRISHTI_CALIBRATE         "1"/"true" to fit isotonic calibration from the OOF arrays
  DRISHTI_OOF_PRED          path to oof_predictions.npy   (only used if DRISHTI_CALIBRATE)
  DRISHTI_OOF_LABELS        path to oof_labels.npy        (only used if DRISHTI_CALIBRATE)
  DRISHTI_METEO_MAX_AGE_MIN how old a meteo feature vector may be and still count
  DRISHTI_CORS_ORIGINS      comma-separated allowed origins (default: "*")
"""

from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent
ML_DIR = REPO_ROOT / "ml"
ARTIFACT_DIR = ML_DIR / "artifacts"


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


MODEL_PATH = Path(os.getenv("DRISHTI_MODEL_PATH", ARTIFACT_DIR / "flash_flood_model_FINAL_v2.json"))
MODEL_RESULTS_PATH = Path(os.getenv("DRISHTI_MODEL_RESULTS", ARTIFACT_DIR / "final_model_results.json"))
OOF_PRED_PATH = Path(os.getenv("DRISHTI_OOF_PRED", ARTIFACT_DIR / "oof_predictions.npy"))
OOF_LABELS_PATH = Path(os.getenv("DRISHTI_OOF_LABELS", ARTIFACT_DIR / "oof_labels.npy"))

ENABLE_MODEL = _flag("DRISHTI_ENABLE_MODEL", True)

# Calibration is OFF by default on purpose. Isotonic regression makes the
# displayed probability meaningful, but the deployed thresholds (0.93 / 0.22)
# were selected on RAW scores. Turning this on without re-running the sweep
# against calibrated scores changes which events fire. See ml/README.md.
ENABLE_CALIBRATION = _flag("DRISHTI_CALIBRATE", False)

# A satellite/reanalysis feature vector older than this is ignored by the
# fusion step. GPM IMERG runs in 30-minute bins; three hours is two bins of
# slack for a late upstream job, after which "no data" is the honest answer.
METEO_MAX_AGE_MINUTES = float(os.getenv("DRISHTI_METEO_MAX_AGE_MIN", "180"))

CORS_ORIGINS = [o.strip() for o in os.getenv("DRISHTI_CORS_ORIGINS", "*").split(",") if o.strip()]

# A sensor that hasn't reported inside this window shows as silent in the
# dashboard's sensor-health panel.
SENSOR_SILENT_AFTER_MINUTES = float(os.getenv("DRISHTI_SENSOR_SILENT_MIN", "15"))
