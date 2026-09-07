"""
Sensor ingestion pipeline.

Flow for each incoming raw reading:
  1. Schema validation (SensorReading) — shape, types, timestamp sanity
  2. Range validation — reject physically impossible values
  3. Unit conversion — normalize to canonical units
  4. Duplicate detection — drop exact repeats (sensor_id + timestamp)
  5. Spike/anomaly flagging — flag (don't drop) implausible jumps vs. recent history

Accepted readings are buffered per-ward in memory (`WardBuffer`), which is
what the risk engine reads from. Swap `WardBuffer` for a real DB/time-series
store (e.g. TimescaleDB, InfluxDB) when moving past prototype stage.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, Hashable, Iterable, List, Set, Tuple

from pydantic import ValidationError

from schemas import (
    IngestResult,
    NormalizedReading,
    SensorReading,
    SensorType,
    VALID_RANGES,
    CANONICAL_UNITS,
)
from unit_conversion import UnsupportedUnitError, to_canonical

# How many past readings per (ward, sensor_type) to keep for spike detection.
HISTORY_WINDOW = 20

# How many (sensor_id, timestamp) keys to remember for duplicate detection.
# This is a GLOBAL budget across every sensor, so it must be much larger than
# HISTORY_WINDOW: with ~500 sensors reporting every 5 minutes, 50k keys is
# roughly 8 hours of dedup memory (~5 MB of tuples). Sized for "long enough
# that a retrying gateway can't sneak a duplicate past us", not forever.
DEDUP_CAPACITY = 50_000

# Multiplier over recent average that triggers an anomaly flag.
# e.g. rainfall reading 5x the recent average gets flagged (kept, not dropped —
# a real spike is exactly what we care about; we just want it double-checked).
SPIKE_MULTIPLIER = 5.0

# Absolute floor that must ALSO be cleared before the multiplier means anything.
# Without this, a 0.1mm drizzle baseline makes a routine 0.6mm reading look like
# a 5x anomaly. Units are canonical units for the sensor type.
MIN_ABSOLUTE_SPIKE_DELTA: Dict[SensorType, float] = {
    SensorType.RAINFALL: 5.0,        # mm — below this a "5x jump" is just noise
    SensorType.SOIL_MOISTURE: 15.0,  # %
    SensorType.WATER_LEVEL: 0.25,    # m
    SensorType.SLOPE_TILT: 1.5,      # deg
    SensorType.TEMPERATURE: 5.0,     # C
}

# Sensor types on a ratio scale (a true, meaningful zero) — only these can be
# sensibly judged by a multiplier. Temperature and tilt are interval scales:
# "5x the average temperature" is meaningless, and tilt averages can be
# negative, which silently disabled spike detection for tilt in the old code.
# For those two, the absolute delta is the whole test.
_RATIO_SCALE_TYPES = {
    SensorType.RAINFALL,
    SensorType.SOIL_MOISTURE,
    SensorType.WATER_LEVEL,
}


class _BoundedKeySet:
    """A set with a FIFO eviction policy, for duplicate detection.

    Membership tests stay O(1) like a plain set, but the memory is capped:
    once `capacity` keys are held, adding a new key evicts the oldest.
    """

    def __init__(self, capacity: int = DEDUP_CAPACITY):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._keys: Set[Hashable] = set()
        self._order: Deque[Hashable] = deque()

    def __contains__(self, key: Hashable) -> bool:
        return key in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, key: Hashable) -> None:
        if key in self._keys:
            return
        self._keys.add(key)
        self._order.append(key)
        while len(self._order) > self.capacity:
            self._keys.discard(self._order.popleft())


class WardBuffer:
    """In-memory rolling store of normalized readings, keyed by ward + sensor type."""

    def __init__(self, window: int = HISTORY_WINDOW, dedup_capacity: int = DEDUP_CAPACITY):
        self.window = window
        self._data: Dict[Tuple[str, SensorType], Deque[NormalizedReading]] = defaultdict(
            lambda: deque(maxlen=self.window)
        )
        # Bounded: an unbounded set here grew forever on a continuous stream,
        # since it was never tied to the deques' maxlen.
        self._seen_keys = _BoundedKeySet(dedup_capacity)

    def is_duplicate(self, sensor_id: str, timestamp) -> bool:
        return (sensor_id, timestamp) in self._seen_keys

    def add(self, reading: NormalizedReading) -> None:
        self._seen_keys.add((reading.sensor_id, reading.timestamp))
        self._data[(reading.ward_id, reading.type)].append(reading)

    def clear(self) -> None:
        """Drop every buffered reading and dedup key. Used by the demo reset."""
        self._data.clear()
        self._seen_keys = _BoundedKeySet(self._seen_keys.capacity)

    def recent(self, ward_id: str, sensor_type: SensorType) -> List[NormalizedReading]:
        return list(self._data.get((ward_id, sensor_type), []))

    def latest_value(self, ward_id: str, sensor_type: SensorType):
        readings = self.recent(ward_id, sensor_type)
        return readings[-1].value if readings else None

    def known_sensors(self, ward_id: str | None = None) -> Dict[str, NormalizedReading]:
        """Latest reading per sensor_id — backs the sensor-health view.

        Only covers sensors still inside the rolling window; a sensor silent
        for longer than `window` readings drops out entirely, so pair this
        with a registry of expected sensors when you build that endpoint.
        """
        latest: Dict[str, NormalizedReading] = {}
        for (w_id, _type), readings in self._data.items():
            if ward_id is not None and w_id != ward_id:
                continue
            for r in readings:
                seen = latest.get(r.sensor_id)
                if seen is None or r.timestamp > seen.timestamp:
                    latest[r.sensor_id] = r
        return latest

    def stats(self) -> Dict[str, int]:
        return {
            "series": len(self._data),
            "readings": sum(len(d) for d in self._data.values()),
            "dedup_keys": len(self._seen_keys),
            "dedup_capacity": self._seen_keys.capacity,
        }


def _is_spike(buffer: WardBuffer, ward_id: str, sensor_type: SensorType, value: float) -> bool:
    """Flag implausible jumps. Requires BOTH an absolute jump and (on ratio-scale
    sensors) a large relative jump, so low baselines don't manufacture anomalies."""
    history = buffer.recent(ward_id, sensor_type)
    if len(history) < 3:
        return False  # not enough history to judge

    avg = sum(r.value for r in history) / len(history)
    delta = abs(value - avg)

    floor = MIN_ABSOLUTE_SPIKE_DELTA.get(sensor_type)
    if floor is not None and delta < floor:
        return False  # too small to be worth flagging, whatever the ratio says

    if sensor_type not in _RATIO_SCALE_TYPES:
        # Interval scale: the absolute delta above is the only meaningful test.
        return True

    if avg <= 0:
        # Baseline is a true zero (e.g. no rain at all) and the jump already
        # cleared the absolute floor — that is a genuine step change.
        return True
    return abs(value) > avg * SPIKE_MULTIPLIER


