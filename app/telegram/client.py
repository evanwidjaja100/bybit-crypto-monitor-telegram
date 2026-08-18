"""Telegram delivery client.

Provides ``send_message`` with timeout, bounded retry with exponential
backoff + jitter, safe message splitting, structured error handling and
no secret logging.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import httpx

from app.config import Settings

logger = logging.getLogger("bybit_monitor.telegram")

TELEGRAM_API_URL = "https://api.telegram.org"
MESSAGE_CHUNK_LIMIT = 4096


class TelegramSendError(Exception):
    """Telegram delivery failed after bounded retries (or unrecoverably).

    ``retry_after`` (seconds) may be set by the sender for flood-control
    responses; the dispatcher treats it as the minimum next-attempt delay.
    """

    def __init__(self, *args: object, retry_after: Optional[float] = None) -> None:
        super().__init__(*args)
        self.retry_after: Optional[float] = retry_after


class TelegramPermanentError(TelegramSendError):
    """Telegram rejected the request permanently (e.g. 400-class).

    Retrying can never succeed; the dispatcher marks such notifications
    ``dead`` immediately.
    """


class _RetryableSendError(Exception):
    pass


def split_message(text: str, limit: int = MESSAGE_CHUNK_LIMIT) -> list[str]:
    """Split text into Telegram-safe chunks preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramClient:
    """Minimal Bot-API client (sendMessage only)."""

    def __init__(
        self, config: Settings, client: Optional[httpx.AsyncClient] = None
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        self.last_success_at: Optional[int] = None
        self.last_error_at: Optional[int] = None
        self.last_error_type: Optional[str] = None

    def _record_failure(self, error_type: str) -> None:
        """Every failed delivery attempt updates the health state."""
        self.last_error_at = int(time.time())
        self.last_error_type = error_type

    async def send_message(self, text: str) -> None:
        """Send ``text``, splitting into <=4096-char chunks as needed."""
        for chunk in split_message(text):
            await self._send_chunk(chunk)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.config.telegram_timeout_seconds
            )
        return self._client

    async def _send_chunk(self, text: str) -> None:
        token = self.config.telegram_bot_token
        url = f"{TELEGRAM_API_URL}/bot{token}/sendMessage"
        payload = {"chat_id": self.config.telegram_chat_id, "text": text}
        attempt = 0
        while True:
            try:
                response = await (await self._http()).post(url, json=payload)
                if response.status_code == 429:
                    retry_after = self._parse_retry_after(response)
                    self._record_failure("http_429")
                    raise TelegramSendError(
                        "http_429", retry_after=retry_after
                    )
                if response.status_code >= 500:
                    raise _RetryableSendError(f"http_{response.status_code}")
                if response.status_code >= 400:
                    # 400-class is permanent (bad token, unknown chat, ...).
                    self._record_failure(f"http_{response.status_code}")
                    raise TelegramPermanentError(f"http_{response.status_code}")
                body = response.json()
                if not body.get("ok"):
                    self._record_failure("telegram_ok_false")
                    raise TelegramPermanentError("telegram_ok_false")
                self.last_success_at = int(time.time())
                return
            except (_RetryableSendError, httpx.HTTPError) as exc:
                attempt += 1
                self._record_failure(
                    f"{type(exc).__name__}:attempt_{attempt}"
                )
                if attempt > self.config.telegram_max_retries:
                    self._record_failure(
                        f"retries_exhausted:{type(exc).__name__}"
                    )
                    raise TelegramSendError(
                        f"retries_exhausted:{type(exc).__name__}"
                    ) from exc
                delay = min(
                    30.0, (2**attempt) + random.uniform(0.0, 0.5)
                )
                logger.warning(
                    "event=telegram_retry attempt=%d delay=%.1fs error=%s",
                    attempt,
                    delay,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> Optional[float]:
        """Telegram flood-control delay: JSON ``retry_after`` or Retry-After."""
        try:
            body = response.json()
            retry_after = body.get("parameters", {}).get("retry_after")
        except ValueError:
            retry_after = None
        if retry_after is None:
            header = response.headers.get("Retry-After")
            if header is not None:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
        return float(retry_after) if retry_after is not None else None

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = [
    "TelegramClient",
    "TelegramSendError",
    "TelegramPermanentError",
    "split_message",
]