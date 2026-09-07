"""
sample_meteo_vectors.py — three feature vectors that exercise each model tier.

USED BY: demo.py, and anyone poking at `POST /wards/{id}/meteo` by hand.

*** WHERE THESE NUMBERS CAME FROM — READ THIS ***

They are NOT rows from `model_dataset_v5.parquet`. They were reverse-engineered
from the trained booster's own split thresholds (`Booster.trees_to_dataframe()`)
and then tuned by coordinate search until each one lands in the intended tier.
That makes them a demo fixture and a smoke test, nothing more. In particular:

  * They are not real observed weather. Do not quote a score from these as
    evidence the model works.
  * The coordinate search optimised the score directly, which is close to
    adversarial. A high score here is a statement about the model's decision
    surface, not about Uttarakhand.

REPLACE THEM with three real held-out rows — one true positive, one near-miss
negative, one easy negative — as soon as the dataset is to hand. That turns
this file from a fixture into an actual end-to-end check, and it is a ten-minute
job for whoever has `model_dataset_v5.parquet` open.

WHAT BUILDING THESE REVEALED (worth knowing before you write an ingest job):
several features are NOT on the scale their names suggest. See
docs/MODEL_FEATURE_CONTRACT.md for the observed range of every feature. The
sharp edges:

  * `flow_accumulation` splits fall between 2 and 9 — it is log-scaled or
    binned upstream, not a raw upstream cell count.
  * `rain_anomaly_24h` splits fall between 21 and 87 — not a z-score.
  * `terrain_ruggedness` splits fall between 107 and 1280 — the TRI in metres,
    not the old normalised `slope_deg/90`.
  * `slope_deg` splits top out at 8.07, consistent with the known coarse-DEM
    problem. Feeding a real ~28° gorge slope puts you off the right-hand edge
    of every split in the model.

Get any of these wrong and the model returns a confident near-zero rather than
an error, because a tree happily routes an out-of-range value down one side.
That failure is silent. It is the single most likely way this integration
breaks in production.
"""

from __future__ import annotations

from typing import Dict

# Clears the WARNING threshold (0.93). Raw score ~0.997.
WARNING_VECTOR: Dict[str, float] = {
    "elevation_m": 1500.0,
    "slope_deg": 6.0,
    "aspect_sin": 0.5,
    "aspect_cos": -0.5,
    "terrain_ruggedness": 400.0,
    "flood_prone_terrain": 1.0,
    "rain_intensity_3h": 3.0,
    "rain_3h": 10.0,
    "rain_6h": 25.0,
    "rain_12h": 76.0,
    "rain_24h": 90.0,
    "rain_48h": 60.0,
    "rain_72h": 70.0,
    "rain_5d": 100.0,
    "rain_7d": 100.0,
    "rain_14d": 250.0,
    "max_intensity_24h": 50.0,
    "wet_fraction_7d": 0.5,
    "rain_anomaly_24h": 45.0,
    "temperature_2m_c": 22.0,
    "dewpoint_2m_c": 10.0,
    "humidity_pct": 85.0,
    "pressure_hpa": 700.0,
    "wind_speed_10m": 2.0,
    "wind_direction_10m": 200.0,
    "soil_moisture_m3m3": 0.43,
    "era5_precip_3h_mm": 3.0,
    "flow_accumulation": 9.5,
    "twi": 20.0,
    "dist_to_stream_m": 250.0,
    "nearest_stream_order": 4.0,
    "nearest_stream_flow_order": 7.0,
    "drainage_density_km_per_km2": 0.40,
    "dist_to_confluence_m": 900.0,
}

# Between the two thresholds — WATCH tier. Raw score ~0.57.
WATCH_VECTOR: Dict[str, float] = {
    **WARNING_VECTOR,
    "slope_deg": 3.0,
    "rain_intensity_3h": 6.0,
    "rain_12h": 40.0,
    "rain_24h": 60.0,
    "rain_48h": 90.0,
    "rain_72h": 110.0,
    "rain_5d": 200.0,
    "rain_7d": 270.0,
    "rain_14d": 300.0,
    "max_intensity_24h": 30.0,
    "dewpoint_2m_c": 21.0,
    "humidity_pct": 98.0,
    "pressure_hpa": 850.0,
    "soil_moisture_m3m3": 0.48,
}

# Below both thresholds — NONE. Raw score ~0.0003.
CALM_VECTOR: Dict[str, float] = {
    **WARNING_VECTOR,
    "rain_intensity_3h": 0.2,
    "rain_3h": 0.4,
    "rain_6h": 1.0,
    "rain_12h": 2.0,
    "rain_24h": 4.0,
    "rain_48h": 6.0,
    "rain_72h": 9.0,
    "rain_5d": 15.0,
    "rain_7d": 20.0,
    "rain_14d": 40.0,
    "max_intensity_24h": 2.0,
    "rain_anomaly_24h": 1.5,
    "wet_fraction_7d": 0.1,
    "era5_precip_3h_mm": 0.0,
    "soil_moisture_m3m3": 0.30,
    "humidity_pct": 52.0,
    "dewpoint_2m_c": 8.0,
    "temperature_2m_c": 19.0,
}

VECTORS = {
    "warning": WARNING_VECTOR,
    "watch": WATCH_VECTOR,
    "calm": CALM_VECTOR,
}
