"""
Data models for the Flash Flood Prediction System.

These are the contracts every other module (ingestion, risk engine,
alert dispatch, and eventually the ML model service) is built against.
Keeping them centralised means the model team can import the same
`SensorReading` / `Alert` shapes without re-deriving the schema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, field_validator


class SensorType(str, Enum):
    RAINFALL = "rainfall"
    SOIL_MOISTURE = "soil_moisture"
    WATER_LEVEL = "water_level"
    SLOPE_TILT = "slope_tilt"          # for landslide precursor signals
    TEMPERATURE = "temperature"         # glacier-melt contribution


class RiskLevel(str, Enum):
    NORMAL = "normal"
    WATCH = "watch"
    WARNING = "warning"
    CRITICAL = "critical"


# Single source of truth for risk-level ordering. Both the risk engine and the
# dispatcher need to compare levels (escalation, suppression), so it lives here
# rather than being redefined in each module.
RISK_LEVEL_ORDER: List[RiskLevel] = [
    RiskLevel.NORMAL,
    RiskLevel.WATCH,
    RiskLevel.WARNING,
    RiskLevel.CRITICAL,
]


def risk_rank(level: RiskLevel) -> int:
    return RISK_LEVEL_ORDER.index(level)


class AlertTier(str, Enum):
    """How much human involvement an alert needs before it goes out.

    The tier is a function of the risk engine's *confidence*, not its risk
    level. A confident WATCH still fires automatically; an unconfident
    WARNING waits for a human to look at it.
    """

    TIER_1_AUTO = "tier_1_auto"        # high confidence  -> dispatch immediately
    TIER_2_VETO = "tier_2_veto"        # medium confidence -> dispatch after a veto window
    TIER_3_REVIEW = "tier_3_review"    # low confidence   -> dashboard only, needs explicit approval


class AlertStatus(str, Enum):
    QUEUED = "queued"                          # handed to the dispatch queue
    PENDING_VETO = "pending_veto"              # Tier 2 countdown running
    AWAITING_REVIEW = "awaiting_review"        # Tier 3, needs a human to approve
    DISPATCHING = "dispatching"                # senders in flight (veto is too late)
    DISPATCHED = "dispatched"                  # every channel succeeded
    PARTIALLY_DISPATCHED = "partially_dispatched"  # some channels failed
    FAILED = "failed"                          # every channel failed
    VETOED = "vetoed"                          # human cancelled during the Tier 2 window
    DISMISSED = "dismissed"                    # human rejected a Tier 3 alert
    SUPERSEDED = "superseded"                  # replaced by a higher-severity alert for the same ward


# Canonical unit each sensor type is normalised to internally.
CANONICAL_UNITS = {
    SensorType.RAINFALL: "mm",
    SensorType.SOIL_MOISTURE: "%",
    SensorType.WATER_LEVEL: "m",
    SensorType.SLOPE_TILT: "deg",
    SensorType.TEMPERATURE: "C",
}

# Sane physical bounds per sensor type — used to reject garbage readings
# (sensor faults, transmission errors) before they ever reach the risk engine.
VALID_RANGES = {
    SensorType.RAINFALL: (0.0, 500.0),        # mm in a single reading window
    SensorType.SOIL_MOISTURE: (0.0, 100.0),   # %
    SensorType.WATER_LEVEL: (0.0, 50.0),      # m
    SensorType.SLOPE_TILT: (-90.0, 90.0),     # deg
    SensorType.TEMPERATURE: (-30.0, 55.0),    # C
}


class SensorReading(BaseModel):
    sensor_id: str = Field(..., min_length=1)
    ward_id: str = Field(..., min_length=1)
    type: SensorType
    value: float
    unit: str
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_reasonable(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        # Reject readings from the future (clock skew tolerance: 5 min)
        # or absurdly old readings (>24h — stale for flash-flood purposes).
        delta_future = (v - now).total_seconds()
        delta_past = (now - v).total_seconds()
        if delta_future > 300:
            raise ValueError("timestamp is in the future")
        if delta_past > 86400:
            raise ValueError("timestamp is stale (>24h old)")
        return v


class NormalizedReading(BaseModel):
    """A SensorReading after unit conversion + range validation."""
    sensor_id: str
    ward_id: str
    type: SensorType
    value: float          # in CANONICAL_UNITS[type]
    unit: str
    timestamp: datetime
    is_anomalous: bool = False   # flagged but not dropped (e.g. spike)


class IngestResult(BaseModel):
    accepted: List[NormalizedReading] = []
    rejected: List[dict] = []   # {"raw": ..., "reason": ...}


class Alert(BaseModel):
    alert_id: str = Field(default_factory=lambda: uuid4().hex)
    ward_id: str
    # Denormalised from ward_config at build time. An alert is an audit record:
    # it should still read correctly if the ward registry is edited afterwards.
    ward_name: Optional[str] = None
    risk_level: RiskLevel
    message: str
    channels: List[str]
    estimated_lead_time_minutes: Optional[int] = None
    evacuation_point: Optional[str] = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # --- Confidence tiering ---
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    tier: AlertTier = AlertTier.TIER_1_AUTO
    tier_reason: str = ""
    status: AlertStatus = AlertStatus.QUEUED

    # Tier 2 dead-man's-switch. `veto_deadline` is an absolute UTC instant so
    # the dashboard can run its own countdown without trusting a duration that
    # went stale in transit.
    veto_deadline: Optional[datetime] = None
    veto_window_seconds: Optional[int] = None

    # --- Outcome bookkeeping ---
    dispatched_at: Optional[datetime] = None
    failed_channels: List[str] = []
    vetoed_at: Optional[datetime] = None
    acted_by: Optional[str] = None      # who vetoed / approved / dismissed
    action_reason: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def seconds_remaining(self) -> Optional[float]:
        """Seconds left on the Tier 2 countdown, computed at serialization time.

        None once the alert is no longer cancellable — the dashboard should
        treat None as "the veto button is gone", not "zero".
        """
        if self.veto_deadline is None or self.status is not AlertStatus.PENDING_VETO:
            return None
        remaining = (self.veto_deadline - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, round(remaining, 1))


class AlertActionRequest(BaseModel):
    """Body for veto / approve / dismiss. `actor` is recorded on the alert so
    there's an audit trail of who cancelled a warning."""
    actor: str = Field(..., min_length=1)
    reason: Optional[str] = None


class AlertActionResult(BaseModel):
    """Result of a veto/approve/dismiss attempt.

    `ok=False` with `status=dispatching|dispatched` is the honest answer to a
    veto that arrived too late — the UI must show that rather than implying
    the alert was cancelled.
    """
    ok: bool
    alert_id: str
    status: Optional[AlertStatus] = None
    detail: str
    alert: Optional[Alert] = None


class DispatchSummary(BaseModel):
    """Per-channel send outcome, for observability."""
    alert_id: str
    results: Dict[str, bool]
