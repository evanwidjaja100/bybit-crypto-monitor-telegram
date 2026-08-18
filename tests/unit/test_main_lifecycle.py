"""Phase 1 - application lifecycle tests (startup / shutdown / signals)."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

import pytest

from app.config import Settings, validate_runtime
from app.main import Application
from tests.conftest import make_settings


def build_app(config: Settings) -> Application:
    # Offline wiring: no external WebSocket / REST / Telegram calls in tests.
    config = make_settings(
        _env_file=None,
        database_path=config.database_path,
        telegram_bot_token=config.telegram_bot_token,
        telegram_chat_id=config.telegram_chat_id,
        immediate_transition_alerts=config.immediate_transition_alerts,
        hourly_active_alerts=config.hourly_active_alerts,
        listing_notifications_enabled=config.listing_notifications_enabled,
        enable_websocket=False,
        rest_fallback_enabled=False,
    )
    return Application(config)


class TestStartupShutdown:
    async def test_start_initializes_database(self, config, caplog):
        caplog.set_level(logging.INFO)
        app = build_app(config)
        await app.start()
        assert app.db is not None
        assert app.db.connected
        assert Path(config.database_path).exists()
        assert "application_started" in caplog.text
        await app.shutdown()
        assert app.db is None
        assert "shutdown_complete" in caplog.text

    async def test_run_exits_cleanly_when_shutdown_requested(self, config):
        app = build_app(config)

        async def _stop_later():
            await asyncio.sleep(0.1)
            app.request_shutdown()

        stopper = asyncio.create_task(_stop_later())
        await asyncio.wait_for(app.run(), timeout=5.0)
        await stopper
        assert app.db is None
        assert app.stop_event.is_set()

    async def test_shutdown_cancels_background_tasks(self, config):
        app = build_app(config)
        cancelled = asyncio.Event()

        async def _background():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        await app.start()
        task = app._spawn(_background())
        await asyncio.sleep(0)
        await app.shutdown()
        assert cancelled.is_set()
        assert task.cancelled()

    async def test_restart_uses_existing_database(self, config):
        app1 = build_app(config)
        await app1.start()
        await app1.shutdown()

        app2 = build_app(config)
        await app2.start()
        await app2.shutdown()
        # Schema must survive restart without re-migration errors.
        assert Path(config.database_path).exists()


class TestFailureModes:
    async def test_missing_token_fails_clearly_at_startup(self):
        config = make_settings(
            telegram_bot_token="",
            telegram_chat_id="",
            immediate_transition_alerts=True,
            hourly_active_alerts=True,
            listing_notifications_enabled=True,
            database_path=":memory:",
        )
        app = build_app(config)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            await app.start()
        # start() failed before opening the DB -> still safe to shutdown
        await app.shutdown()

    async def test_missing_token_ok_in_monitoring_only_mode(self, tmp_path):
        config = make_settings(
            telegram_bot_token="",
            telegram_chat_id="",
            immediate_transition_alerts=False,
            hourly_active_alerts=False,
            listing_notifications_enabled=False,
            database_path=str(tmp_path / "monitor.sqlite"),
        )
        validate_runtime(config)  # does not raise
        app = build_app(config)
        await app.start()
        await app.shutdown()


class TestSignalHandling:
    def test_sigterm_sets_stop_event(self, config):
        async def _scenario():
            app = build_app(config)
            try:
                await app.start()
                assert not app.stop_event.is_set()
                app.request_shutdown(signal.SIGTERM)
                assert app.stop_event.is_set()
            finally:
                await app.shutdown()

        asyncio.run(_scenario())

    def test_sigint_sets_stop_event(self, config):
        async def _scenario():
            app = build_app(config)
            try:
                await app.start()
                assert not app.stop_event.is_set()
                app.request_shutdown(signal.SIGINT)
                assert app.stop_event.is_set()
            finally:
                await app.shutdown()

        asyncio.run(_scenario())
