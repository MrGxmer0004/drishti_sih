"""
model_service.py — the meteo-hydro (Branch A) ML model, wrapped for the API.

WHAT THIS IS
    A thin service layer over `ml/inference_two_tier_scorer.py`. It loads the
    trained XGBoost booster once at startup, scores a satellite/reanalysis
    feature vector, and returns a two-tier verdict (WARNING / WATCH / NONE)
    plus a confidence number the alert tiering can use.

WHAT THIS IS NOT
    It is NOT a scorer for the ground IoT sensors. The model's 34 features are
    GPM/ERA5/SRTM/HydroSHEDS quantities on a 0.1° grid. Rainfall, soil
    moisture, water level, slope tilt and temperature readings from a LoRaWAN
    node are a different measurement space entirely. There is no defensible
    mapping from five ground sensors to 34 satellite features, so this module
    does not attempt one. `risk_engine.py` handles the ground sensors;
    `fusion.py` combines the two verdicts without merging their inputs.
    (Project briefing §10 — do not conflate the branches.)

WHERE THE CONFIDENCE NUMBER COMES FROM — read this before trusting it
    `alert_dispatch.classify_tier` routes on confidence, so this number decides
    whether a human gets a say. It is derived as:

        confidence = 1 - false_alarm_rate_at_that_threshold

    read straight out of `final_model_results.json`, so it tracks the model
    when it is retrained. The false-alarm rate is measured on the permanently
    reserved near-miss holdout (top-rainfall days that never flooded) — the
    hardest realistic negatives available, and the check that surfaced every
    other bug in this project's history.

    Two honest caveats:

    1. This is a POPULATION-level number, not a per-instance calibrated
       probability. Every WARNING gets the same confidence. Per-instance
       confidence needs isotonic calibration (see ml/README.md) AND a
       re-verified threshold sweep against calibrated scores.

    2. It is not P(real flood | alert). Precision from the sweep table
       (0.921 at the WARNING threshold) looks like that number but is not:
       it was computed on a re-balanced negative sample, not at the true
       ~0.05% prevalence, so it overstates deployment precision. 1 - FAR is
       the more conservative and more transportable of the two, which is why
       it is what this module uses.

FAILURE MODE
    If xgboost is missing or the artifacts are absent, this module logs a
    warning and reports `available = False`. The API still starts and the
    rule-based ground-sensor branch still works. A missing model degrades the
    system; it does not stop it.
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import config
from schemas import RiskLevel

logger = logging.getLogger("drishti.model")

# `ml/` is a sibling of `backend/`, not an installed package. Adding it to the
# path keeps ONE copy of the scorer (the one the ML member maintains) rather
# than a backend fork that silently drifts from it.
if str(config.ML_DIR) not in sys.path:
    sys.path.insert(0, str(config.ML_DIR))


# Fraction of the feature vector that may be missing before scoring is refused.
# NaNs are legitimate — XGBoost splits on missingness natively and ~0.1% of
# ERA5 rows genuinely have none — but a vector that is mostly holes is an
# upstream plumbing failure, not a weather observation.
MAX_MISSING_FRACTION = 0.5

# Map the scorer's tier onto the shared RiskLevel vocabulary.
_TIER_TO_RISK = {
    "WARNING": RiskLevel.WARNING,
    "WATCH": RiskLevel.WATCH,
    "NONE": RiskLevel.NORMAL,
}


@dataclass
class ModelAssessment:
    """One model verdict on one ward's meteo feature vector."""

    available: bool                       # was the model able to score at all?
    tier: str = "NONE"                    # WARNING | WATCH | NONE
    risk_level: RiskLevel = RiskLevel.NORMAL
    raw_score: Optional[float] = None     # booster output, NOT a calibrated probability
    confidence: float = 0.0               # 1 - FAR at the firing threshold
    threshold: Optional[float] = None     # the threshold this verdict cleared
    false_alarm_rate: Optional[float] = None
    calibrated: bool = False              # was isotonic calibration applied?
    missing_features: List[str] = field(default_factory=list)
    features_age_minutes: Optional[float] = None
    detail: str = ""
    reason_unavailable: str = ""


