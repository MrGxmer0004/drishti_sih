"""
Unit conversion for incoming sensor readings.

Field sensors won't all report in the same unit (some rain gauges report
inches, some water-level sensors report cm, etc). This module converts
whatever comes in to the canonical unit for that sensor type so the risk
engine never has to think about units.
"""

from schemas import SensorType

# (from_unit -> factor to multiply by to get canonical unit), per sensor type.
# Canonical units: rainfall=mm, soil_moisture=%, water_level=m, slope_tilt=deg, temperature=C
_CONVERSIONS = {
    SensorType.RAINFALL: {
        "mm": lambda v: v,
        "cm": lambda v: v * 10,
        "in": lambda v: v * 25.4,
    },
    SensorType.SOIL_MOISTURE: {
        "%": lambda v: v,
        "fraction": lambda v: v * 100,
    },
    SensorType.WATER_LEVEL: {
        "m": lambda v: v,
        "cm": lambda v: v / 100,
        "ft": lambda v: v * 0.3048,
    },
    SensorType.SLOPE_TILT: {
        "deg": lambda v: v,
        "rad": lambda v: v * 57.29577951308232,
    },
    SensorType.TEMPERATURE: {
        "C": lambda v: v,
        "F": lambda v: (v - 32) * 5.0 / 9.0,
        "K": lambda v: v - 273.15,
    },
}


class UnsupportedUnitError(ValueError):
    pass


def to_canonical(sensor_type: SensorType, value: float, unit: str) -> float:
    """Convert `value` in `unit` to this sensor type's canonical unit."""
    table = _CONVERSIONS.get(sensor_type)
    if table is None:
        raise UnsupportedUnitError(f"no conversion table for sensor type {sensor_type}")
    fn = table.get(unit)
    if fn is None:
        raise UnsupportedUnitError(
            f"unit '{unit}' not supported for sensor type '{sensor_type.value}' "
            f"(supported: {list(table.keys())})"
        )
    return fn(value)