def ingest_batch(raw_readings: Iterable[dict], buffer: WardBuffer) -> IngestResult:
    """Validate + normalize a batch of raw sensor payloads.

    Never raises — every failure is captured in `IngestResult.rejected` with
    a reason, so a bad reading from one faulty sensor can't take down the batch.
    """
    result = IngestResult()

    for raw in raw_readings:
        try:
            reading = SensorReading.model_validate(raw)
        except ValidationError as e:
            result.rejected.append({"raw": raw, "reason": f"schema_invalid: {e.errors()}"})
            continue

        # Range check (pre-conversion is wrong; convert first, then range-check
        # against canonical bounds so unit mistakes don't slip through as "valid").
        try:
            canonical_value = to_canonical(reading.type, reading.value, reading.unit)
        except UnsupportedUnitError as e:
            result.rejected.append({"raw": raw, "reason": str(e)})
            continue

        lo, hi = VALID_RANGES[reading.type]
        if not (lo <= canonical_value <= hi):
            result.rejected.append({
                "raw": raw,
                "reason": f"value {canonical_value} out of valid range [{lo}, {hi}] "
                          f"for {reading.type.value}",
            })
            continue

        if buffer.is_duplicate(reading.sensor_id, reading.timestamp):
            result.rejected.append({"raw": raw, "reason": "duplicate_reading"})
            continue

        anomalous = _is_spike(buffer, reading.ward_id, reading.type, canonical_value)

        normalized = NormalizedReading(
            sensor_id=reading.sensor_id,
            ward_id=reading.ward_id,
            type=reading.type,
            value=canonical_value,
            unit=CANONICAL_UNITS[reading.type],
            timestamp=reading.timestamp,
            is_anomalous=anomalous,
        )
        buffer.add(normalized)
        result.accepted.append(normalized)

    return result
