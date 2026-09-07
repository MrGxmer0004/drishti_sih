"""
generate_feature_contract.py — regenerate the feature range table in
docs/MODEL_FEATURE_CONTRACT.md from the trained model artifact.

Run after every retrain:
    cd ml && python generate_feature_contract.py

WHY THIS IS DERIVED FROM THE MODEL AND NOT FROM THE DATASET

The dataset says what the pipeline intended to produce. The booster's split
thresholds say what it actually learned from. Those disagreed once already —
`terrain_ruggedness` was a duplicate of `slope_deg / 90` for part of this
project's history — and the split thresholds are the version that matters at
inference time, because they are what an incoming feature vector is compared
against.

The table this prints is a range check, not a schema. A value outside the
low-high span is not rejected anywhere; it simply falls on the outer side of
the outermost split, where the model has no evidence. That is the silent
failure this file exists to make visible.
"""

from __future__ import annotations

import json
from pathlib import Path

import xgboost as xgb

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
MODEL = ARTIFACTS / "flash_flood_model_FINAL_v2.json"
RESULTS = ARTIFACTS / "final_model_results.json"


def main() -> int:
    booster = xgb.Booster()
    booster.load_model(str(MODEL))

    features = json.loads(RESULTS.read_text())["features_used"]
    index_to_name = dict(enumerate(features))

    splits = booster.trees_to_dataframe()
    splits = splits[splits.Feature != "Leaf"].copy()
    splits["name"] = splits.Feature.str.lstrip("f").astype(int).map(index_to_name)

    gain = booster.get_score(importance_type="gain")
    total_gain = sum(gain.values()) or 1.0
    grouped = splits.groupby("name")["Split"].agg(["count", "min", "max"])

    print("| feature | splits | lowest split | highest split | gain share |")
    print("|---|---:|---:|---:|---:|")
    for i, name in enumerate(features):
        share = gain.get(f"f{i}", 0.0) / total_gain
        if name in grouped.index:
            row = grouped.loc[name]
            print(
                f"| `{name}` | {int(row['count'])} | {row['min']:.3f} | "
                f"{row['max']:.3f} | {share:.1%} |"
            )
        else:
            # Never split on. The model ignores this feature entirely — worth
            # noticing, since it means the column costs pipeline effort for
            # nothing.
            print(f"| `{name}` | 0 | — | — | 0.0% |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
