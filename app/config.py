"""Configuration loading and validation.

All important runtime behaviour is configuration-driven via environment
variables / a local `.env` file (see ``.env.example``).
"""

from __future__ import annotations

from pydantic import ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_NUMERIC_NON_NEGATIVE = (
    "instrument_refresh_seconds",
    "announcement_refresh_seconds",
    "rest_ticker_poll_seconds",
    "spot_sample_seconds",
    "alert_debounce_seconds",
    "composition_change_cooldown_seconds",
    "spot_history_retention_seconds",
    "spot_anchor_tolerance_seconds",
    "rest_timeout_seconds",
    "telegram_timeout_seconds",
    "ws_heartbeat_interval_seconds",
    "ws_stale_seconds",
    "health_summary_seconds",
    "dispatcher_poll_seconds",
    "notification_max_age_seconds",
    "listing_notification_max_age_seconds",
)

_NUMERIC_POSITIVE = (
    "rest_ticker_poll_seconds",
    "rest_timeout_seconds",
    "telegram_timeout_seconds",
    "ws_heartbeat_interval_seconds",
    "ws_stale_seconds",
    "spot_history_retention_seconds",
    "dispatcher_poll_seconds",
)


class Settings(BaseSettings):
    """Application settings.

    Field names map to the environment variables documented in
    ``.env.example`` (case-insensitive). No production secret has a
    hard-coded default.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Bybit ---
    bybit_base_url: str = "https://api.bybit.com"
    bybit_ws_base_url: str = "wss://stream.bybit.com/v5/public"

    # --- Telegram (never provide defaults for these) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Alert rules ---
    alert_threshold_percent: float = 5.0
    min_qualifying_coins: int = 1
    max_qualifying_coins: int = 3

    # --- Polling / refresh intervals (seconds) ---
    instrument_refresh_seconds: float = 300.0
    announcement_refresh_seconds: float = 300.0
    rest_ticker_poll_seconds: float = 10.0
    spot_sample_seconds: float = 60.0

    # --- Alert layers ---
    immediate_transition_alerts: bool = True
    hourly_active_alerts: bool = True
    composition_change_alerts: bool = False
    listing_notifications_enabled: bool = True

    alert_debounce_seconds: float = 20.0
    composition_change_cooldown_seconds: float = 300.0

    # --- Market universe ---
    enable_spot: bool = True
    enable_linear_usdt: bool = True
    enable_linear_usdc: bool = True
    enable_inverse: bool = False
    enable_websocket: bool = True
    rest_fallback_enabled: bool = True

    # --- Storage ---
    database_path: str = "./data/bybit_monitor.sqlite"

    # --- Persistence / sampling tuning ---
    spot_history_retention_seconds: float = 7200.0  # 120 minutes
    spot_anchor_tolerance_seconds: float = 90.0  # +/- 90 seconds

    # --- Network tuning ---
    rest_timeout_seconds: float = 10.0
    rest_max_retries: int = 3
    telegram_timeout_seconds: float = 15.0
    telegram_max_retries: int = 3

    # --- WebSocket tuning ---
    ws_heartbeat_interval_seconds: float = 20.0
    ws_stale_seconds: float = 60.0
    ws_reconnect_max_attempts: int = 20
    ws_subscribe_batch_size: int = 10

    # --- Observability ---
    log_level: str = "INFO"
    health_summary_seconds: float = 60.0

    # --- Dispatcher / delivery ---
    dispatcher_poll_seconds: float = 2.0
    # Retryable alerts expire after this age; listing alerts stay longer
    # because a listing notification remains relevant.
    notification_max_age_seconds: float = 7200.0  # 2 hours
    listing_notification_max_age_seconds: float = 86400.0  # 24 hours

    @model_validator(mode="after")
    def _validate_ranges(self) -> "Settings":
        import math

        if not math.isfinite(self.alert_threshold_percent) or self.alert_threshold_percent <= 0:
            raise ValueError("ALERT_THRESHOLD_PERCENT must be a finite number > 0")
        if self.min_qualifying_coins < 0:
            raise ValueError("MIN_QUALIFYING_COINS must be >= 0")
        if self.max_qualifying_coins < self.min_qualifying_coins:
            raise ValueError("MAX_QUALIFYING_COINS must be >= MIN_QUALIFYING_COINS")
        if self.rest_max_retries < 0 or self.telegram_max_retries < 0:
            raise ValueError("retry counts must be >= 0")
        if self.ws_subscribe_batch_size < 1:
            raise ValueError("WS_SUBSCRIBE_BATCH_SIZE must be >= 1")
        for name in _NUMERIC_NON_NEGATIVE:
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name.upper()} must be a finite number >= 0")
        for name in _NUMERIC_POSITIVE:
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name.upper()} must be a finite number > 0")
        if not (self.enable_spot or self.enable_linear_usdt or self.enable_linear_usdc):
            raise ValueError("At least one market universe must be enabled")
        if self.min_qualifying_coins == 0 or self.max_qualifying_coins == 0:
            raise ValueError("MIN/MAX_QUALIFYING_COINS must be >= 1")
        return self

    def alert_delivery_configured(self) -> bool:
        """True when Telegram delivery credentials are present."""
        return bool(self.telegram_bot_token.strip()) and bool(
            self.telegram_chat_id.strip()
        )


def validate_runtime(config: Settings) -> None:
    """Fail fast when an alert layer is enabled without delivery credentials.

    Monitoring-only mode is still possible by disabling all alert layers.
    """
    layers_enabled = (
        config.immediate_transition_alerts
        or config.hourly_active_alerts
        or config.listing_notifications_enabled
    )
    if layers_enabled and not config.alert_delivery_configured():
        raise ValueError(
            "Telegram delivery is required because alert layers are enabled. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or disable "
            "IMMEDIATE_TRANSITION_ALERTS / HOURLY_ACTIVE_ALERTS / "
            "LISTING_NOTIFICATIONS_ENABLED."
        )


def load_settings() -> Settings:
    """Load settings from environment variables and the local .env file."""
    return Settings()


__all__ = [
    "Settings",
    "ValidationError",
    "load_settings",
    "validate_runtime",
]
