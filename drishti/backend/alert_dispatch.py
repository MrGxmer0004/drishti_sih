"""
Alert generation + tiered dispatch.

Turns a RiskAssessment into the Alert JSON contract and sends it across
channels. Two things this module is responsible for:

1. CONFIDENCE TIERING (the dead-man's-switch)
   Alerts are routed by the risk engine's *confidence*, not just its risk level:

     Tier 1 (high confidence)   -> dispatched immediately, no human in the loop
     Tier 2 (medium confidence) -> dispatched automatically after a short veto
                                   window, during which a human can CANCEL it.
                                   Doing nothing SENDS the alert.
     Tier 3 (low confidence)    -> dashboard only; needs an explicit human
                                   approval before anything is sent.

   Tier 2 is deliberately a veto, not an approval. In the Nepal case the lead
   time was far too short for "wait for a human to say yes" to be survivable,
   so inaction has to resolve toward warning people, not toward silence.

2. NON-BLOCKING DISPATCH
   Sending is async and runs on a background worker queue, so `POST /ingest`
   returns as soon as readings are stored. Channel senders are still stubs
   (log only) — swap each `_send_*` for a real gateway using
   `httpx.AsyncClient`; the `dispatch_async(alert)` interface doesn't change.

Known limitation: everything here is in-process memory. A crash loses pending
Tier 2 countdowns and queued alerts. Before real deployment, back the queue
and the pending registry with something durable (Redis / a DB table with a
scheduled sweep), so a restart can't silently swallow a warning.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Deque, Dict, List, Optional

from schemas import (
    Alert,
    AlertActionResult,
    AlertStatus,
    AlertTier,
    RiskLevel,
    risk_rank,
)
from risk_engine import RiskAssessment
from ward_config import get_ward_info

logger = logging.getLogger("flash_flood.alerts")
logging.basicConfig(level=logging.INFO)

# Only WATCH and above generate an outbound alert — NORMAL is silent.
ALERTABLE_LEVELS = {RiskLevel.WATCH, RiskLevel.WARNING, RiskLevel.CRITICAL}

# --- Tiering policy (tune with the domain experts, not in code review) ---
TIER_1_MIN_CONFIDENCE = 0.85
TIER_2_MIN_CONFIDENCE = 0.55

# A CRITICAL assessment is never left as dashboard-only, even at low
# confidence: at critical lead times, "nobody looked at the dashboard" is a
# worse failure than a false alarm. It drops to Tier 2 instead, so a human
# still gets a window to kill it.
CRITICAL_NEVER_REVIEW_ONLY = True

# --- Veto window sizing ---
DEFAULT_VETO_SECONDS = 90
MIN_VETO_SECONDS = 30
# The veto window is a tax on lead time. Never spend more than this fraction of
# the estimated lead time waiting for a human who may not be at the desk.
MAX_VETO_FRACTION_OF_LEAD_TIME = 0.2

# Don't re-fire the same-or-lower severity for a ward inside this window.
# Without it, every ingest batch re-assesses and would restart a countdown.
ALERT_COOLDOWN_SECONDS = 600

# --- Sender reliability ---
SEND_TIMEOUT_SECONDS = 10.0
MAX_SEND_ATTEMPTS = 3
SEND_BACKOFF_SECONDS = 0.5

_MESSAGE_TEMPLATES = {
    RiskLevel.WATCH: (
        "Flash flood watch issued for your area. Conditions are being monitored "
        "closely. Stay alert to further updates."
    ),
    RiskLevel.WARNING: (
        "Flash flood warning: rising risk detected in your area. Prepare to move "
        "to higher ground. Evacuation point: {evac_point}."
    ),
    RiskLevel.CRITICAL: (
        "Flash flood risk imminent. Move to higher ground immediately. "
        "Evacuation point: {evac_point}."
    ),
}

_CHANNELS_BY_LEVEL: Dict[RiskLevel, List[str]] = {
    RiskLevel.WATCH: ["dashboard"],
    RiskLevel.WARNING: ["push", "dashboard"],
    RiskLevel.CRITICAL: ["sms", "push", "dashboard"],
}


# --- Tiering ------------------------------------------------------------

def classify_tier(risk_level: RiskLevel, confidence: float) -> tuple[AlertTier, str]:
    """Map (risk level, confidence) -> tier, with a one-line human-readable reason."""
    if confidence >= TIER_1_MIN_CONFIDENCE:
        return AlertTier.TIER_1_AUTO, (
            f"confidence {confidence:.2f} >= {TIER_1_MIN_CONFIDENCE} — dispatched automatically"
        )
    if confidence >= TIER_2_MIN_CONFIDENCE:
        return AlertTier.TIER_2_VETO, (
            f"confidence {confidence:.2f} in "
            f"[{TIER_2_MIN_CONFIDENCE}, {TIER_1_MIN_CONFIDENCE}) — auto-sends unless cancelled"
        )
    if risk_level is RiskLevel.CRITICAL and CRITICAL_NEVER_REVIEW_ONLY:
        return AlertTier.TIER_2_VETO, (
            f"confidence {confidence:.2f} is low, but CRITICAL risk is never held for "
            f"review — auto-sends unless cancelled"
        )
    return AlertTier.TIER_3_REVIEW, (
        f"confidence {confidence:.2f} < {TIER_2_MIN_CONFIDENCE} — flagged for human review, "
        f"nothing sent yet"
    )


def veto_window_seconds(lead_time_minutes: Optional[int]) -> int:
    """How long to hold a Tier 2 alert before it fires. 0 means 'fire now'.

    If the lead time is so short that even the minimum window would eat a
    meaningful slice of it, we don't offer a window at all — the alert is
    promoted to Tier 1 and goes out immediately.
    """
    if lead_time_minutes is None:
        return DEFAULT_VETO_SECONDS
    budget = lead_time_minutes * 60 * MAX_VETO_FRACTION_OF_LEAD_TIME
    if budget < MIN_VETO_SECONDS:
        return 0
    return int(min(DEFAULT_VETO_SECONDS, budget))


def build_alert(
    assessment: RiskAssessment, *, veto_seconds: Optional[int] = None
) -> Optional[Alert]:
    """Return an Alert for this assessment, or None if risk doesn't warrant one.

    `veto_seconds` overrides the computed window (used by demos/tests to keep
    countdowns short). The absolute `veto_deadline` is set here so it is
    anchored to alert creation, not to when a worker happens to pick it up.
    """
    if assessment.risk_level not in ALERTABLE_LEVELS:
        return None

    ward_info = get_ward_info(assessment.ward_id)
    template = _MESSAGE_TEMPLATES[assessment.risk_level]
    message = template.format(evac_point=ward_info["evacuation_point"])

    tier, tier_reason = classify_tier(assessment.risk_level, assessment.confidence)

    window: Optional[int] = None
    deadline: Optional[datetime] = None
    if tier is AlertTier.TIER_2_VETO:
        window = (
            veto_seconds
            if veto_seconds is not None
            else veto_window_seconds(assessment.estimated_lead_time_minutes)
        )
        if window <= 0:
            tier = AlertTier.TIER_1_AUTO
            tier_reason = (
                f"{tier_reason}; promoted to immediate dispatch — "
                f"{assessment.estimated_lead_time_minutes}min lead time leaves no room "
                f"for a veto window"
            )
            window = None
        else:
            deadline = datetime.now(timezone.utc) + timedelta(seconds=window)

    status = {
        AlertTier.TIER_1_AUTO: AlertStatus.QUEUED,
        AlertTier.TIER_2_VETO: AlertStatus.PENDING_VETO,
        AlertTier.TIER_3_REVIEW: AlertStatus.AWAITING_REVIEW,
    }[tier]

    return Alert(
        ward_id=assessment.ward_id,
        ward_name=ward_info["name"],
        risk_level=assessment.risk_level,
        message=message,
        channels=_CHANNELS_BY_LEVEL[assessment.risk_level],
        estimated_lead_time_minutes=assessment.estimated_lead_time_minutes,
        evacuation_point=ward_info["evacuation_point"],
        confidence=assessment.confidence,
        tier=tier,
        tier_reason=tier_reason,
        status=status,
        veto_window_seconds=window,
        veto_deadline=deadline,
    )


# --- CAP 1.2 rendering --------------------------------------------------
# DRISHTI's stated dissemination path is SACHET / NDMA, which speaks CAP
# (Common Alerting Protocol). Nothing here actually POSTs to SACHET — the
# channel senders below are still stubs — but this renders a fired alert into
# the exact CAP 1.2 XML a SACHET feed expects, so the format is real and
# reviewable. `status` defaults to "Exercise": these alerts are not being
# broadcast to the public, and saying so in the payload is the honest choice.

_CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"

# risk level -> (urgency, severity, certainty) per the CAP value lists.
_CAP_LEVEL_MAP = {
    RiskLevel.CRITICAL: ("Immediate", "Extreme", "Observed"),
    RiskLevel.WARNING: ("Expected", "Severe", "Likely"),
    RiskLevel.WATCH: ("Future", "Moderate", "Possible"),
}


def _cap_dt(value: Optional[datetime]) -> str:
    """CAP requires a timezone-qualified ISO-8601 instant."""
    dt = value or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def alert_to_cap_xml(alert: Alert, *, status: str = "Exercise") -> str:
    """Render a fired alert as CAP 1.2 XML (the SACHET / NDMA wire format).

    `status` is "Exercise" by default because DRISHTI is not wired to a live
    public broadcaster; pass "Actual" only from a real deployment.
    """
    urgency, severity, certainty = _CAP_LEVEL_MAP.get(
        alert.risk_level, ("Unknown", "Unknown", "Unknown")
    )
    info = get_ward_info(alert.ward_id)
    lat, lon = info.get("latitude"), info.get("longitude")

    root = ET.Element("alert", {"xmlns": _CAP_NS})
    ET.SubElement(root, "identifier").text = f"drishti.{alert.alert_id}"
    ET.SubElement(root, "sender").text = "drishti@sdma.uk.gov.in"
    ET.SubElement(root, "sent").text = _cap_dt(alert.generated_at)
    ET.SubElement(root, "status").text = status
    ET.SubElement(root, "msgType").text = "Alert"
    ET.SubElement(root, "scope").text = "Public"

    node = ET.SubElement(root, "info")
    ET.SubElement(node, "language").text = "en-IN"
    ET.SubElement(node, "category").text = "Met"
    if info.get("glacier_fed"):
        # glacier / moraine / slope collapse is a geophysical trigger too
        ET.SubElement(node, "category").text = "Geo"
    ET.SubElement(node, "event").text = f"Flash flood {alert.risk_level.value}"
    ET.SubElement(node, "urgency").text = urgency
    ET.SubElement(node, "severity").text = severity
    ET.SubElement(node, "certainty").text = certainty
    ET.SubElement(node, "senderName").text = (
        "DRISHTI — State Disaster Management Authority, Uttarakhand"
    )
    ET.SubElement(node, "headline").text = (
        f"Flash flood {alert.risk_level.value} — {alert.ward_name or alert.ward_id}"
    )
    ET.SubElement(node, "description").text = alert.message
    if alert.evacuation_point:
        ET.SubElement(node, "instruction").text = (
            f"Move to higher ground now. Nearest safe point: {alert.evacuation_point}."
        )

    for name, value in (
        ("drishti:confidence", f"{alert.confidence:.3f}"),
        ("drishti:tier", alert.tier.value),
        ("drishti:lead_time_minutes", str(alert.estimated_lead_time_minutes)),
    ):
        p = ET.SubElement(node, "parameter")
        ET.SubElement(p, "valueName").text = name
        ET.SubElement(p, "value").text = value

    area = ET.SubElement(node, "area")
    ET.SubElement(area, "areaDesc").text = (
        f"{alert.ward_name or alert.ward_id} ({alert.ward_id})"
    )
    if lat is not None and lon is not None:
        ET.SubElement(area, "circle").text = f"{lat},{lon} 5.0"
    geo = ET.SubElement(area, "geocode")
    ET.SubElement(geo, "valueName").text = "drishti:ward_id"
    ET.SubElement(geo, "value").text = alert.ward_id

    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


# --- Channel senders (stubs — replace with real integrations) ------------
# Each is async so a slow gateway parks on the event loop instead of holding a
# threadpool slot. Real implementation, e.g. for SMS:
#
#     async def _send_sms(alert: Alert) -> None:
#         async with httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS) as client:
#             resp = await client.post(SMS_GATEWAY_URL, json={...})
#             resp.raise_for_status()

async def _send_sms(alert: Alert) -> None:
    logger.info("[SMS] ward=%s :: %s", alert.ward_id, alert.message)


async def _send_push(alert: Alert) -> None:
    logger.info("[PUSH] ward=%s :: %s", alert.ward_id, alert.message)


async def _send_dashboard(alert: Alert) -> None:
    logger.info(
        "[DASHBOARD] ward=%s risk=%s confidence=%.2f tier=%s lead_time=%smin",
        alert.ward_id, alert.risk_level.value, alert.confidence,
        alert.tier.value, alert.estimated_lead_time_minutes,
    )


Sender = Callable[[Alert], Awaitable[None]]

_SENDERS: Dict[str, Sender] = {
    "sms": _send_sms,
    "push": _send_push,
    "dashboard": _send_dashboard,
}


async def _send_with_retry(channel: str, sender: Sender, alert: Alert) -> bool:
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            await asyncio.wait_for(sender(alert), timeout=SEND_TIMEOUT_SECONDS)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "dispatch attempt %s/%s failed on channel '%s' (alert=%s)",
                attempt, MAX_SEND_ATTEMPTS, channel, alert.alert_id,
            )
            if attempt < MAX_SEND_ATTEMPTS:
                await asyncio.sleep(SEND_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    return False


async def dispatch_async(alert: Alert) -> Dict[str, bool]:
    """Fan out an alert across all its channels concurrently.

    One channel failing (or being slow) must not block the others, so the
    sends run in parallel and each result is reported independently.
    """
    channels = [c for c in alert.channels if c in _SENDERS]
    for unknown in [c for c in alert.channels if c not in _SENDERS]:
        logger.warning("unknown channel '%s' — skipping", unknown)

    if not channels:
        return {}

    outcomes = await asyncio.gather(
        *(_send_with_retry(c, _SENDERS[c], alert) for c in channels)
    )
    return dict(zip(channels, outcomes))


def dispatch(alert: Alert) -> Dict[str, bool]:
    """Blocking convenience wrapper for scripts and tests.

    Refuses to run inside an event loop — calling it there would block the
    loop, which is exactly the failure mode this rewrite removes.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(dispatch_async(alert))
    raise RuntimeError(
        "dispatch() is blocking; inside async code use `await dispatch_async(alert)` "
        "or submit to an AlertDispatcher."
    )


