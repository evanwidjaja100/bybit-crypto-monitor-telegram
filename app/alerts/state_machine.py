"""Alert state machine.

States:

    EMPTY        = 0 qualifying coins
    ACTIVE_RANGE = 1-3 qualifying coins
    OVER_RANGE   = 4+ qualifying coins

Transitions:

    EMPTY -> ACTIVE_RANGE       send live transition alert (debounced)
    ACTIVE_RANGE -> ACTIVE_RANGE no immediate duplicate (optional,
                                  cooldown-controlled composition update)
    ACTIVE_RANGE -> OVER_RANGE  suppress range alerts
    OVER_RANGE -> ACTIVE_RANGE  send live transition alert (debounced)
    ACTIVE_RANGE -> EMPTY       reset active state
    OVER_RANGE -> EMPTY         remain silent

All state is persisted so restarts never create duplicate alerts and the
hourly active-state message is never sent twice for the same hourly bucket.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import Settings
from app.market.deduplication import QualifyingSet
from app.persistence.repository import AlertStateRepository

STATE_EMPTY = "EMPTY"
STATE_ACTIVE_RANGE = "ACTIVE_RANGE"
STATE_OVER_RANGE = "OVER_RANGE"


def classify_state(count: int, min_coins: int, max_coins: int) -> str:
    if count < min_coins:
        return STATE_EMPTY
    if count <= max_coins:
        return STATE_ACTIVE_RANGE
    return STATE_OVER_RANGE


def hourly_bucket(ts: int) -> str:
    """UTC hourly bucket identifier (e.g. ``2026-08-18-02``)."""
    return time.strftime("%Y-%m-%d-%H", time.gmtime(ts))


@dataclass
class AlertDecision:
    """What the state machine wants done for one update cycle.

    ``live_transition`` / ``composition_update`` / ``hourly_update`` are
    the only outputs; delivery itself is handled by the dispatcher.
    """

    state: str
    qualifying_count: int
    fingerprint: tuple[str, ...]
    live_transition: bool = False
    composition_update: bool = False
    hourly_update: bool = False
    transition_reason: str = ""


class AlertStateMachine:
    """Drives the alert policy from unique-coin aggregations.

    Consumes only :class:`QualifyingSet` - raw contract counts never
    reach this class.
    """

    def __init__(self, config: Settings, repo: AlertStateRepository) -> None:
        self.config = config
        self.repo = repo
        self._state: Optional[dict[str, Any]] = None

    async def _load(self) -> dict[str, Any]:
        if self._state is None:
            self._state = await self.repo.load()
        return self._state

    async def update(
        self, qualifying: QualifyingSet, now: Optional[int] = None
    ) -> AlertDecision:
        """Evaluate and persist in one committed step (test/legacy path)."""
        decision, state = await self.evaluate(qualifying, now)
        await self._save(state)
        self._state = state
        return decision

    async def evaluate(
        self, qualifying: QualifyingSet, now: Optional[int] = None
    ) -> tuple[AlertDecision, dict[str, Any]]:
        """Compute the next decision and state WITHOUT persisting.

        Callers that persist the returned state themselves (and possibly
        other rows in the same transaction) must use :meth:`persist_no_commit`.
        State is always read fresh from the repository so a caller whose
        transaction fails can simply not persist and retry later.
        """
        now = int(now if now is not None else time.time())
        state = await self.repo.load()

        prev_state = state["state"]
        new_state = classify_state(
            qualifying.count,
            self.config.min_qualifying_coins,
            self.config.max_qualifying_coins,
        )
        fingerprint = qualifying.fingerprint
        decision = AlertDecision(
            state=new_state,
            qualifying_count=qualifying.count,
            fingerprint=fingerprint,
        )

        if new_state == STATE_ACTIVE_RANGE:
            if prev_state != STATE_ACTIVE_RANGE and state.get("pending_since") is None:
                state["pending_since"] = now
                state["pending_from"] = prev_state
            pending = state.get("pending_since")
            if pending is None:
                # Stable in the active range: only composition updates apply.
                prev_fingerprint = tuple(json.loads(state["fingerprint"]))
                if fingerprint != prev_fingerprint:
                    self._maybe_composition_update(decision, state, now)
            else:
                debounced = (
                    now - int(pending) >= int(self.config.alert_debounce_seconds)
                )
                if debounced:
                    if self.config.immediate_transition_alerts:
                        decision.live_transition = True
                        decision.transition_reason = (
                            f"{state.get('pending_from') or prev_state} -> {new_state}"
                        )
                        state["last_transition_at"] = now
                    state["pending_since"] = None
                    state["pending_from"] = None
                    # The debounced transition owns this hourly bucket.
                    state["last_hourly_bucket"] = hourly_bucket(now)
        else:
            # Leaving ACTIVE_RANGE cancels any pending transition.
            state["pending_since"] = None
            state["pending_from"] = None

        if (
            new_state == STATE_ACTIVE_RANGE
            and state.get("pending_since") is None
            and self.config.hourly_active_alerts
            and not decision.live_transition
        ):
            bucket = hourly_bucket(now)
            if state.get("last_hourly_bucket") != bucket:
                decision.hourly_update = True
                state["last_hourly_bucket"] = bucket

        state["state"] = new_state
        state["fingerprint"] = json.dumps(list(fingerprint))
        state["updated_at"] = now
        return decision, state

    def _state_fields(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": state["state"],
            "fingerprint": tuple(json.loads(state["fingerprint"])),
            "updated_at": int(state["updated_at"]),
            "last_transition_at": state.get("last_transition_at"),
            "pending_since": state.get("pending_since"),
            "pending_from": state.get("pending_from"),
            "last_hourly_bucket": state.get("last_hourly_bucket"),
            "last_composition_at": state.get("last_composition_at"),
        }

    async def persist_no_commit(self, state: dict[str, Any]) -> None:
        """Persist the state returned by :meth:`evaluate` without committing."""
        await self.repo.save_no_commit(**self._state_fields(state))

    async def _save(self, state: dict[str, Any]) -> None:
        await self.repo.save(**self._state_fields(state))

    def _maybe_composition_update(
        self, decision: AlertDecision, state: dict[str, Any], now: int
    ) -> None:
        if not self.config.composition_change_alerts:
            return
        last = state.get("last_composition_at")
        if last is not None:
            if now - int(last) < int(self.config.composition_change_cooldown_seconds):
                return
        decision.composition_update = True
        state["last_composition_at"] = now

    async def _save(self, state: dict[str, Any]) -> None:
        await self.repo.save(
            state=state["state"],
            fingerprint=tuple(json.loads(state["fingerprint"])),
            updated_at=int(state["updated_at"]),
            last_transition_at=state.get("last_transition_at"),
            pending_since=state.get("pending_since"),
            pending_from=state.get("pending_from"),
            last_hourly_bucket=state.get("last_hourly_bucket"),
            last_composition_at=state.get("last_composition_at"),
        )

    async def reset(self) -> None:
        """Force EMPTY state (used by tests / operator actions)."""
        await self._save(
            {
                "state": STATE_EMPTY,
                "fingerprint": "[]",
                "updated_at": int(time.time()),
                "last_transition_at": None,
                "pending_since": None,
                "pending_from": None,
                "last_hourly_bucket": None,
                "last_composition_at": None,
            }
        )
        self._state = None


__all__ = [
    "AlertDecision",
    "AlertStateMachine",
    "STATE_EMPTY",
    "STATE_ACTIVE_RANGE",
    "STATE_OVER_RANGE",
    "classify_state",
    "hourly_bucket",
]