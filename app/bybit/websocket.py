"""Bybit public WebSocket live-price feed (Phase 10).

Separate connections are maintained for Spot and Linear. Ticker topics
are generated from the instrument registry and (re)subscribed in batches
within Bybit's limits. Reconnects use bounded exponential backoff, and a
stale-stream watchdog triggers reconnect + REST reconciliation so the
REST feed remains the fallback and discovery/recovery logic is never
replaced.

Delta handling: partial ``delta`` updates merge into the existing ticker
snapshot; they never replace it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Iterable, Optional

import websockets
from websockets.asyncio.client import connect as ws_connect

from app.bybit.normalizer import parse_ticker
from app.config import Settings
from app.market.discovery import InstrumentRegistry
from app.market.price_engine import PriceEngine

logger = logging.getLogger("bybit_monitor.bybit.websocket")

PING_OP = {"op": "ping"}
SUBSCRIBE_OP = "subscribe"
MAX_RECONNECT_BACKOFF = 60.0


async def _maybe_await(callback_result: object) -> None:
    if asyncio.iscoroutine(callback_result):
        await callback_result


class BybitWebSocketClient:
    """One persistent WebSocket connection for one category."""

    def __init__(
        self,
        category: str,
        config: Settings,
        on_message: Callable[[dict[str, Any]], None],
        on_status: Optional[Callable[[bool, str], None]] = None,
        on_connected: Optional[Callable[[], object]] = None,
    ) -> None:
        self.category = category
        self.config = config
        self.on_message = on_message
        self.on_status = on_status
        self.on_connected = on_connected
        self._symbols: set[str] = set()
        self._subscribed: set[str] = set()
        self._connect_attempts = 0
        # Epoch wall-clock timestamp of the last received message (health).
        self._last_message_at = 0.0
        # Monotonic timestamp of the last received message (stale watchdog).
        self._last_message_monotonic = 0.0
        self._last_ping_at = 0.0
        self.connected = False
        self._ws: Any = None

    @property
    def url(self) -> str:
        return f"{self.config.bybit_ws_base_url}/{self.category}"

    @property
    def last_message_at(self) -> float:
        """Epoch timestamp of the last received message (0 = never)."""
        return self._last_message_at

    @property
    def last_message_monotonic(self) -> float:
        """Monotonic timestamp of the last received message (0 = never)."""
        return self._last_message_monotonic

    def set_symbols(self, symbols: Iterable[str]) -> None:
        """Update the desired subscription set (managed by the registry)."""
        self._symbols = set(symbols)

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------
    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info("event=ws_loop_started stream=%s url=%s", self.category, self.url)
        while not stop_event.is_set():
            try:
                await self._connect_and_listen(stop_event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "event=ws_error stream=%s error=%s",
                    self.category,
                    type(exc).__name__,
                )
            if stop_event.is_set():
                break
            delay = self._backoff_delay()
            logger.warning(
                "event=ws_reconnect stream=%s attempt=%d delay=%.1fs",
                self.category,
                self._connect_attempts,
                delay,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _backoff_delay(self) -> float:
        return min(MAX_RECONNECT_BACKOFF, 2 ** min(max(self._connect_attempts, 1), 6))

    async def _connect_and_listen(self, stop_event: asyncio.Event) -> None:
        self._connect_attempts += 1
        self._set_status(False)
        async with ws_connect(
            self.url, ping_interval=None, close_timeout=5.0
        ) as ws:
            self._connect_attempts = 0
            self.connected = True
            self._ws = ws
            self._subscribed = set()
            self._last_message_at = time.time()
            self._last_message_monotonic = time.monotonic()
            self._last_ping_at = time.monotonic()
            self._set_status(True)
            logger.info("event=ws_connected stream=%s", self.category)
            await self.resubscribe()
            if self.on_connected is not None:
                await _maybe_await(self.on_connected())
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    if self._symbols and (
                        time.monotonic() - self._last_message_monotonic
                        > self.config.ws_stale_seconds
                    ):
                        logger.warning(
                            "event=ws_stale stream=%s", self.category
                        )
                        self._set_status(False, reason="stale")
                        return
                    if (
                        time.monotonic() - self._last_ping_at
                        >= self.config.ws_heartbeat_interval_seconds
                    ):
                        await ws.send(json.dumps(PING_OP))
                        self._last_ping_at = time.monotonic()
                    continue
                try:
                    self._handle_raw(raw)
                except Exception:
                    logger.exception(
                        "event=ws_message_error stream=%s", self.category
                    )
            self.connected = False
            self._ws = None

    def _set_status(self, connected: bool, reason: str = "") -> None:
        self.connected = connected
        if self.on_status is not None:
            self.on_status(connected, reason)

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------
    async def resubscribe(self) -> None:
        """Subscribe to any desired symbols not yet subscribed (batched)."""
        if self._ws is None:
            return
        missing = sorted(self._symbols - self._subscribed)
        if not missing:
            return
        await self._send_topic_ops(SUBSCRIBE_OP, missing)

    async def unsubscribe(self, symbols: Iterable[str]) -> None:
        """Unsubscribe from symbols no longer desired (batched)."""
        if self._ws is None:
            return
        stale = sorted(set(symbols) & self._subscribed)
        if not stale:
            return
        await self._send_topic_ops("unsubscribe", stale)

    async def _send_topic_ops(self, op: str, symbols: Iterable[str]) -> None:
        symbol_list = list(symbols)
        batch_size = self.config.ws_subscribe_batch_size
        for start in range(0, len(symbol_list), batch_size):
            batch = symbol_list[start : start + batch_size]
            topics = [f"tickers.{symbol}" for symbol in batch]
            await self._ws.send(json.dumps({"op": op, "args": topics}))
            if op == SUBSCRIBE_OP:
                self._subscribed.update(batch)
            else:
                self._subscribed.difference_update(batch)
        logger.info(
            "event=ws_%s stream=%s topics=%d", op, self.category, len(symbol_list)
        )

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------
    def _handle_raw(self, raw: str) -> None:
        data = json.loads(raw)
        if data.get("op") == "pong":
            self._last_message_at = time.time()
            self._last_message_monotonic = time.monotonic()
            return
        topic = data.get("topic") or ""
        if topic.startswith("tickers."):
            symbol = topic[len("tickers.") :]
            self._last_message_at = time.time()
            self._last_message_monotonic = time.monotonic()
            self.on_message(
                {
                    "category": self.category,
                    "symbol": symbol,
                    "type": data.get("type"),
                    "ts": data.get("ts"),
                    "data": data.get("data") or {},
                }
            )


class WebSocketManager:
    """Runs the Spot and Linear streams into the shared price engine."""

    def __init__(
        self,
        registry: InstrumentRegistry,
        price_engine: PriceEngine,
        config: Settings,
        on_status: Optional[Callable[[str, bool, str], None]] = None,
        on_reconnect: Optional[Callable[[str], object]] = None,
    ) -> None:
        self.price_engine = price_engine
        self.registry = registry
        self.config = config
        self.on_status = on_status
        self.on_reconnect = on_reconnect

        def message_handler(category: str):
            def handle(payload: dict[str, Any]) -> None:
                ts_ms = payload.get("ts")
                if payload.get("type") == "snapshot":
                    ticker = parse_ticker(category, payload["data"], ts_ms=ts_ms)
                    self.price_engine.apply_snapshot(ticker)
                else:
                    self.price_engine.update_from_delta(
                        category, payload["symbol"], payload["data"], ts_ms=ts_ms
                    )

            return handle

        def status_handler(category: str):
            def handle(connected: bool, reason: str) -> None:
                if self.on_status is not None:
                    self.on_status(category, connected, reason)

            return handle

        self.clients: dict[str, BybitWebSocketClient] = {
            category: BybitWebSocketClient(
                category,
                config,
                on_message=message_handler(category),
                on_status=status_handler(category),
                on_connected=self._on_connected(category),
            )
            for category in ("spot", "linear")
        }

    def _on_connected(self, category: str):
        def callback() -> None:
            if self.on_reconnect is not None:
                return _maybe_await(self.on_reconnect(category))
            return None

        return callback

    async def sync_subscriptions(self) -> int:
        """Reconcile ticker topics against the registry for both streams.

        Filters: Trading status only; Spot topics require ``enable_spot``;
        Linear symbols are subscribed only when their settle coin matches
        an enabled linear flag. Symbols that are no longer desired are
        unsubscribed so stale subscriptions cannot accumulate.
        """
        instruments = await self.registry.repo.load_all()
        total = 0
        for category, client in self.clients.items():
            desired = set()
            for inst in instruments.values():
                if inst.category != category or inst.status != "Trading":
                    continue
                if category == "spot":
                    if self.config.enable_spot:
                        desired.add(inst.symbol)
                elif inst.settle_coin == "USDT":
                    if self.config.enable_linear_usdt:
                        desired.add(inst.symbol)
                elif inst.settle_coin == "USDC":
                    if self.config.enable_linear_usdc:
                        desired.add(inst.symbol)
            previous = client._subscribed
            client.set_symbols(desired)
            await client.resubscribe()
            await client.unsubscribe(previous - desired)
            total += len(desired)
        return total

    async def start(self, stop_event: asyncio.Event) -> list[asyncio.Task]:
        tasks = []
        if self.config.enable_websocket:
            for category in ("spot", "linear"):
                client = self.clients[category]
                tasks.append(asyncio.create_task(client.run(stop_event)))
        return tasks

    async def close(self) -> None:
        for client in self.clients.values():
            client._ws = None  # let the loop exit cleanly
            client.connected = False


__all__ = ["BybitWebSocketClient", "WebSocketManager"]