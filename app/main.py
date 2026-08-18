"""Application entry point and lifecycle management.

Startup order:

    load config -> validate runtime -> initialize logging -> initialize
    database -> start application services -> stay alive

Shutdown order (on SIGINT / SIGTERM):

    stop background loops -> flush pending work -> close HTTP sessions ->
    close WebSockets -> commit database state -> close database -> exit
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Optional

from app.alerts.dispatcher import AlertDispatcher
from app.alerts.formatter import format_listing_alert
from app.alerts.service import AlertService
from app.alerts.state_machine import AlertStateMachine
from app.bybit.rest import BybitError, BybitRestClient
from app.bybit.websocket import WebSocketManager
from app.config import Settings, load_settings, validate_runtime
from app.health.monitor import HealthMonitor
from app.logging_config import setup_logging
from app.market.deduplication import aggregate_qualifying
from app.market.discovery import InstrumentDiscovery, InstrumentRegistry
from app.market.listing import ListingTracker
from app.market.momentum import MomentumEngine, MomentumEvaluator, SpotHistory
from app.market.price_engine import PriceEngine, TickerPollService
from app.persistence.database import Database
from app.persistence.migrations import apply_migrations
from app.persistence.repository import (
    AlertStateRepository,
    InstrumentRepository,
    ListingEventRepository,
    PriceSampleRepository,
    Repository,
)
from app.telegram.client import TelegramClient

logger = logging.getLogger("bybit_monitor.app")


class Application:
    """Top-level application orchestrator."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self.db: Optional[Database] = None
        self.stop_event: asyncio.Event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.rest: Optional[BybitRestClient] = None
        self.telegram: Optional[TelegramClient] = None
        self.dispatcher: Optional[AlertDispatcher] = None
        self.registry: Optional[InstrumentRegistry] = None
        self.discovery: Optional[InstrumentDiscovery] = None
        self.price_engine: Optional[PriceEngine] = None
        self.ws_manager: Optional[WebSocketManager] = None
        self.alert_service: Optional[AlertService] = None
        self.health: Optional[HealthMonitor] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        validate_runtime(self.config)
        logger.info("event=config_validated")
        self.db = Database(self.config.database_path)
        await self.db.connect()
        await apply_migrations(self.db)
        await self.db.commit()
        logger.info("event=database_initialized path=%s", self.config.database_path)
        self._install_signal_handlers()
        await self._start_services()
        logger.info("event=application_started")

    async def run(self) -> None:
        try:
            await self.start()
            await self.stop_event.wait()
        finally:
            # asyncio.shield protects shutdown from the cancellation that
            # delivered a stop signal, guaranteeing the database and any
            # non-daemon worker threads are cleaned up before exit.
            await asyncio.shield(self.shutdown())
            logger.info("event=application_stopped")

    async def shutdown(self) -> None:
        logger.info("event=shutdown_started")
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._stop_services()
        if self.db is not None:
            await self.db.commit()
            await self.db.close()
            self.db = None
        logger.info("event=shutdown_complete")

    # ------------------------------------------------------------------
    # Service wiring
    # ------------------------------------------------------------------
    async def _start_services(self) -> None:
        cfg = self.config
        assert self.db is not None

        self.rest = BybitRestClient(
            base_url=cfg.bybit_base_url,
            timeout=cfg.rest_timeout_seconds,
            max_retries=cfg.rest_max_retries,
        )

        self.telegram = TelegramClient(cfg)
        self.dispatcher = AlertDispatcher(self.telegram, Repository(self.db), cfg)
        await self.dispatcher.start()
        logger.info("event=dispatcher_started")

        self.registry = InstrumentRegistry(InstrumentRepository(self.db))
        self.price_engine = PriceEngine(cfg)

        self.listing_tracker = ListingTracker(
            ListingEventRepository(self.db),
            cfg,
            notify=self._notify_listing,
        )
        await self.listing_tracker.reconcile_unsent()

        self.discovery = InstrumentDiscovery(
            self.rest,
            self.registry,
            cfg,
            on_events=self._on_discovery_events,
        )
        self.ws_manager = WebSocketManager(
            self.registry,
            self.price_engine,
            cfg,
            on_reconnect=self._on_ws_reconnect,
        )

        self.momentum = MomentumEngine(
            cfg,
            SpotHistory(
                PriceSampleRepository(self.db),
                sample_seconds=cfg.spot_sample_seconds,
            ),
        )
        self.evaluator = MomentumEvaluator(
            self.registry, self.price_engine, self.momentum, cfg
        )
        self.alert_service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(self.db)),
            self.dispatcher,
            cfg,
        )

        self.health = HealthMonitor(
            cfg,
            discovery=self.discovery,
            ws_manager=self.ws_manager,
            telegram=self.telegram,
            dispatcher=self.dispatcher,
            database=self.db,
            registry=self.registry,
        )

        # First discovery seeds the registry and its subscriptions, so the
        # monitor has a market universe from the very first cycle.
        if cfg.enable_websocket or cfg.rest_fallback_enabled:
            try:
                await self.discovery.discover_once()
            except BybitError:
                logger.warning("event=initial_discovery_failed")

        if cfg.enable_websocket:
            for task in await self.ws_manager.start(self.stop_event):
                self._tasks.append(task)
            await self.ws_manager.sync_subscriptions()

        if cfg.rest_fallback_enabled:
            self.ticker_poll = TickerPollService(
                self.rest, self.registry, self.price_engine, cfg
            )
            self._tasks.append(
                self._spawn(self.ticker_poll.run_forever(self.stop_event))
            )
        self._tasks.append(self._spawn(self._market_loop()))
        self._tasks.append(self._spawn(self.discovery.run_forever(self.stop_event)))
        self._tasks.append(self._spawn(self.health.run_forever(self.stop_event)))
        if cfg.listing_notifications_enabled:
            self._tasks.append(self._spawn(self._announcement_loop()))

    async def _stop_services(self) -> None:
        if self.ws_manager is not None:
            await self.ws_manager.close()
        if self.dispatcher is not None:
            await self.dispatcher.stop(timeout=30.0)
        if self.telegram is not None:
            await self.telegram.close()
        if self.rest is not None:
            await self.rest.close()
        logger.info("event=services_stopped")

    # ------------------------------------------------------------------
    # Background loops and callbacks
    # ------------------------------------------------------------------
    async def _market_loop(self) -> None:
        cfg = self.config
        logger.info(
            "event=market_loop_started interval=%.1fs", cfg.rest_ticker_poll_seconds
        )
        while not self.stop_event.is_set():
            try:
                values = await self.evaluator.evaluate_all()
                qualifying = aggregate_qualifying(
                    values, cfg.alert_threshold_percent
                )
                decision = await self.alert_service.process(qualifying)
                if self.health is not None:
                    self.health.set_qualifying_count(decision.qualifying_count)
                await self.momentum.spot_history.prune(
                    int(time.time()), cfg.spot_history_retention_seconds
                )
            except BybitError as exc:
                logger.warning("event=market_loop_rest_error error=%s", type(exc).__name__)
            except Exception:
                logger.exception("event=market_loop_error")
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=cfg.rest_ticker_poll_seconds
                )
            except asyncio.TimeoutError:
                continue

    async def _announcement_loop(self) -> None:
        cfg = self.config
        logger.info(
            "event=announcement_loop_started interval=%.0fs",
            cfg.announcement_refresh_seconds,
        )
        while not self.stop_event.is_set():
            try:
                announcements = await self.rest.get_announcements(limit=50)
                await self.listing_tracker.handle_announcements(announcements)
            except BybitError as exc:
                logger.warning(
                    "event=announcement_fetch_failed error=%s", type(exc).__name__
                )
            except Exception:
                logger.exception("event=announcement_loop_error")
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=cfg.announcement_refresh_seconds
                )
            except asyncio.TimeoutError:
                continue

    async def _on_discovery_events(self, result) -> None:
        await self.listing_tracker.handle_registry(result)
        await self.ws_manager.sync_subscriptions()

    def _notify_listing(self, event: dict) -> object:
        return self.dispatcher.enqueue(
            format_listing_alert(event), tag="listing"
        )

    async def _on_ws_reconnect(self, category: str) -> None:
        logger.info("event=ws_reconnect_rest_refresh category=%s", category)
        if getattr(self, "ticker_poll", None) is not None:
            await self.ticker_poll.poll_once()

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        installed: list[int] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown, sig)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows does not support loop.add_signal_handler.
                try:
                    signal.signal(sig, self.request_shutdown)
                    installed.append(sig)
                except (ValueError, OSError):
                    logger.warning("event=signal_handler_unavailable signal=%s", sig)
        logger.info("event=signal_handlers_installed signals=%r", installed)

    def request_shutdown(self, signum: Optional[int] = None, *args) -> None:  # noqa: ARG002
        """Request a graceful shutdown.

        Sets the stop event synchronously. Signal handlers run on the main
        thread (Windows fallback) or are delivered through the event loop
        (POSIX ``loop.add_signal_handler``), so a direct ``Event.set()`` is
        safe in both cases.
        """
        logger.info(
            "event=shutdown_requested signal=%s",
            signum if signum is not None else "internal",
        )
        self.stop_event.set()

    # ------------------------------------------------------------------
    # Task helpers
    # ------------------------------------------------------------------
    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task


def main() -> None:
    config = load_settings()
    setup_logging(
        config.log_level,
        secrets=[config.telegram_bot_token, config.telegram_chat_id],
    )
    try:
        asyncio.run(Application(config).run())
    except KeyboardInterrupt:
        logger.info("event=interrupted_by_keyboard")


if __name__ == "__main__":
    main()