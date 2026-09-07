"""
inference_deploy.py -- two-tier scoring for the flash-flood MVP.

WHY THIS EXISTS: the validated model has a hard ceiling of ~70% recall at
the FAR<=15% operating point (see train_flash_flood_FINAL1.py docstring --
every avenue to push that ceiling higher via features/tuning/data volume
has been tried and either rejected or requires new data that doesn't exist
yet). Deploying a single threshold means ~30% of real flash floods get NO
signal at all. This script does not change the model or its ceiling --
it changes what you DO with the probabilities it already produces, by
reading a second row out of the SAME threshold sweep you already computed
in train_flash_flood_FINAL1.py, at no extra training or compute cost:

  WARNING  (high precision, thr from final_model_results.json["chosen_threshold"])
           -> "act on this now" -- ~70% recall, ~92% precision, ~14% FAR
  WATCH    (high recall,   thr from final_model_results.json["watch_threshold"])
           -> "elevated risk, keep monitoring" -- ~93% recall, ~87% precision, ~30% FAR
  NONE     -> below both thresholds

This is a standard two-tier pattern in operational flood warning (cf. NWS
Flash Flood Watch vs Warning) and is the single highest-value change
available right now that does not require retraining or new data.

USAGE:
    from inference_deploy import FloodScorer
    scorer = FloodScorer("/path/to/flash_flood_model_FINAL_v2.json",
                          "/path/to/final_model_results.json")
    tier, prob = scorer.score(feature_row)          # single row
    tiers, probs = scorer.score_batch(X)            # 2D array, same FEATURES order/order as training

OPTIONAL CALIBRATION:
    train_flash_flood_FINAL1.py now also saves oof_predictions.npy /
    oof_labels.npy. These are RAW model scores, not calibrated
    probabilities -- XGBoost's binary:logistic output is not guaranteed to
    mean "P(flash flood)" in a well-calibrated sense, especially after
    scale_pos_weight reweighting. If you want the number shown to an
    operator ("72% chance") to be meaningful rather than just a ranking
    score, fit and apply isotonic regression as below. Calibration does
    NOT change recall/precision/FAR at a given raw-score threshold (it's a
    monotonic-preserving-enough transform in practice, but ALWAYS re-verify
    the sweep table against calibrated scores before changing which
    threshold value you deploy -- do not assume the reported thr=0.930 /
    thr=0.21 values still apply post-calibration without checking).
"""

from pathlib import Path
import json
import numpy as np
import xgboost as xgb


class FloodScorer:
    def __init__(self, model_path, results_json_path, calibrator=None):
        self.booster = xgb.Booster()
        self.booster.load_model(str(model_path))

        with open(results_json_path) as f:
            results = json.load(f)

        chosen = results.get("chosen_threshold")
        watch = results.get("watch_threshold")
        if chosen is None:
            raise ValueError(
                "final_model_results.json has no chosen_threshold -- "
                "the training run reported 'NO THRESHOLD achieves target FAR'. "
                "This model is not deployable as-is at the stated FAR tolerance."
            )
        self.warning_threshold = float(chosen["threshold"])
        self.watch_threshold = float(watch["threshold"]) if watch else None
        self.features = results["features_used"]
        self.calibrator = calibrator  # optional sklearn IsotonicRegression, already fit

        if self.watch_threshold is not None and self.watch_threshold >= self.warning_threshold:
            raise ValueError(
                "watch_threshold >= warning_threshold -- thresholds are inverted, "
                "check final_model_results.json before deploying."
            )

    def _raw_scores(self, X):
        X = np.asarray(X, dtype=np.float32)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        dmat = xgb.DMatrix(X)
        scores = self.booster.predict(dmat)
        if self.calibrator is not None:
            scores = self.calibrator.predict(scores)
        return scores

    def _tier(self, p):
        if p >= self.warning_threshold:
            return "WARNING"
        if self.watch_threshold is not None and p >= self.watch_threshold:
            return "WATCH"
        return "NONE"

    def score(self, feature_row):
        p = float(self._raw_scores(feature_row)[0])
        return self._tier(p), p

    def score_batch(self, X):
        probs = self._raw_scores(X)
        tiers = [self._tier(p) for p in probs]
        return tiers, probs


def fit_calibrator(oof_predictions_path, oof_labels_path):
    """Optional: fit isotonic calibration on the OOF predictions saved by
    train_flash_flood_FINAL1.py. Uses OOF (never-trained-on-that-fold)
    scores, not in-sample predictions, to avoid an optimistically-calibrated
    result. Re-fit whenever the model is retrained."""
    from sklearn.isotonic import IsotonicRegression
    p_oof = np.load(oof_predictions_path)
    y_oof = np.load(oof_labels_path)
    cal = IsotonicRegression(out_of_bounds="clip")
    cal.fit(p_oof, y_oof)
    return cal


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Score a feature CSV with the two-tier flood model.")
    ap.add_argument("model_path")
    ap.add_argument("results_json")
    ap.add_argument("features_csv", help="CSV with a header row matching FEATURES order/names")
    ap.add_argument("--calibrate-with", nargs=2, metavar=("OOF_PRED_NPY", "OOF_LABEL_NPY"),
                     help="Optionally fit+apply isotonic calibration from saved OOF arrays")
    args = ap.parse_args()

    cal = fit_calibrator(*args.calibrate_with) if args.calibrate_with else None
    scorer = FloodScorer(args.model_path, args.results_json, calibrator=cal)

    import pandas as pd
    df = pd.read_csv(args.features_csv)
    tiers, probs = scorer.score_batch(df[scorer.features].to_numpy(np.float32))
    for t, p in zip(tiers, probs):
        print(f"{t}\t{p:.4f}")
