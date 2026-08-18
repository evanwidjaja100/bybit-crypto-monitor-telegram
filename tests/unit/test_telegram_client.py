"""Phase 8 - Telegram client tests (retry, splitting, error handling)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.telegram.client import TelegramClient, TelegramSendError, split_message
from tests.conftest import make_settings


def config(**overrides) -> Settings:
    overrides.setdefault("telegram_bot_token", "123456789:AAfaketokenvaluetests_only")
    overrides.setdefault("telegram_chat_id", "-1001234567890")
    overrides.setdefault("telegram_max_retries", 2)
    return make_settings(**overrides)


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Keep retry-count behavior without real exponential sleeps."""

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.telegram.client.asyncio.sleep", no_sleep)


def build_client(
    cfg: Settings, handler
) -> tuple[TelegramClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return TelegramClient(cfg, client=http), http


class TestSendMessage:
    async def test_posts_to_bot_endpoint(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        client, http = build_client(config(), handler)
        await client.send_message("hello")
        assert "bot123456789:AAfaketokenvaluetests_only/sendMessage" in captured["url"]
        assert captured["payload"]["chat_id"] == "-1001234567890"
        assert captured["payload"]["text"] == "hello"
        await http.aclose()

    async def test_long_message_split_into_multiple_requests(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"ok": True})

        client, http = build_client(config(), handler)
        text = "x" * 9000
        await client.send_message(text)
        assert len(calls) == 3
        assert all(len(c) <= 4096 for c in calls)
        assert "".join(calls) == text
        await http.aclose()

    async def test_retries_then_succeeds_on_500(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={"ok": True})

        client, http = build_client(config(telegram_max_retries=3), handler)
        await client.send_message("hello")
        assert len(attempts) == 2
        await http.aclose()

    async def test_429_surfaces_immediately_with_retry_after(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(
                429, json={"ok": False, "parameters": {"retry_after": 45}}
            )

        client, http = build_client(config(telegram_max_retries=5), handler)
        with pytest.raises(TelegramSendError) as excinfo:
            await client.send_message("hello")
        # Flood control is scheduled by the dispatcher (no internal
        # hammering); the retry_after delay must be surfaced.
        assert len(attempts) == 1
        assert excinfo.value.retry_after == 45
        await http.aclose()

    async def test_429_retry_after_from_header(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "30"})

        client, http = build_client(config(), handler)
        with pytest.raises(TelegramSendError) as excinfo:
            await client.send_message("hello")
        assert excinfo.value.retry_after == 30
        await http.aclose()

    async def test_400_is_not_retried(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(400, json={"ok": False})

        client, http = build_client(config(telegram_max_retries=3), handler)
        with pytest.raises(TelegramSendError):
            await client.send_message("hello")
        assert len(attempts) == 1
        await http.aclose()

    async def test_retries_exhausted_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client, http = build_client(config(telegram_max_retries=2), handler)
        with pytest.raises(TelegramSendError):
            await client.send_message("hello")
        await http.aclose()

    async def test_ok_false_is_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "description": "bad"})

        client, http = build_client(config(), handler)
        with pytest.raises(TelegramSendError):
            await client.send_message("hello")
        await http.aclose()

    async def test_no_secret_in_logs_on_failure(self, caplog):
        import logging

        caplog.set_level(logging.WARNING)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        token = "123456789:AAfaketokenvaluetests_only"
        client, http = build_client(config(telegram_max_retries=2), handler)
        with pytest.raises(TelegramSendError):
            await client.send_message("hello")
        assert token not in caplog.text
        await http.aclose()


class TestSplitMessage:
    def test_short_text_unchanged(self):
        assert split_message("short") == ["short"]

    def test_splits_at_newline_when_possible(self):
        text = "\n".join(f"line-{i}" for i in range(500))
        chunks = split_message(text, limit=500)
        assert len(chunks) > 1
        assert all(len(c) <= 500 for c in chunks)
        # Only boundary whitespace is dropped; content is preserved.
        assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
        # Later chunks start at line boundaries.
        assert all(chunk.startswith("line-") for chunk in chunks[1:])

    def test_hard_split_without_newline(self):
        text = "x" * 10000
        chunks = split_message(text, limit=4096)
        assert all(len(c) <= 4096 for c in chunks)
        assert "".join(chunks) == text