"""Telegram delivery client.

Provides ``send_message`` with timeout, bounded retry with exponential
backoff + jitter, safe message splitting, structured error handling and
no secret logging.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import httpx

from app.config import Settings

logger = logging.getLogger("bybit_monitor.telegram")

TELEGRAM_API_URL = "https://api.telegram.org"
MESSAGE_CHUNK_LIMIT = 4096


class TelegramSendError(Exception):
    """Telegram delivery failed after bounded retries (or unrecoverably)."""


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
                if response.status_code == 429 or response.status_code >= 500:
                    raise _RetryableSendError(f"http_{response.status_code}")
                if response.status_code >= 400:
                    raise TelegramSendError(f"http_{response.status_code}")
                body = response.json()
                if not body.get("ok"):
                    raise TelegramSendError("telegram_ok_false")
                return
            except (_RetryableSendError, httpx.HTTPError) as exc:
                attempt += 1
                if attempt > self.config.telegram_max_retries:
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

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["TelegramClient", "TelegramSendError", "split_message"]