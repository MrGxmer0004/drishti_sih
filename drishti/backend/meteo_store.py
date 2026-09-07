"""
meteo_store.py — latest satellite/reanalysis feature vector per ward.

The ground-sensor stream arrives by `POST /ingest` and lands in `WardBuffer`.
The meteo-hydro branch arrives on a completely different cadence: an upstream
job pulls GPM IMERG / ERA5, joins the static SRTM + HydroSHEDS terrain columns,
and posts one finished 34-feature vector per ward per cycle to
`POST /wards/{id}/meteo`. This module is where that vector waits until the next
risk assessment reads it.

It is deliberately a separate store from `WardBuffer`, not a sixth sensor type:
the two branches have different units, different cadences, different failure
modes, and must not be averaged together (project briefing §10).

PROTOTYPE LIMITATION, same as WardBuffer: in-memory, single process, lost on
restart. A restart means every ward reverts to ground-sensors-only until the
next upstream cycle posts fresh vectors. Back this with Redis or a table before
anyone depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class MeteoFeatures:
    ward_id: str
    features: Dict[str, float]
    observed_at: datetime       # when the underlying observation window closed
    received_at: datetime       # when this service was told about it
    source: str = "unspecified"  # e.g. "gpm_imerg_v07+era5", "backfill", "demo"

    def age_minutes(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.observed_at).total_seconds() / 60.0


class MeteoFeatureStore:
    """Last-write-wins, one vector per ward."""

    def __init__(self) -> None:
        self._by_ward: Dict[str, MeteoFeatures] = {}

    def put(
        self,
        ward_id: str,
        features: Dict[str, float],
        observed_at: Optional[datetime] = None,
        source: str = "unspecified",
    ) -> MeteoFeatures:
        now = datetime.now(timezone.utc)
        if observed_at is None:
            observed_at = now
        elif observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)

        record = MeteoFeatures(
            ward_id=ward_id,
            features=dict(features),
            observed_at=observed_at,
            received_at=now,
            source=source,
        )
        self._by_ward[ward_id] = record
        return record

    def get(self, ward_id: str) -> Optional[MeteoFeatures]:
        return self._by_ward.get(ward_id)

    def wards_with_features(self) -> List[str]:
        return list(self._by_ward.keys())

    def stats(self) -> Dict[str, object]:
        now = datetime.now(timezone.utc)
        return {
            "wards": len(self._by_ward),
            "oldest_age_minutes": round(
                max((r.age_minutes(now) for r in self._by_ward.values()), default=0.0), 1
            ),
        }


# Process-wide instance, mirroring the shared `WardBuffer` in main.py.
meteo_store = MeteoFeatureStore()
