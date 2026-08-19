"""Phase J1 - subscription ACK registration race tests.

Contract (plan section 5):
- A pending subscription record must exist before the subscribe frame
  becomes observable to the network, so a Bybit ACK can never beat the
  registration and be classified unknown/discarded.
- Subscription synchronization operations are serialized so overlapping
  batches for the same symbol cannot be created.
- A failed send removes the pending batch (no false pending state).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.bybit.websocket import BybitWebSocketClient, PendingSubscription
from tests.conftest import make_settings


def make_client(tmp_path, **overrides) -> BybitWebSocketClient:
    cfg = make_settings(
        database_path=str(tmp_path / "ws.sqlite"),
        telegram_bot_token="fake",
        telegram_chat_id="-100fake",
        ws_subscribe_batch_size=10,
        **overrides,
    )
    return BybitWebSocketClient("linear", cfg, on_message=lambda payload: None)


class AckDuringSendWs:
    """Critical race fake: send() parses the req_id, immediately invokes the
    client's ACK handler, and only then returns from send()."""

    def __init__(self, client: BybitWebSocketClient) -> None:
        self.client = client
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)
        data = json.loads(message)
        if data.get("op") == "subscribe":
            self.client._handle_subscribe_ack(
                {
                    "op": "subscribe",
                    "success": True,
                    "req_id": data["req_id"],
                }
            )

    async def recv(self) -> str:
        await asyncio.sleep(3600)


class ObservableSendWs:
    """Records whether the pending row exists at the moment the frame becomes
    observable (inside send())."""

    def __init__(self, client: BybitWebSocketClient) -> None:
        self.client = client
        self.pending_at_send: list[bool] = []

    async def send(self, message: str) -> None:
        data = json.loads(message)
        if data.get("op") == "subscribe":
            self.pending_at_send.append(
                data["req_id"] in self.client._pending_subscriptions
            )

    async def recv(self) -> str:
        await asyncio.sleep(3600)


class FailingSendWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)
        raise ConnectionError("socket broken")

    async def recv(self) -> str:
        await asyncio.sleep(3600)


class FailingObservableSendWs:
    """Records pending-at-send then raises, to verify the rollback path."""

    def __init__(self, client: BybitWebSocketClient) -> None:
        self.client = client
        self.pending_at_send: list[bool] = []
        self.last_req_id: str | None = None

    async def send(self, message: str) -> None:
        data = json.loads(message)
        if data.get("op") == "subscribe":
            self.pending_at_send.append(
                data["req_id"] in self.client._pending_subscriptions
            )
            self.last_req_id = data["req_id"]
        raise ConnectionError("socket broken")

    async def recv(self) -> str:
        await asyncio.sleep(3600)


class SlowSendWs:
    """send() yields control before completing, so concurrent tasks can
    interleave unless the subscription lock serializes them."""

    def __init__(self, client: BybitWebSocketClient) -> None:
        self.client = client
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)
        await asyncio.sleep(0.05)

    async def recv(self) -> str:
        await asyncio.sleep(3600)


class TestAckRegistrationRace:
    async def test_ack_arriving_during_send_is_not_lost(self, tmp_path):
        client = make_client(tmp_path)
        client._ws = AckDuringSendWs(client)
        client.set_symbols(["BTCUSDT", "ETHUSDT"])
        await client.resubscribe()
        assert client.pending_symbols == set()
        assert client._subscribed == {"BTCUSDT", "ETHUSDT"}

    async def test_pending_exists_before_subscribe_frame_is_observable(
        self, tmp_path
    ):
        client = make_client(tmp_path)
        ws = ObservableSendWs(client)
        client._ws = ws
        client.set_symbols(["BTCUSDT"])
        await client.resubscribe()
        assert ws.pending_at_send == [True]

    async def test_send_failure_removes_pending_batch(self, tmp_path):
        client = make_client(tmp_path)
        ws = FailingObservableSendWs(client)
        client._ws = ws
        client.set_symbols(["BTCUSDT"])
        with pytest.raises(ConnectionError):
            await client.resubscribe()
        assert ws.pending_at_send == [True]  # registered before observable
        assert client._subscribed == set()
        assert client.pending_symbols == set()
        assert client.pending_subscriptions == {}

    async def test_concurrent_resubscribe_calls_do_not_duplicate_batches(
        self, tmp_path
    ):
        client = make_client(tmp_path)
        client._ws = SlowSendWs(client)
        client.set_symbols(["BTCUSDT", "ETHUSDT"])
        await asyncio.gather(client.resubscribe(), client.resubscribe())
        batches = [
            json.loads(m)
            for m in client._ws.sent
            if json.loads(m).get("op") == "subscribe"
        ]
        assert len(batches) == 1
        assert len(batches[0]["args"]) == 2

    async def test_dynamic_listing_and_startup_resubscribe_do_not_duplicate_symbol(
        self, tmp_path
    ):
        client = make_client(tmp_path)
        client._ws = SlowSendWs(client)
        client.set_symbols(["BTCUSDT"])

        async def startup_resubscribe() -> None:
            await client.resubscribe()

        async def dynamic_listing_sync() -> None:
            client.set_symbols(["BTCUSDT", "FRESHUSDT"])
            await client.resubscribe()

        await asyncio.gather(startup_resubscribe(), dynamic_listing_sync())
        batches = [
            json.loads(m)
            for m in client._ws.sent
            if json.loads(m).get("op") == "subscribe"
        ]
        symbols = [set(b["args"]) for b in batches]
        assert sum("tickers.BTCUSDT" in s for s in symbols) == 1
        assert sum("tickers.FRESHUSDT" in s for s in symbols) == 1

    async def test_ack_handler_runs_concurrently_without_the_sync_lock(
        self, tmp_path
    ):
        """The ACK path must never acquire the subscription lock: recovery
        depends on ACKs being processable while a sync is in flight."""
        client = make_client(tmp_path)
        client._ws = AckDuringSendWs(client)
        client.set_symbols(["BTCUSDT", "ETHUSDT"])
        task = asyncio.create_task(client.resubscribe())
        # Give the in-flight sync a chance to hold the lock and await send.
        await asyncio.sleep(0.01)
        assert await asyncio.wait_for(task, timeout=5.0) is None
        assert client._subscribed == {"BTCUSDT", "ETHUSDT"}


class TestStateInvariants:
    async def test_desired_symbol_in_at_most_one_of_pending_or_confirmed(
        self, tmp_path
    ):
        client = make_client(tmp_path)
        client._ws = AckDuringSendWs(client)
        client.set_symbols(["BTCUSDT"])
        await client.resubscribe()
        assert "BTCUSDT" in client._subscribed
        assert "BTCUSDT" not in client.pending_symbols

    async def test_confirmed_is_subset_of_desired(self, tmp_path):
        client = make_client(tmp_path)
        client._ws = AckDuringSendWs(client)
        client.set_symbols(["BTCUSDT", "ETHUSDT"])
        await client.resubscribe()
        assert client._subscribed <= client._desired_symbols