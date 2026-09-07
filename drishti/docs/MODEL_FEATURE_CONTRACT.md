# Model feature contract

The 34 features the trained model expects, and the range each one actually
occupies. Anyone writing the upstream job that posts to
`POST /wards/{id}/meteo` needs this.

## Why this document exists

The model does not validate its inputs. XGBoost routes an out-of-range value
down one side of a split and returns a confident number, so a feature on the
wrong scale produces a plausible-looking score rather than an error. This
failure is completely silent.

It is not hypothetical. Building the demo fixtures, a hand-written vector
representing 240 mm of rain in 24 hours on 25-degree flood-prone terrain
scored **0.0027** — nowhere near the 0.93 warning threshold. Nothing was wrong
with the model. Six features were on scales their names did not suggest.

## The ranges

Every number below is derived from the booster's own split thresholds
(`Booster.trees_to_dataframe()`), so it reflects what the model actually
learned, not what the pipeline intended to produce. A value outside the
low-high span is not necessarily wrong, but the model has no evidence there:
it will fall on whichever side of the outermost split it lands on and stay
there.

"Gain share" is that feature's share of total split gain — how much of the
model's decision-making it accounts for.

| feature | splits | lowest split | highest split | gain share |
|---|---:|---:|---:|---:|
| `elevation_m` | 84 | 253.733 | 5494.4 | 0.2% |
| `slope_deg` | 114 | 0.297 | 8.074 | 0.5% |
| `aspect_sin` | 54 | -0.967 | 0.941 | 0.1% |
| `aspect_cos` | 25 | -1.0 | 0.503 | 0.1% |
| `terrain_ruggedness` | 183 | 107.35 | 1280.28 | 1.1% |
| `flood_prone_terrain` | 3 | 1.0 | 1.0 | 0.4% |
| `rain_intensity_3h` | 42 | 0.005 | 19.69 | 0.1% |
| `rain_3h` | 12 | 0.09 | 19.69 | 0.1% |
| `rain_6h` | 29 | 0.005 | 49.805 | 0.1% |
| `rain_12h` | 34 | 0.005 | 75.705 | 0.1% |
| `rain_24h` | 159 | 45.945 | 87.825 | 69.4% |
| `rain_48h` | 100 | 38.91 | 134.89 | 5.0% |
| `rain_72h` | 150 | 48.975 | 165.72 | 0.2% |
| `rain_5d` | 136 | 53.565 | 193.83 | 0.2% |
| `rain_7d` | 76 | 65.985 | 265.215 | 0.2% |
| `rain_14d` | 208 | 78.405 | 414.665 | 0.2% |
| `max_intensity_24h` | 120 | 13.74 | 53.09 | 1.4% |
| `wet_fraction_7d` | 64 | 0.073 | 0.732 | 0.1% |
| `rain_anomaly_24h` | 121 | 21.172 | 87.132 | 18.0% |
| `temperature_2m_c` | 55 | 7.069 | 29.605 | 0.1% |
| `dewpoint_2m_c` | 47 | 4.456 | 26.865 | 0.1% |
| `humidity_pct` | 140 | 68.53 | 99.602 | 0.2% |
| `pressure_hpa` | 212 | 611.964 | 981.54 | 0.2% |
| `wind_speed_10m` | 87 | 0.234 | 4.087 | 0.3% |
| `wind_direction_10m` | 49 | 48.736 | 334.804 | 0.1% |
| `soil_moisture_m3m3` | 72 | 0.284 | 0.506 | 0.1% |
| `era5_precip_3h_mm` | 59 | 0.0 | 6.047 | 0.1% |
| `flow_accumulation` | 7 | 2.0 | 9.0 | 0.1% |
| `twi` | 52 | 14.933 | 23.154 | 0.1% |
| `dist_to_stream_m` | 22 | 202.614 | 1523.482 | 0.1% |
| `nearest_stream_order` | 2 | 3.0 | 3.0 | 0.2% |
| `nearest_stream_flow_order` | 7 | 6.0 | 7.0 | 0.1% |
| `drainage_density_km_per_km2` | 32 | 0.184 | 0.424 | 0.2% |
| `dist_to_confluence_m` | 24 | 648.826 | 4283.631 | 0.1% |


## The traps, specifically

`rain_24h` carries 69% of the model's gain and `rain_anomaly_24h` another 18%.
Between them, those two features are most of the model. Both are easy to get
wrong:

- **`rain_anomaly_24h` is not a z-score.** Its splits run from 21 to 87. A
  vector built with anomaly values in the -3 to +3 range that a standardised
  anomaly would produce sits below every split in the model, and the second
  most important feature contributes nothing.

- **`flow_accumulation` splits run from 2 to 9.** That is not a raw upstream
  cell count, which would be in the thousands. It is log-scaled or binned
  somewhere in the HydroSHEDS join. Feeding a raw count puts every row past
  the top split.

- **`terrain_ruggedness` splits run from 107 to 1280.** This is the real
  Terrain Ruggedness Index in metres, from the fix described in the briefing.
  It is emphatically not the old normalised `slope_deg / 90`, which lived in
  [0, 1].

- **`slope_deg` splits stop at 8.07.** This is the known coarse-DEM problem,
  visible in the model itself: it has never seen a steep slope. When the raw
  SRTM `.hgt` tiles are recovered and slope is recomputed at native
  resolution, real gorge slopes near 28 degrees will sit off the right-hand
  edge of every slope split in this model. **The model must be retrained at
  that point.** Feeding real slopes to this booster would be worse than
  feeding it the coarse ones.

- **`twi` splits start at 14.93.** Values below that are outside anything
  the model saw.

- **`nearest_stream_order` and `nearest_stream_flow_order` are near-constants
  in this model** (2 and 7 splits respectively). They carry almost no weight;
  do not spend engineering effort on their precision.

## One non-obvious behaviour worth knowing

The model is not monotonic in total rainfall, and it should not be expected to
be. Holding everything else fixed and raising `rain_48h` from 90 to 140 drops
the score from 0.46 to 0.08, while lowering `rain_5d` from 200 to 100 raises it
to 0.76.

Read physically, the model learned that a flash flood signature is an intense
burst against a comparatively dry antecedent — a cloudburst — rather than the
tail end of a long wet spell. That is a sensible thing to have learned. It also
means "set every rainfall feature high" produces a low score, which is a
confusing first experience when hand-testing.

## Handling missing values

Leave a genuinely unavailable feature out of the payload. `POST
/wards/{id}/meteo` converts absent keys to NaN and XGBoost splits on
missingness natively — roughly 0.1% of ERA5 rows legitimately have none, and
the model was trained that way.

Do not impute. Substituting a mean invents weather that was never observed and
is strictly worse than telling the model you do not know.

The service refuses to score a vector missing more than half its features and
returns the list of what was absent, so a broken upstream job surfaces as an
error rather than a confident all-clear.

## Keeping this current

This table is generated from the model artifact. Regenerate it after every
retrain:

```bash
cd ml && python generate_feature_contract.py
```

The values above describe `flash_flood_model_FINAL_v2.json` and nothing else.