class MeteoModelService:
    """Loads the booster once and scores feature vectors against it."""

    def __init__(self) -> None:
        self.available = False
        self.reason_unavailable = "not loaded"
        self.scorer = None
        self.features: List[str] = []
        self.calibrated = False
        self._confidence_by_tier: Dict[str, float] = {}
        self._far_by_tier: Dict[str, Optional[float]] = {}
        self._threshold_by_tier: Dict[str, Optional[float]] = {}
        self.pooled_pr_auc: Optional[float] = None

    # --- lifecycle -------------------------------------------------------

    def load(self) -> None:
        """Called once from the FastAPI lifespan hook. Never raises."""
        if not config.ENABLE_MODEL:
            self.reason_unavailable = "disabled via DRISHTI_ENABLE_MODEL"
            logger.warning("meteo model disabled by configuration — rule engine only")
            return

        try:
            import json

            from inference_two_tier_scorer import FloodScorer, fit_calibrator
        except ImportError as e:  # xgboost / numpy / the scorer module itself
            self.reason_unavailable = f"import failed: {e}"
            logger.warning("meteo model unavailable (%s) — rule engine only", e)
            return

        if not config.MODEL_PATH.exists() or not config.MODEL_RESULTS_PATH.exists():
            self.reason_unavailable = (
                f"artifacts not found at {config.MODEL_PATH} / {config.MODEL_RESULTS_PATH}"
            )
            logger.warning("meteo model artifacts missing — rule engine only")
            return

        calibrator = None
        if config.ENABLE_CALIBRATION:
            try:
                calibrator = fit_calibrator(config.OOF_PRED_PATH, config.OOF_LABELS_PATH)
                self.calibrated = True
                logger.warning(
                    "isotonic calibration is ON. The deployed thresholds were selected on "
                    "RAW scores — re-verify the sweep against calibrated scores before "
                    "trusting the recall/FAR figures."
                )
            except Exception as e:
                logger.warning("calibration failed (%s) — continuing with raw scores", e)

        try:
            self.scorer = FloodScorer(
                config.MODEL_PATH, config.MODEL_RESULTS_PATH, calibrator=calibrator
            )
        except Exception as e:
            self.reason_unavailable = f"scorer init failed: {e}"
            logger.warning("meteo model failed to load (%s) — rule engine only", e)
            return

        with open(config.MODEL_RESULTS_PATH) as f:
            results = json.load(f)

        self.features = list(self.scorer.features)
        self.pooled_pr_auc = results.get("pooled_pr_auc")

        for tier_name, key in (("WARNING", "chosen_threshold"), ("WATCH", "watch_threshold")):
            row = results.get(key) or {}
            far = row.get("false_alarm_rate")
            self._far_by_tier[tier_name] = far
            self._threshold_by_tier[tier_name] = row.get("threshold")
            # 1 - FAR on the reserved near-miss holdout. See module docstring
            # for why this and not the sweep's precision column.
            self._confidence_by_tier[tier_name] = round(1.0 - far, 3) if far is not None else 0.5

        self.available = True
        self.reason_unavailable = ""
        logger.info(
            "meteo model loaded: %d features, WARNING thr=%s (conf %.3f), WATCH thr=%s (conf %.3f)",
            len(self.features),
            self._threshold_by_tier.get("WARNING"),
            self._confidence_by_tier.get("WARNING", 0.0),
            self._threshold_by_tier.get("WATCH"),
            self._confidence_by_tier.get("WATCH", 0.0),
        )

    # --- introspection ---------------------------------------------------

    def info(self) -> dict:
        """Everything the dashboard (or a judge) needs to see about what is
        actually running. Deliberately includes the caveats."""
        return {
            "available": self.available,
            "reason_unavailable": self.reason_unavailable,
            "branch": "meteo-hydro (rainfall-triggered) — Branch A",
            "model_path": str(config.MODEL_PATH),
            "n_features": len(self.features),
            "features": self.features,
            "pooled_pr_auc": self.pooled_pr_auc,
            "thresholds": {
                tier: {
                    "threshold": self._threshold_by_tier.get(tier),
                    "false_alarm_rate": self._far_by_tier.get(tier),
                    "confidence_used": self._confidence_by_tier.get(tier),
                }
                for tier in ("WARNING", "WATCH")
            },
            "calibrated": self.calibrated,
            "confidence_basis": "1 - false_alarm_rate on the reserved near-miss holdout",
            "caveats": [
                "Population-level confidence: every WARNING carries the same number.",
                "Raw booster scores are not calibrated probabilities.",
                "Scores partly reflect the labelling rule (rain_24h is both label input "
                "and feature) — read as 'heavy-rain flood signature', not causal prediction.",
                "Covers rainfall-triggered floods only. Glacier/moraine/slope-collapse "
                "events are structurally invisible to this model.",
            ],
        }

    # --- scoring ---------------------------------------------------------

    def score_features(
        self,
        features: Dict[str, float],
        observed_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> ModelAssessment:
        """Score one ward's meteo feature vector.

        `features` is a name -> value mapping. Names not in the trained feature
        list are ignored; absent names become NaN, which XGBoost handles
        natively. Both are reported back so a broken upstream job is visible
        rather than silently producing a confident NONE.
        """
        if not self.available or self.scorer is None:
            return ModelAssessment(available=False, reason_unavailable=self.reason_unavailable)

        now = now or datetime.now(timezone.utc)
        age_minutes: Optional[float] = None
        if observed_at is not None:
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            age_minutes = (now - observed_at).total_seconds() / 60.0
            if age_minutes > config.METEO_MAX_AGE_MINUTES:
                return ModelAssessment(
                    available=False,
                    features_age_minutes=round(age_minutes, 1),
                    reason_unavailable=(
                        f"feature vector is {age_minutes:.0f}min old "
                        f"(limit {config.METEO_MAX_AGE_MINUTES:.0f}min)"
                    ),
                )

        missing = [f for f in self.features if f not in features or features[f] is None]
        if len(missing) > MAX_MISSING_FRACTION * len(self.features):
            return ModelAssessment(
                available=False,
                missing_features=missing,
                features_age_minutes=round(age_minutes, 1) if age_minutes is not None else None,
                reason_unavailable=(
                    f"{len(missing)}/{len(self.features)} features missing — "
                    f"refusing to score a mostly-empty vector"
                ),
            )

        row = [
            float(features[f]) if (f in features and features[f] is not None) else math.nan
            for f in self.features
        ]

        try:
            tier, raw = self.scorer.score(row)
        except Exception as e:
            logger.exception("model scoring failed")
            return ModelAssessment(available=False, reason_unavailable=f"scoring failed: {e}")

        return ModelAssessment(
            available=True,
            tier=tier,
            risk_level=_TIER_TO_RISK.get(tier, RiskLevel.NORMAL),
            raw_score=round(float(raw), 4),
            confidence=self._confidence_by_tier.get(tier, 0.0),
            threshold=self._threshold_by_tier.get(tier),
            false_alarm_rate=self._far_by_tier.get(tier),
            calibrated=self.calibrated,
            missing_features=missing,
            features_age_minutes=round(age_minutes, 1) if age_minutes is not None else None,
            detail=(
                f"model score {raw:.4f} -> {tier}"
                + (
                    f" (>= {self._threshold_by_tier.get(tier)}, "
                    f"FAR {self._far_by_tier.get(tier):.1%} on near-miss holdout)"
                    if tier != "NONE" and self._far_by_tier.get(tier) is not None
                    else ""
                )
            ),
        )

    def score_rows(self, rows: Sequence[Sequence[float]]):
        """Batch passthrough for offline replay/backtesting. Returns
        (tiers, raw_scores) exactly as the scorer produces them."""
        if not self.available or self.scorer is None:
            raise RuntimeError(f"model unavailable: {self.reason_unavailable}")
        return self.scorer.score_batch(rows)


# One process-wide instance; `main.py` calls `.load()` in the lifespan hook.
model_service = MeteoModelService()
