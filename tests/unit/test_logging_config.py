"""Phase 1 - logging / secret redaction tests."""

from __future__ import annotations

import logging

from app.logging_config import SecretRedactionFilter, setup_logging


class TestSecretRedaction:
    def test_configured_secret_is_redacted(self):
        f = SecretRedactionFilter(["supersecret"])
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "value is supersecret now", (), None
        )
        assert f.filter(record) is True
        assert "supersecret" not in record.getMessage()
        assert "***" in record.getMessage()

    def test_telegram_token_pattern_is_redacted(self):
        token = "9876543210:AAHgRvGhtyYzFakeTokenValue1234567890123"
        f = SecretRedactionFilter([])
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, f"url https://api.telegram.org/bot{token}/sendMessage", (), None
        )
        assert f.filter(record) is True
        assert token not in record.getMessage()
        assert "***" in record.getMessage()

    def test_normal_messages_are_untouched(self):
        f = SecretRedactionFilter(["token"])
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "event=ticker_update symbol=BTCUSDT", (), None
        )
        assert f.filter(record) is True
        assert "event=ticker_update symbol=BTCUSDT" in record.getMessage()

    def test_no_secrets_appear_in_captured_logs(self, caplog):
        token = "1234567890:AAFakeBotTokenForTestingPurposesOnly_0123456789"
        setup_logging("INFO", secrets=[token])
        caplog.set_level(logging.INFO)
        logger = logging.getLogger("test.telegram")
        logger.info("sending telegram message with token=%s chat=%s", token, "-100fake")
        assert token not in caplog.text

    def test_setup_logging_attaches_console_handler(self):
        setup_logging("INFO", secrets=["secret"])
        root = logging.getLogger()
        assert any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in root.handlers
        )