# --- Dispatcher ---------------------------------------------------------

class AlertDispatcher:
    """Owns the alert lifecycle: tier routing, veto countdowns, background sends.

    All state transitions happen under a single lock, so a veto arriving at the
    same instant the countdown expires resolves one way or the other and the
    caller is told honestly which one happened.
    """

    def __init__(
        self,
        workers: int = 2,
        max_queue: int = 1000,
        history_size: int = 500,
        cooldown_seconds: int = ALERT_COOLDOWN_SECONDS,
        flush_pending_on_stop: bool = True,
    ):
        self.workers = workers
        self.max_queue = max_queue
        self.cooldown_seconds = cooldown_seconds
        # On graceful shutdown, pending Tier 2 alerts fire rather than vanish:
        # a process restart is not a human deciding "don't warn them".
        self.flush_pending_on_stop = flush_pending_on_stop

        self._queue: Optional[asyncio.Queue] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._pending: Dict[str, Alert] = {}
        self._timers: Dict[str, asyncio.Task] = {}
        self._by_id: Dict[str, Alert] = {}
        self._history: Deque[Alert] = deque(maxlen=history_size)
        self._last_dispatch: Dict[str, tuple[RiskLevel, datetime]] = {}
        self._subscribers: List[asyncio.Queue] = []
        self._running = False

    # -- lifecycle --

    async def start(self) -> None:
        if self._running:
            return
        self._queue = asyncio.Queue(maxsize=self.max_queue)
        self._worker_tasks = [
            asyncio.create_task(self._worker(i), name=f"alert-worker-{i}")
            for i in range(self.workers)
        ]
        self._running = True
        logger.info("alert dispatcher started (%s workers)", self.workers)

    async def stop(self, drain_timeout: float = 10.0) -> None:
        if not self._running:
            return
        self._running = False

        async with self._lock:
            pending = list(self._pending.values())
        for alert in pending:
            if alert.tier is AlertTier.TIER_2_VETO and self.flush_pending_on_stop:
                logger.warning(
                    "shutting down with alert %s (ward=%s) still in its veto window — "
                    "dispatching now, since a restart is not a human veto",
                    alert.alert_id, alert.ward_id,
                )
                await self._fire_now(alert)
            else:
                logger.warning(
                    "shutting down with alert %s (ward=%s) unresolved in status %s",
                    alert.alert_id, alert.ward_id, alert.status.value,
                )

        for task in self._timers.values():
            task.cancel()
        self._timers.clear()

        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
            except asyncio.TimeoutError:
                logger.error("dispatch queue did not drain within %ss", drain_timeout)

        for task in self._worker_tasks:
            task.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        logger.info("alert dispatcher stopped")

    # -- submission --

    async def submit(self, alert: Alert) -> Optional[Alert]:
        """Route a freshly built alert. Returns None if it was suppressed.

        Suppression rules:
          * an existing pending alert of equal-or-higher severity for the ward
            wins (no duplicate countdowns);
          * a higher-severity alert SUPERSEDES the pending one;
          * a same-or-lower severity alert within the cooldown of a recent
            dispatch is dropped. Escalation always gets through.
        """
        events: List[tuple[str, Alert]] = []
        now = datetime.now(timezone.utc)

        async with self._lock:
            for existing in list(self._pending.values()):
                if existing.ward_id != alert.ward_id:
                    continue
                if risk_rank(alert.risk_level) > risk_rank(existing.risk_level):
                    self._cancel_timer(existing.alert_id)
                    existing.status = AlertStatus.SUPERSEDED
                    self._pending.pop(existing.alert_id, None)
                    self._archive(existing)
                    events.append(("alert.superseded", existing))
                else:
                    logger.info(
                        "suppressing alert for ward=%s (%s): pending %s already covers it",
                        alert.ward_id, alert.risk_level.value, existing.alert_id,
                    )
                    return None

            last = self._last_dispatch.get(alert.ward_id)
            if (
                last is not None
                and risk_rank(alert.risk_level) <= risk_rank(last[0])
                and (now - last[1]).total_seconds() < self.cooldown_seconds
            ):
                logger.info(
                    "suppressing alert for ward=%s (%s): %s dispatched %.0fs ago, cooldown %ss",
                    alert.ward_id, alert.risk_level.value, last[0].value,
                    (now - last[1]).total_seconds(), self.cooldown_seconds,
                )
                return None

            self._by_id[alert.alert_id] = alert

            if alert.tier is AlertTier.TIER_1_AUTO:
                alert.status = AlertStatus.QUEUED
                self._pending[alert.alert_id] = alert
                self._enqueue(alert)
                events.append(("alert.queued", alert))
            elif alert.tier is AlertTier.TIER_2_VETO:
                alert.status = AlertStatus.PENDING_VETO
                self._pending[alert.alert_id] = alert
                self._timers[alert.alert_id] = asyncio.create_task(
                    self._veto_timer(alert), name=f"veto-{alert.alert_id}"
                )
                events.append(("alert.pending_veto", alert))
            else:
                alert.status = AlertStatus.AWAITING_REVIEW
                self._pending[alert.alert_id] = alert
                events.append(("alert.awaiting_review", alert))

        for event, payload in events:
            await self._publish(event, payload)
        return alert

    # -- human actions --

    async def veto(self, alert_id: str, actor: str, reason: Optional[str] = None) -> AlertActionResult:
        """Cancel a Tier 2 alert during its countdown."""
        async with self._lock:
            alert = self._by_id.get(alert_id)
            if alert is None:
                return AlertActionResult(ok=False, alert_id=alert_id, detail="unknown alert id")
            if alert.status is not AlertStatus.PENDING_VETO:
                return AlertActionResult(
                    ok=False,
                    alert_id=alert_id,
                    status=alert.status,
                    alert=alert,
                    detail=(
                        f"too late — alert is already '{alert.status.value}'"
                        if alert.status in (
                            AlertStatus.DISPATCHING,
                            AlertStatus.DISPATCHED,
                            AlertStatus.PARTIALLY_DISPATCHED,
                            AlertStatus.QUEUED,
                        )
                        else f"alert is not in a veto window (status '{alert.status.value}')"
                    ),
                )
            self._cancel_timer(alert_id)
            alert.status = AlertStatus.VETOED
            alert.vetoed_at = datetime.now(timezone.utc)
            alert.acted_by = actor
            alert.action_reason = reason
            self._pending.pop(alert_id, None)
            self._archive(alert)

        logger.info("alert %s VETOED by %s (%s)", alert_id, actor, reason or "no reason given")
        await self._publish("alert.vetoed", alert)
        return AlertActionResult(
            ok=True, alert_id=alert_id, status=alert.status, alert=alert,
            detail="alert cancelled; nothing was sent",
        )

    async def approve(self, alert_id: str, actor: str, reason: Optional[str] = None) -> AlertActionResult:
        """Dispatch a Tier 3 alert that a human has reviewed and accepted."""
        async with self._lock:
            alert = self._by_id.get(alert_id)
            if alert is None:
                return AlertActionResult(ok=False, alert_id=alert_id, detail="unknown alert id")
            if alert.status is not AlertStatus.AWAITING_REVIEW:
                return AlertActionResult(
                    ok=False, alert_id=alert_id, status=alert.status, alert=alert,
                    detail=f"alert is not awaiting review (status '{alert.status.value}')",
                )
            alert.status = AlertStatus.QUEUED
            alert.acted_by = actor
            alert.action_reason = reason
            self._enqueue(alert)

        logger.info("alert %s APPROVED by %s", alert_id, actor)
        await self._publish("alert.approved", alert)
        return AlertActionResult(
            ok=True, alert_id=alert_id, status=alert.status, alert=alert,
            detail="alert queued for dispatch",
        )

    async def dismiss(self, alert_id: str, actor: str, reason: Optional[str] = None) -> AlertActionResult:
        """Reject a Tier 3 alert without sending anything."""
        async with self._lock:
            alert = self._by_id.get(alert_id)
            if alert is None:
                return AlertActionResult(ok=False, alert_id=alert_id, detail="unknown alert id")
            if alert.status is not AlertStatus.AWAITING_REVIEW:
                return AlertActionResult(
                    ok=False, alert_id=alert_id, status=alert.status, alert=alert,
                    detail=f"alert is not awaiting review (status '{alert.status.value}')",
                )
            alert.status = AlertStatus.DISMISSED
            alert.acted_by = actor
            alert.action_reason = reason
            self._pending.pop(alert_id, None)
            self._archive(alert)

        logger.info("alert %s DISMISSED by %s (%s)", alert_id, actor, reason or "no reason given")
        await self._publish("alert.dismissed", alert)
        return AlertActionResult(
            ok=True, alert_id=alert_id, status=alert.status, alert=alert,
            detail="alert dismissed; nothing was sent",
        )

    # -- reads --

    def get(self, alert_id: str) -> Optional[Alert]:
        return self._by_id.get(alert_id)

    def active(self, ward_id: Optional[str] = None) -> List[Alert]:
        alerts = [a for a in self._pending.values() if ward_id is None or a.ward_id == ward_id]
        return sorted(alerts, key=lambda a: a.generated_at, reverse=True)

    def history(self, ward_id: Optional[str] = None, limit: int = 100) -> List[Alert]:
        alerts = [a for a in self._history if ward_id is None or a.ward_id == ward_id]
        return sorted(alerts, key=lambda a: a.generated_at, reverse=True)[:limit]

    # -- pub/sub for the dashboard websocket --

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _publish(self, event: str, alert: Alert) -> None:
        payload = {"event": event, "alert": alert.model_dump(mode="json")}
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # A slow dashboard client must not slow down dispatch.
                logger.warning("dropping event for a slow websocket subscriber")

    # -- internals --

    def _archive(self, alert: Alert) -> None:
        """Move an alert into bounded history, evicting the id index in step so
        neither structure grows without limit."""
        if self._history.maxlen is not None and len(self._history) == self._history.maxlen:
            oldest = self._history[0]
            self._by_id.pop(oldest.alert_id, None)
        self._history.append(alert)

    def _cancel_timer(self, alert_id: str) -> None:
        task = self._timers.pop(alert_id, None)
        if task is not None:
            task.cancel()

    def _enqueue(self, alert: Alert) -> None:
        """Caller must hold the lock."""
        if self._queue is None:
            raise RuntimeError("dispatcher not started — call `await dispatcher.start()`")
        try:
            self._queue.put_nowait(alert)
        except asyncio.QueueFull:
            # Never silently drop a warning. Log loud enough to page someone.
            logger.critical(
                "DISPATCH QUEUE FULL (%s) — alert %s for ward %s was not queued",
                self.max_queue, alert.alert_id, alert.ward_id,
            )
            alert.status = AlertStatus.FAILED

    async def _veto_timer(self, alert: Alert) -> None:
        """Sleep out the veto window, then fire unless a human intervened."""
        try:
            deadline = alert.veto_deadline or datetime.now(timezone.utc)
            delay = (deadline - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        async with self._lock:
            # Re-check under the lock: a veto may have landed in the last
            # milliseconds and already moved this alert on.
            if alert.status is not AlertStatus.PENDING_VETO:
                return
            alert.status = AlertStatus.QUEUED
            self._timers.pop(alert.alert_id, None)
            self._enqueue(alert)

        logger.info(
            "veto window expired for alert %s (ward=%s) — auto-dispatching",
            alert.alert_id, alert.ward_id,
        )
        await self._publish("alert.auto_dispatch", alert)

    async def _fire_now(self, alert: Alert) -> None:
        async with self._lock:
            if alert.status is not AlertStatus.PENDING_VETO:
                return
            self._cancel_timer(alert.alert_id)
            alert.status = AlertStatus.QUEUED
            self._enqueue(alert)

    async def _worker(self, index: int) -> None:
        assert self._queue is not None
        while True:
            alert = await self._queue.get()
            try:
                async with self._lock:
                    if alert.status is not AlertStatus.QUEUED:
                        continue  # vetoed or superseded between queue and worker
                    alert.status = AlertStatus.DISPATCHING

                results = await dispatch_async(alert)

                async with self._lock:
                    failed = [c for c, ok in results.items() if not ok]
                    succeeded = [c for c, ok in results.items() if ok]
                    alert.failed_channels = failed
                    if succeeded and not failed:
                        alert.status = AlertStatus.DISPATCHED
                    elif succeeded:
                        alert.status = AlertStatus.PARTIALLY_DISPATCHED
                    else:
                        alert.status = AlertStatus.FAILED
                    if succeeded:
                        alert.dispatched_at = datetime.now(timezone.utc)
                        self._last_dispatch[alert.ward_id] = (
                            alert.risk_level, alert.dispatched_at,
                        )
                    self._pending.pop(alert.alert_id, None)
                    self._archive(alert)

                if alert.status is AlertStatus.FAILED:
                    logger.critical(
                        "alert %s for ward %s FAILED on every channel — escalate manually",
                        alert.alert_id, alert.ward_id,
                    )
                await self._publish("alert.dispatched", alert)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("alert worker %s crashed handling %s", index, alert.alert_id)
            finally:
                self._queue.task_done()
