"""Phase 1 - configuration tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, validate_runtime
from tests.conftest import make_settings


class TestConfigBasics:
    def test_defaults_match_plan(self):
        s = make_settings()
        assert s.bybit_base_url == "https://api.bybit.com"
        assert s.alert_threshold_percent == 5.0
        assert s.min_qualifying_coins == 1
        assert s.max_qualifying_coins == 3
        assert s.instrument_refresh_seconds == 300.0
        assert s.rest_ticker_poll_seconds == 10.0
        assert s.spot_sample_seconds == 60.0
        assert s.immediate_transition_alerts is True
        assert s.hourly_active_alerts is True
        assert s.alert_debounce_seconds == 20.0
        assert s.composition_change_cooldown_seconds == 300.0
        assert s.enable_spot is True
        assert s.enable_linear_usdt is True
        assert s.enable_linear_usdc is True
        assert s.enable_inverse is False

    def test_valid_environment_loads(self, monkeypatch):
        monkeypatch.setenv("BYBIT_BASE_URL", "https://api-testnet.bybit.com")
        monkeypatch.setenv("ALERT_THRESHOLD_PERCENT", "6.5")
        monkeypatch.setenv("MAX_QUALIFYING_COINS", "2")
        monkeypatch.setenv("ALERT_DEBOUNCE_SECONDS", "30")
        s = make_settings()
        assert s.bybit_base_url == "https://api-testnet.bybit.com"
        assert s.alert_threshold_percent == 6.5
        assert s.max_qualifying_coins == 2
        assert s.alert_debounce_seconds == 30.0

    def test_invalid_numeric_configuration_rejected(self):
        with pytest.raises(ValidationError):
            make_settings(alert_threshold_percent="not-a-number")
        with pytest.raises(ValidationError):
            make_settings(rest_ticker_poll_seconds="abc")
        with pytest.raises(ValidationError):
            make_settings(instrument_refresh_seconds="NaN")
        with pytest.raises(ValidationError):
            make_settings(max_qualifying_coins="five")

    def test_threshold_must_be_strictly_positive(self):
        with pytest.raises(ValidationError):
            make_settings(alert_threshold_percent=0.0)
        with pytest.raises(ValidationError):
            make_settings(alert_threshold_percent=-3.0)

    def test_qualifying_range_validation(self):
        with pytest.raises(ValidationError):
            make_settings(min_qualifying_coins=4, max_qualifying_coins=3)

    def test_non_negative_durations(self):
        with pytest.raises(ValidationError):
            make_settings(alert_debounce_seconds=-1.0)
        with pytest.raises(ValidationError):
            make_settings(rest_ticker_poll_seconds=0.0)

    def test_at_least_one_market_universe_enabled(self):
        with pytest.raises(ValidationError):
            make_settings(
                enable_spot=False,
                enable_linear_usdt=False,
                enable_linear_usdc=False,
            )


class TestTelegramCredentialValidation:
    def test_missing_token_fails_clearly_when_alerts_enabled(self):
        s = make_settings(
            telegram_bot_token="",
            telegram_chat_id="-1001234567890",
            immediate_transition_alerts=True,
            hourly_active_alerts=True,
            listing_notifications_enabled=False,
        )
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            validate_runtime(s)

    def test_missing_chat_id_fails_clearly(self):
        s = make_settings(
            telegram_bot_token="123456:AAfake",
            telegram_chat_id="",
            immediate_transition_alerts=True,
            hourly_active_alerts=False,
            listing_notifications_enabled=False,
        )
        with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
            validate_runtime(s)

    def test_monitoring_only_mode_allowed_without_credentials(self):
        s = make_settings(
            telegram_bot_token="",
            telegram_chat_id="",
            immediate_transition_alerts=False,
            hourly_active_alerts=False,
            listing_notifications_enabled=False,
        )
        validate_runtime(s)  # should not raise

    def test_valid_credentials_pass(self):
        s = make_settings(telegram_bot_token="123456:AAfake", telegram_chat_id="-1001")
        validate_runtime(s)  # should not raise

    def test_no_secret_defaults(self):
        s = make_settings()
        assert s.telegram_bot_token == ""
        assert s.telegram_chat_id == ""
