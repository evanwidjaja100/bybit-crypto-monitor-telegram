"""Alert service: decision -> (atomic) formatting + persistence -> queue.

Glue between the state machine (what to do) and the dispatcher (how to
deliver), following the required architecture:

    market event
    -> alert decision
    -> persistent/outgoing alert record
    -> Telegram queue
    -> Telegram dispatcher

State and the outgoing record are written in ONE transaction: a crash
between the two is impossible, and a failed delivery never loses the
state transition. Duplicate logical decisions are suppressed by the
partial unique index on ``outgoing_notifications.dedupe_key``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

from app.alerts.dispatcher import AlertDispatcher
from app.alerts.formatter import format_alert
from app.alerts.state_machine import (
    AlertDecision,
    AlertStateMachine,
    hourly_bucket,
)
from app.config import Settings
from app.market.deduplication import QualifyingSet

logger = logging.getLogger("bybit_monitor.alerts.service")


class AlertService:
    def __init__(
        self,
        state_machine: AlertStateMachine,
        dispatcher: AlertDispatcher,
        config: Settings,
    ) -> None:
        self.state_machine = state_machine
        self.dispatcher = dispatcher
        self.config = config

    async def process(
        self, qualifying: QualifyingSet, now: Optional[int] = None
    ) -> AlertDecision:
        """Run one alert-decision cycle and enqueue any required message.

        The state transition and the outgoing notification are persisted
        in a single transaction; the message is handed to the dispatcher
        queue only after that transaction commits.
        """
        now = int(now if now is not None else time.time())
        decision, state = await self.state_machine.evaluate(qualifying, now)
        kind = None
        message = None
        dedupe_key = None
        if decision.live_transition:
            kind = "transition"
        elif decision.composition_update:
            kind = "composition"
        elif decision.hourly_update:
            kind = "hourly"
        if kind is not None:
            message = format_alert(qualifying, self.config, now, kind=kind)
            dedupe_key = self._dedupe_key(kind, decision, now)

        repo = self.dispatcher.repo
        try:
            async with repo.db.transaction():
                await self.state_machine.persist_no_commit(state)
                notification_id = None
                if kind is not None:
                    notification_id = await repo.insert_outgoing_notification(
                        kind,
                        message,
                        dedupe_key=dedupe_key,
                        commit=False,
                    )
        except sqlite3.IntegrityError:
            # A duplicate logical decision (same kind, coins and time)
            # already owns this dedupe_key; the state write was rolled
            # back with it, which is safe: the persisted state for the
            # same cycle is identical.
            logger.warning("event=duplicate_notification_suppressed key=%s", dedupe_key)
        else:
            if notification_id is not None:
                self.dispatcher.feed(notification_id, message)

        logger.info(
            "event=alert_decision qualifying_count=%d state=%s "
            "transition=%s composition=%s hourly=%s",
            decision.qualifying_count,
            decision.state,
            decision.live_transition,
            decision.composition_update,
            decision.hourly_update,
        )
        return decision

    def _dedupe_key(
        self, kind: str, decision: AlertDecision, now: int
    ) -> str:
        coins = ":".join(decision.fingerprint)
        if kind == "transition":
            return f"transition:{int(now)}:{coins}"
        if kind == "hourly":
            return f"hourly:{hourly_bucket(now)}:{coins}"
        if kind == "composition":
            cooldown = int(self.config.composition_change_cooldown_seconds)
            return f"composition:{coins}:{int(now) // cooldown}"
        raise ValueError(f"unknown alert kind: {kind}")  # pragma: no cover


__all__ = ["AlertService"]
