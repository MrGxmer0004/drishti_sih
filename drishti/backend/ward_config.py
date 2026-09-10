"""
ward_config.py — static ward reference data (name, evacuation point, glacier
flag, baseline lead time, dashboard map position).

Replace this with a real lookup (DB table / GIS layer) once ward metadata is
finalised. `glacier_fed` matters because FFGS misses glacier-triggered events
entirely — wards flagged True get an extra risk contribution from
temperature-driven melt signals even when rainfall is low.

TWO SETS OF COORDINATES, deliberately:
  * `map_x` / `map_y` are PERCENTAGE positions on the dashboard's schematic
    ward map. They are layout values, not geography. The dashboard places a
    marker at `left: map_x%`, `top: map_y%`.
  * `latitude` / `longitude` are the real WGS84 coordinates (point locations
    for the six pilot wards in Rudraprayag district, Uttarakhand). The
    dashboard's geographic map projects markers from these; `map_x`/`map_y`
    are kept only as a fallback for the old schematic view.

The ward IDs below match the ones used in `demo.py` and in the dashboard's
offline demo mode, so live and demo mode show the same place names.
"""

from typing import Dict, List, Optional, TypedDict


class WardInfo(TypedDict):
    name: str
    evacuation_point: str
    glacier_fed: bool
    baseline_lead_time_minutes: int  # rough terrain-based travel time to safety
    map_x: float                     # dashboard map position, % of width
    map_y: float                     # dashboard map position, % of height
    latitude: Optional[float]        # real coords — pending the GIS layer
    longitude: Optional[float]


WARD_REGISTRY: Dict[str, WardInfo] = {
    "WD_1023": {
        "name": "Rudraprayag Town",
        "evacuation_point": "Govt. Inter College",
        "glacier_fed": False,
        "baseline_lead_time_minutes": 20,
        "map_x": 34, "map_y": 62,
        "latitude": 30.2844, "longitude": 78.9811,
    },
    "WD_1044": {
        "name": "Gaurikund",
        "evacuation_point": "Community Hall, Upper Ridge",
        "glacier_fed": True,
        "baseline_lead_time_minutes": 15,
        "map_x": 55, "map_y": 30,
        "latitude": 30.6606, "longitude": 79.0209,
    },
    "WD_2011": {
        "name": "Sonprayag",
        "evacuation_point": "Helipad Shelter",
        "glacier_fed": True,
        "baseline_lead_time_minutes": 18,
        "map_x": 46, "map_y": 48,
        "latitude": 30.6280, "longitude": 79.0207,
    },
    "WD_2087": {
        "name": "Ukhimath",
        "evacuation_point": "Block Office",
        "glacier_fed": False,
        "baseline_lead_time_minutes": 25,
        "map_x": 68, "map_y": 40,
        "latitude": 30.5147, "longitude": 79.0900,
    },
    "WD_3009": {
        "name": "Chandrapuri",
        "evacuation_point": "Riverside School (high block)",
        "glacier_fed": False,
        "baseline_lead_time_minutes": 22,
        "map_x": 60, "map_y": 72,
        "latitude": 30.3670, "longitude": 78.9880,
    },
    "WD_3044": {
        "name": "Tilwara",
        "evacuation_point": "Mandi Ground",
        "glacier_fed": False,
        "baseline_lead_time_minutes": 24,
        "map_x": 24, "map_y": 68,
        "latitude": 30.2430, "longitude": 78.9560,
    },
}

_DEFAULT_WARD: WardInfo = {
    "name": "Unknown Ward",
    "evacuation_point": "Nearest designated safe zone",
    "glacier_fed": False,
    "baseline_lead_time_minutes": 20,
    "map_x": 50, "map_y": 50,
    "latitude": None, "longitude": None,
}


def get_ward_info(ward_id: str) -> WardInfo:
    return WARD_REGISTRY.get(ward_id, _DEFAULT_WARD)


def known_ward_ids() -> List[str]:
    """Ward IDs the system has metadata for. A ward outside this list is still
    assessed — it just falls back to `_DEFAULT_WARD`, which is why the
    dashboard's ward list and the risk endpoint can legitimately disagree."""
    return list(WARD_REGISTRY.keys())
