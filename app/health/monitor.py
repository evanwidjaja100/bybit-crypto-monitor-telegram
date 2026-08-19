"""Health monitoring and observability.

A :class:`HealthMonitor` periodically collects a :class:`HealthState`
snapshot from the running subsystems and logs a readable summary block.
Structured ``event=...`` logs are emitted by each subsystem itself; this
module only aggregates them for unattended-operation visibility.

Subsystems are passed by duck typing so the monitor has no dependency on
their concrete types (tests use small stubs).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import Settings

logger = logging.getLogger("bybit_monitor.health")


@dataclass
class HealthState:
    """One health snapshot of all monitored subsystems."""

    collected_at: int = 0
    rest_healthy: bool = False
    spot_ws_connected: bool = False
    linear_ws_connected: bool = False
    telegram_healthy: bool = False
    dispatcher_healthy: bool = True
    telegram_last_error_type: Optional[str] = None
    telegram_last_error_age: Optional[int] = None
    telegram_dead_count: int = 0
    database_healthy: bool = False
    last_spot_ticker_age: Optional[int] = None
    last_linear_ticker_age: Optional[int] = None
    last_discovery_age: Optional[int] = None
    spot_instrument_count: int = 0
    linear_usdt_count: int = 0
    linear_usdc_count: int = 0
    qualifying_coin_count: int = 0
    telegram_queue_depth: int = 0
    # Container-health classification (H3):
    #   healthy   - every critical dimension is acceptable
    #   degraded  - temporary issues (Telegram, short reconnect) only
    #   unhealthy - a critical subsystem failure persisted beyond grace
    overall: str = "healthy"
    critical_issues: list[str] = field(default_factory=list)
    degraded_issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _age(now: int, last_seen: Optional[int]) -> Optional[int]:
    if last_seen is None:
        return None
    # Never negative: a slightly skewed clock must not read as "future".
    return max(0, now - int(last_seen))


class HealthMonitor:
    """Collects health state from the live subsystems."""

    def __init__(
        self,
        config: Settings,
        discovery: Optional[Any] = None,
        ws_manager: Optional[Any] = None,
        telegram: Optional[Any] = None,
        dispatcher: Optional[Any] = None,
        database: Optional[Any] = None,
        registry: Optional[Any] = None,
        repository: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.discovery = discovery
        self.ws_manager = ws_manager
        self.telegram = telegram
        self.dispatcher = dispatcher
        self.database = database
        self.registry = registry
        self.repository = repository
        self.qualifying_coin_count = 0
        self.last_state: Optional[HealthState] = None
        # First time each critical subsystem was seen bad (monotonic), so
        # a short reconnect or blip never flips container health.
        self._critical_since: dict[str, float] = {}

    def set_qualifying_count(self, count: int) -> None:
        """Report the last unique-coin qualifying count (wired by the loop)."""
        self.qualifying_coin_count = count

    async def snapshot(self, now: Optional[int] = None) -> HealthState:
        now = int(now if now is not None else time.time())
        state = HealthState(collected_at=now)
        await self._collect_substates(state, now)
        state.qualifying_coin_count = self.qualifying_coin_count
        self._classify(state)
        self.last_state = state
        return state

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------
    async def _collect_substates(self, state: HealthState, now: int) -> None:
        if self.database is not None:
            state.database_healthy = await self._database_healthy()
            if not state.database_healthy:
                state.notes.append("database unhealthy")

        if self.discovery is not None:
            last_success = getattr(self.discovery, "last_success_at", None)
            state.last_discovery_age = _age(now, last_success)
            stale = (
                state.last_discovery_age is not None
                and state.last_discovery_age > self.config.instrument_refresh_seconds * 3
            )
            state.rest_healthy = last_success is not None and not stale
            if not state.rest_healthy:
                state.notes.append("rest unhealthy")

        if self.ws_manager is not None:
            spot = getattr(self.ws_manager, "clients", {}).get("spot")
            linear = getattr(self.ws_manager, "clients", {}).get("linear")
            if spot is not None:
                state.spot_ws_connected = bool(getattr(spot, "connected", False))
                state.last_spot_ticker_age = _age(now, getattr(spot, "last_message_at", None))
                if not state.spot_ws_connected:
                    state.notes.append("spot ws disconnected")
            if linear is not None:
                state.linear_ws_connected = bool(getattr(linear, "connected", False))
                state.last_linear_ticker_age = _age(
                    now, getattr(linear, "last_message_at", None)
                )
                if not state.linear_ws_connected:
                    state.notes.append("linear ws disconnected")

        if self.telegram is not None:
            last_success = getattr(self.telegram, "last_success_at", None)
            last_error = getattr(self.telegram, "last_error_at", None)
            state.telegram_healthy = last_success is not None and (
                last_error is None or last_success >= last_error
            )
            state.telegram_last_error_type = getattr(
                self.telegram, "last_error_type", None
            )
            state.telegram_last_error_age = _age(now, last_error)
            if not state.telegram_healthy:
                state.notes.append("telegram unhealthy")

        if self.dispatcher is not None:
            depth = getattr(self.dispatcher, "depth", None)
            if callable(depth):
                state.telegram_queue_depth = await depth()
            state.dispatcher_healthy = bool(
                getattr(self.dispatcher, "worker_healthy", True)
            )
            if not state.dispatcher_healthy:
                state.notes.append("dispatcher unhealthy")
            repo = getattr(self.dispatcher, "repo", None)
            count_dead = getattr(repo, "count_dead", None)
            if callable(count_dead):
                try:
                    state.telegram_dead_count = await count_dead()
                except Exception:
                    state.telegram_dead_count = -1

        if self.registry is not None:
            await self._collect_counts(state)

    async def _collect_counts(self, state: HealthState) -> None:
        instruments = await self.registry.repo.load_all()
        for instrument in instruments.values():
            if instrument.status != "Trading":
                continue
            if instrument.category == "spot":
                state.spot_instrument_count += 1
            elif instrument.category == "linear":
                if instrument.settle_coin == "USDT":
                    state.linear_usdt_count += 1
                elif instrument.settle_coin == "USDC":
                    state.linear_usdc_count += 1

    async def _database_healthy(self) -> bool:
        try:
            row = await self.database.fetchone("SELECT 1 AS one")
            return bool(row is not None and row["one"] == 1)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Container-health classification (H3)
    # ------------------------------------------------------------------
    def _classify(self, state: HealthState) -> None:
        """Classify the snapshot into healthy / degraded / unhealthy.

        Critical dimensions: database, dispatcher, REST discovery, and
        each WebSocket stream that is enabled. A critical failure only
        counts once it has persisted beyond ``critical_health_failure_seconds``
        (grace), so short reconnects never flip container health. Temporary
        Telegram issues and a non-empty retry queue are degraded only.
        """
        critical = []
        degraded = []
        grace = self.config.critical_health_failure_seconds

        if not state.database_healthy:
            critical.append("database")

        if state.dispatcher_healthy is False:
            critical.append("dispatcher")

        if state.rest_healthy is False:
            critical.append("rest")

        enabled_spot = bool(self.config.enable_websocket and self.config.enable_spot)
        enabled_linear = bool(
            self.config.enable_websocket
            and (self.config.enable_linear_usdt or self.config.enable_linear_usdc)
        )
        if enabled_spot and not self._ws_acceptable(state.last_spot_ticker_age):
            critical.append("spot_ws")
        if enabled_linear and not self._ws_acceptable(state.last_linear_ticker_age):
            critical.append("linear_ws")
        # Non-critical degradations (never make the container unhealthy).
        if not state.telegram_healthy:
            degraded.append("telegram")
        if state.telegram_queue_depth > 0:
            degraded.append("retry_queue")

        bad = set(critical)
        for name in list(self._critical_since):
            if name not in bad:
                del self._critical_since[name]
        now_mono = time.monotonic()
        persisted = []
        for name in bad:
            since = self._critical_since.setdefault(name, now_mono)
            # Database failures are critical immediately (no grace): the
            # app cannot persist anything without the database.
            if now_mono - since >= grace or name == "database":
                persisted.append(name)
        # The disabled streams must never linger in the grace tracker.
        self._critical_since = {
            name: since
            for name, since in self._critical_since.items()
            if name in bad
        }

        if state.database_healthy is False:
            state.overall = "unhealthy"
        elif persisted:
            state.overall = "unhealthy"
        elif degraded:
            state.overall = "degraded"
        else:
            state.overall = "healthy"
        state.critical_issues = persisted
        state.degraded_issues = degraded
        if persisted:
            state.notes.append(f"critical: {','.join(sorted(persisted))}")

    def _ws_acceptable(self, last_ticker_age: Optional[int]) -> bool:
        """A stream is acceptable while its last ticker is fresher than the
        stale threshold the client itself reconnects at (``ws_stale_seconds``).
        A feed that stays dead past that threshold is a real failure; the
        grace window below decides whether it is critical yet."""
        return last_ticker_age is not None and last_ticker_age <= int(
            self.config.ws_stale_seconds
        )

    # ------------------------------------------------------------------
    # Persisted snapshot (read by the container healthcheck)
    # ------------------------------------------------------------------
    async def persist_snapshot(self, state: HealthState) -> None:
        """Write the compact health snapshot into the kv table.

        The container healthcheck script reads this row and exits non-zero
        when the heartbeat is stale or a critical failure is persisted.
        """
        if self.repository is None:
            return
        payload = {
            "last_updated_at": int(time.time()),
            "overall": state.overall,
            "database": "ok" if state.database_healthy else "fail",
            "dispatcher": "ok" if state.dispatcher_healthy else "fail",
            "rest": "ok" if state.rest_healthy else "fail",
            "spot_ws": "ok" if state.spot_ws_connected else "fail",
            "linear_ws": "ok" if state.linear_ws_connected else "fail",
            "last_discovery_age": state.last_discovery_age,
            "last_spot_ticker_age": state.last_spot_ticker_age,
            "last_linear_ticker_age": state.last_linear_ticker_age,
            "critical_issues": state.critical_issues,
        }
        await self.repository.kv_set_json("health:snapshot", payload)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    @staticmethod
    def format_summary(state: HealthState) -> str:
        def age_text(value: Optional[int]) -> str:
            return "n/a" if value is None else f"{value}s"

        def ws_text(connected: bool, age: Optional[int]) -> str:
            if not connected:
                return "disconnected"
            return f"connected ({age_text(age)} last msg)"

        telegram_line = "healthy"
        if not state.telegram_healthy:
            telegram_line = "unhealthy"
        if state.telegram_last_error_type:
            telegram_line += (
                f" (last failure: {age_text(state.telegram_last_error_age)} "
                f"ago {state.telegram_last_error_type})"
            )

        return (
            "HEALTH\n"
            "------\n"
            f"Spot instruments: {state.spot_instrument_count}\n"
            f"Linear USDT: {state.linear_usdt_count}\n"
            f"Linear USDC: {state.linear_usdc_count}\n"
            f"Spot WS: {ws_text(state.spot_ws_connected, state.last_spot_ticker_age)}\n"
            f"Linear WS: {ws_text(state.linear_ws_connected, state.last_linear_ticker_age)}\n"
            f"REST: {'healthy' if state.rest_healthy else 'unhealthy'}\n"
            f"Telegram: {telegram_line}\n"
            f"Dispatcher: {'healthy' if state.dispatcher_healthy else 'unhealthy'}\n"
            f"Database: {'healthy' if state.database_healthy else 'unhealthy'}\n"
            f"Qualifying coins: {state.qualifying_coin_count}\n"
            f"Telegram queue: {state.telegram_queue_depth}\n"
            f"Telegram dead: {state.telegram_dead_count}\n"
            f"Last discovery: {age_text(state.last_discovery_age)} ago"
        )

    # ------------------------------------------------------------------
    # Periodic loop
    # ------------------------------------------------------------------
    async def run_forever(self, stop_event: asyncio.Event) -> None:
        interval = min(
            self.config.health_summary_seconds,
            self.config.health_write_interval_seconds,
        )
        logger.info(
            "event=health_loop_started interval=%.0fs", interval
        )
        while not stop_event.is_set():
            try:
                state = await self.snapshot()
                logger.info("event=health_summary\n%s", self.format_summary(state))
                await self.persist_snapshot(state)
                if state.notes:
                    logger.warning(
                        "event=health_issues issues=%s", ",".join(state.notes)
                    )
            except Exception:
                logger.exception("event=health_summary_error")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


__all__ = ["HealthMonitor", "HealthState", "format_summary"]
