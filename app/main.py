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
from typing import Optional

from app.config import Settings, load_settings, validate_runtime
from app.logging_config import setup_logging
from app.persistence.database import Database
from app.persistence.migrations import apply_migrations

logger = logging.getLogger("bybit_monitor.app")


class Application:
    """Top-level application orchestrator."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self.db: Optional[Database] = None
        self.stop_event: asyncio.Event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

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
    # Service hooks (overridden / extended in later phases)
    # ------------------------------------------------------------------
    async def _start_services(self) -> None:
        """Start background services (discovery, polling, dispatcher...)."""
        return None

    async def _stop_services(self) -> None:
        """Stop background services in a deterministic order."""
        return None

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
