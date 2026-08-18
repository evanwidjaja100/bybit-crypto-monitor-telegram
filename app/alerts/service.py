"""Alert service: decision -> formatting -> persistent queue.

Glue between the state machine (what to do) and the dispatcher (how to
deliver), following the required architecture:

    market event
    -> alert decision
    -> persistent/outgoing alert record
    -> Telegram queue
    -> Telegram dispatcher
"""

from __future__ import annotations

import logging
from typing import Optional

from app.alerts.dispatcher import AlertDispatcher
from app.alerts.formatter import format_alert
from app.alerts.state_machine import AlertDecision, AlertStateMachine
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
        """Run one alert-decision cycle and enqueue any required message."""
        decision = await self.state_machine.update(qualifying, now)
        if decision.live_transition:
            await self._enqueue(qualifying, decision, "transition", now)
        elif decision.composition_update:
            await self._enqueue(qualifying, decision, "composition", now)
        elif decision.hourly_update:
            await self._enqueue(qualifying, decision, "hourly", now)
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

    async def _enqueue(
        self,
        qualifying: QualifyingSet,
        decision: AlertDecision,
        kind: str,
        now: Optional[int],
    ) -> None:
        message = format_alert(qualifying, self.config, now, kind=kind)
        await self.dispatcher.enqueue(message, tag=kind)


__all__ = ["AlertService"]