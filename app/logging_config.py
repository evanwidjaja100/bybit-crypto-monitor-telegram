"""Logging initialization with secret redaction.

Production secrets (Telegram bot tokens / chat ids) must never appear in
logs. A ``SecretRedactionFilter`` is attached to the root logger so every
handler, including test capture handlers, sees redacted records.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

# Matches Telegram bot tokens of the form ``123456789:AAHt5...``.
# No leading boundary is required because tokens routinely appear after
# ``bot`` (e.g. ``https://api.telegram.org/bot<token>/sendMessage``).
_TELEGRAM_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{25,}(?![A-Za-z0-9_-])")


class SecretRedactionFilter(logging.Filter):
    """Redacts configured secret values and token-shaped substrings."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            if secret and secret in message:
                message = message.replace(secret, "***")
        message = _TELEGRAM_TOKEN_RE.sub("***", message)
        # Replace the formatted message and drop args so handlers never
        # re-format with the original arguments.
        record.msg = message
        record.args = ()
        return True


def _resolve_level(level: str) -> int:
    value = getattr(logging, str(level).upper(), None)
    if not isinstance(value, int):
        return logging.INFO
    return value


def setup_logging(
    level: str = "INFO",
    secrets: Iterable[str] = (),
    fmt: str = "%(asctime)s %(levelname)s %(name)s %(message)s",
) -> None:
    """Configure the root logger with a redacting console handler."""
    root = logging.getLogger()
    root.setLevel(_resolve_level(level))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(SecretRedactionFilter(secrets))
    root.addHandler(console)

    # Attach to the logger itself so every downstream handler sees
    # redacted records too.
    root.addFilter(SecretRedactionFilter(secrets))

    logging.getLogger("asyncio").setLevel(logging.WARNING)


__all__ = ["SecretRedactionFilter", "setup_logging"]
