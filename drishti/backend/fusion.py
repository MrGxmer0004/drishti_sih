"""
fusion.py — combines the two branches into the single RiskAssessment that the
alert dispatcher consumes.

    Branch A  meteo-hydro (rainfall-triggered)
              GPM IMERG + ERA5 + SRTM + HydroSHEDS, 0.1° grid
              -> trained XGBoost model, via model_service.py

    Branch B  ground sensors (incl. the geohazard signals FFGS misses)
              rainfall / soil moisture / water level / slope tilt / temperature
              -> rule-based engine, via risk_engine.py

`assess_ward_risk_fused()` has the SAME signature contract as
`risk_engine.assess_ward_risk()` and returns the same `RiskAssessment`, so
`alert_dispatch.py` and `main.py` are unchanged by its existence. If the model
is unavailable or has no fresh features for a ward, this collapses exactly to
the rule-based result.

HOW THE TWO ARE COMBINED

Risk level: the maximum of the two. Neither branch can see what the other
sees, so a signal from either is a real signal. A model WARNING on a ward whose
sensors are quiet is still a warning — that ward may simply have no sensors.

Confidence: this is the number `alert_dispatch.classify_tier` routes on, so it
decides whether a human gets a veto window. Four cases:

  BOTH branches elevated
      Independent corroboration, so combine as independent evidence:
          conf = 1 - (1 - conf_rule)(1 - conf_model),  capped at CONF_CAP.
      The independence assumption is stronger here than it usually is —
      satellite reanalysis and a ground rain gauge really are separate
      instruments — but it is still an assumption, hence the cap.

  MODEL ONLY
      conf_model - MODEL_ONLY_PENALTY. The penalty exists because the model's
      confidence is a population-level 1-FAR figure with no ground
      confirmation behind this specific ward. In practice it moves a
      model-only WARNING out of Tier 1 and into Tier 2, so a satellite-only
      call always gets a human veto window rather than firing silently.

  RULE ONLY, and the model had fresh features and disagreed
      conf_rule - MODEL_DISAGREEMENT_PENALTY. Fresh satellite data that does
      not see a rainfall flood signature is genuine evidence against.

  RULE ONLY, and the disagreement is one the model CANNOT have an opinion on
      No penalty. The model has no water-level, slope-tilt or seismic inputs;
      a glacier collapse or a slope failure is invisible to it by construction.
      Penalising the geohazard branch for the rainfall model's silence would
      suppress precisely the events DRISHTI exists to catch. See
      `GEOHAZARD_SIGNALS` below.

EVERY CONSTANT BELOW IS A GUESS, in the same sense as the thresholds in
risk_engine.py — chosen so the tier boundaries land somewhere defensible, not
derived from data. They need re-deriving against a validation set where both
branches were live at once. That dataset does not exist yet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ingestion import WardBuffer
from meteo_store import MeteoFeatureStore
from model_service import MeteoModelService
from risk_engine import RiskAssessment, SignalResult, assess_ward_risk, estimate_lead_time
from schemas import RiskLevel, risk_rank

# --- Fusion constants (tunable guesses — see module docstring) ------------

CONF_CAP = 0.95                  # ceiling on combined-evidence confidence
MODEL_ONLY_PENALTY = 0.10        # satellite call with no ground corroboration
MODEL_DISAGREEMENT_PENALTY = 0.10  # fresh model data that sees nothing
CONF_FLOOR = 0.05

# Rule-engine signals the meteo model structurally cannot observe. When one of
# these is the driver, model silence is not evidence of absence.
GEOHAZARD_SIGNALS = {"water_level", "slope_tilt", "glacier_melt"}


def _combine_independent(a: float, b: float) -> float:
    return min(CONF_CAP, 1.0 - (1.0 - a) * (1.0 - b))


def assess_ward_risk_fused(
    ward_id: str,
    buffer: WardBuffer,
    meteo_store: Optional[MeteoFeatureStore] = None,
    model_service: Optional[MeteoModelService] = None,
    now: Optional[datetime] = None,
) -> RiskAssessment:
    """Rule-based ground-sensor assessment, fused with the meteo model verdict.

    Falls back cleanly to the rule-only assessment whenever the model is
    disabled, unloadable, or has no fresh feature vector for this ward.
    """
    now = now or datetime.now(timezone.utc)
    assessment = assess_ward_risk(ward_id, buffer, now=now)

    if meteo_store is None or model_service is None or not model_service.available:
        assessment.model_branch = {
            "available": False,
            "reason": (
                model_service.reason_unavailable
                if model_service is not None
                else "model service not wired in"
            ),
        }
        return assessment

    record = meteo_store.get(ward_id)
    if record is None:
        assessment.model_branch = {
            "available": False,
            "reason": "no meteo feature vector posted for this ward",
        }
        assessment.confidence_factors.append(
            "meteo model: no feature vector for this ward — ground sensors only"
        )
        return assessment

    verdict = model_service.score_features(
        record.features, observed_at=record.observed_at, now=now
    )

    assessment.model_branch = {
        "available": verdict.available,
        "reason": verdict.reason_unavailable,
        "tier": verdict.tier,
        "raw_score": verdict.raw_score,
        "threshold": verdict.threshold,
        "false_alarm_rate": verdict.false_alarm_rate,
        "confidence": verdict.confidence,
        "calibrated": verdict.calibrated,
        "features_age_minutes": verdict.features_age_minutes,
        "missing_features": verdict.missing_features,
        "source": record.source,
        "detail": verdict.detail,
    }

    if not verdict.available:
        assessment.confidence_factors.append(
            f"meteo model: unusable ({verdict.reason_unavailable}) — ground sensors only"
        )
        return assessment

    # Surface the model as its own signal row so the dashboard's per-signal
    # breakdown shows it alongside the sensor signals instead of hiding it.
    assessment.signals.append(
        SignalResult(
            name="meteo_model",
            level=verdict.risk_level,
            value=verdict.raw_score,
            threshold=verdict.threshold,
            samples=1,
            available=True,
            detail=verdict.detail or "model score below both thresholds",
        )
    )

    rule_level = assessment.risk_level
    model_level = verdict.risk_level
    rule_elevated = rule_level is not RiskLevel.NORMAL
    model_elevated = model_level is not RiskLevel.NORMAL

    if rule_elevated and model_elevated:
        fused_conf = _combine_independent(assessment.confidence, verdict.confidence)
        assessment.confidence_factors.append(
            f"meteo model agrees ({verdict.tier}, conf {verdict.confidence:.2f}) "
            f"-> combined {fused_conf:+.2f} of independent evidence"
        )
        assessment.reasons.append(
            f"meteo-hydro model: {verdict.tier} — {verdict.detail}"
        )

    elif model_elevated:
        fused_conf = verdict.confidence - MODEL_ONLY_PENALTY
        assessment.confidence_factors.append(
            f"meteo model alone ({verdict.tier}, conf {verdict.confidence:.2f}); "
            f"no ground corroboration {-MODEL_ONLY_PENALTY:+.2f}"
        )
        assessment.reasons.append(
            f"meteo-hydro model: {verdict.tier} with no corroborating ground sensor "
            f"signal — {verdict.detail}"
        )

    elif rule_elevated:
        driver = _driving_signal(assessment)
        if driver is not None and driver.name in GEOHAZARD_SIGNALS:
            fused_conf = assessment.confidence
            assessment.confidence_factors.append(
                f"meteo model sees nothing, but '{driver.name}' is outside what a "
                f"rainfall model can observe — no penalty applied"
            )
        else:
            fused_conf = assessment.confidence - MODEL_DISAGREEMENT_PENALTY
            assessment.confidence_factors.append(
                f"fresh meteo data shows no rainfall flood signature "
                f"{-MODEL_DISAGREEMENT_PENALTY:+.2f}"
            )
    else:
        fused_conf = assessment.confidence  # both quiet; nothing to fuse

    fused_level = max((rule_level, model_level), key=risk_rank)
    if risk_rank(fused_level) > risk_rank(rule_level):
        # The ML branch raised the level; the ground sensors have no trend for
        # this higher level, so fall back to the terrain baseline and say so
        # rather than carrying a projection that was fitted at a lower level.
        assessment.estimated_lead_time_minutes = estimate_lead_time(ward_id, fused_level)
        assessment.lead_time_band = None
        assessment.lead_time_driver = "meteo_model"
        assessment.lead_time_basis = "terrain_baseline"
        assessment.impact_imminent = False

    assessment.risk_level = fused_level
    assessment.confidence = round(max(CONF_FLOOR, min(CONF_CAP, fused_conf)), 3)
    return assessment


def _driving_signal(assessment: RiskAssessment) -> Optional[SignalResult]:
    """Highest-severity elevated sensor signal, excluding the model row itself."""
    elevated = [
        s
        for s in assessment.signals
        if s.level is not RiskLevel.NORMAL and s.name != "meteo_model"
    ]
    if not elevated:
        return None
    return max(elevated, key=lambda s: risk_rank(s.level))
