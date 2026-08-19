"""Phase H1 - WebSocket subscription ACK validation tests.

Contract: a topic is never treated as confirmed subscribed merely because
a subscribe request was sent. Confirmation happens only when Bybit returns
``success: true`` for the exact ``req_id`` of the pending request.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from websockets.asyncio.server import serve

from app.bybit.websocket import BybitWebSocketClient, SubscriptionAckError
from tests.conftest import make_settings


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


class FakeWs:
    """Minimal fake socket: records sends, never receives."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        await asyncio.sleep(3600)

    async def close(self) -> None:
        pass


def ws_config(tmp_path, port: int = 1, **overrides):
    overrides.setdefault("database_path", str(tmp_path / "ws.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    overrides.setdefault("bybit_ws_base_url", f"ws://127.0.0.1:{port}/v5/public")
    overrides.setdefault("ws_heartbeat_interval_seconds", 0.2)
    overrides.setdefault("ws_stale_seconds", 60.0)
    overrides.setdefault("ws_subscribe_batch_size", 10)
    return make_settings(**overrides)


def make_client(tmp_path, **overrides) -> BybitWebSocketClient:
    cfg = ws_config(tmp_path, **overrides)
    client = BybitWebSocketClient("linear", cfg, on_message=lambda payload: None)
    client._ws = FakeWs()
    return client


def subscribe_ack(req_id: str, success: bool, ret_msg: str = "") -> str:
    return json.dumps(
        {"op": "subscribe", "success": success, "req_id": req_id, "ret_msg": ret_msg}
    )


class TestPendingState:
    async def test_subscribe_send_does_not_mark_symbols_confirmed(self, tmp_path):
        client = make_client(tmp_path)
        client.set_symbols(["BTCUSDT", "ETHUSDT"])
        await client.resubscribe()
        assert client._subscribed == set()
        assert client.pending_symbols == {"BTCUSDT", "ETHUSDT"}
        sent = json.loads(client._ws.sent[0])
        assert sent["op"] == "subscribe"
        assert sent["req_id"] == "sub-linear-1"
        assert sent["args"] == ["tickers.BTCUSDT", "tickers.ETHUSDT"]

    async def test_successful_subscribe_ack_marks_symbols_confirmed(self, tmp_path):
        client = make_client(tmp_path)
        client.set_symbols(["BTCUSDT", "ETHUSDT"])
        await client.resubscribe()
        req_id = json.loads(client._ws.sent[0])["req_id"]
        client._handle_raw(subscribe_ack(req_id, True))
        assert client._subscribed == {"BTCUSDT", "ETHUSDT"}
        assert client.pending_symbols == set()

    async def test_failed_subscribe_ack_does_not_mark_symbols_confirmed(
        self, tmp_path
    ):
        client = make_client(tmp_path)
        client.set_symbols(["BTCUSDT"])
        await client.resubscribe()
        req_id = json.loads(client._ws.sent[0])["req_id"]
        with pytest.raises(SubscriptionAckError):
            client._handle_raw(subscribe_ack(req_id, False))
        assert client._subscribed == set()
        assert client.pending_symbols == set()

    async def test_failed_ack_triggers_recovery(self, tmp_path):
        """A rejected batch causes a reconnect, after which the desired
        universe is rebuilt and eventually fully confirmed."""
        connections = 0
        acks: dict[str, bool] = {}

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("op") == "subscribe":
                        acks[data["req_id"]] = connections == 1
                        await websocket.send(
                            subscribe_ack(data["req_id"], connections != 1)
                        )
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port)
            client = BybitWebSocketClient(
                "spot", cfg, on_message=lambda payload: None
            )
            client.set_symbols(["BTCUSDT"])
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            # First connection rejects the subscribe -> reconnect occurs.
            assert await wait_until(lambda: connections >= 2)
            assert await wait_until(lambda: client._subscribed == {"BTCUSDT"})
            stop.set()
            await task

    async def test_unknown_subscribe_ack_is_safe(self, tmp_path):
        client = make_client(tmp_path)
        client.set_symbols(["BTCUSDT"])
        await client.resubscribe()
        client._handle_raw(subscribe_ack("sub-linear-999", True))
        assert client._subscribed == set()
        assert client.pending_symbols == {"BTCUSDT"}

    async def test_duplicate_subscribe_ack_is_idempotent(self, tmp_path):
        client = make_client(tmp_path)
        client.set_symbols([f"SYM{i}" for i in range(10)])
        await client.resubscribe()
        req_id = json.loads(client._ws.sent[0])["req_id"]
        client._handle_raw(subscribe_ack(req_id, True))
        assert len(client._subscribed) == 10
        # Second delivery of the same ACK: pending already resolved, safe.
        client._handle_raw(subscribe_ack(req_id, True))
        assert len(client._subscribed) == 10
        assert client.pending_symbols == set()

    async def test_subscription_ack_timeout_triggers_recovery(self, tmp_path):
        """A never-ACKed subscription must not stay pending indefinitely."""
        connections = 0

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                async for _message in websocket:
                    pass  # never ACK
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(
                tmp_path, port, ws_subscription_ack_timeout_seconds=0.1
            )
            client = BybitWebSocketClient(
                "spot", cfg, on_message=lambda payload: None
            )
            client.set_symbols(["BTCUSDT"])
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            # The ACK timeout forces repeated reconnects; the symbol is
            # never confirmed without an ACK.
            assert await wait_until(lambda: connections >= 3, timeout=10.0)
            assert client._subscribed == set()
            stop.set()
            await task

    async def test_reconnect_clears_pending_and_confirmed_but_keeps_desired(
        self, tmp_path
    ):
        connections = 0

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("op") == "subscribe":
                        if connections == 1:
                            # Confirm, then drop the stream.
                            await websocket.send(
                                subscribe_ack(data["req_id"], True)
                            )
                            await websocket.close()
                        # Connection 2 leaves the requests pending.
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port)
            client = BybitWebSocketClient(
                "spot", cfg, on_message=lambda payload: None
            )
            client.set_symbols(["BTCUSDT", "ETHUSDT"])
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            # Connection 1 confirms both, then drops.
            assert await wait_until(
                lambda: client._subscribed == {"BTCUSDT", "ETHUSDT"}
            )
            # Connection 2 starts with a clean slate: confirmed and
            # pending are empty; the desired universe is preserved and
            # re-requested (still pending because connection 2 never ACKs).
            assert await wait_until(lambda: connections >= 2)
            assert await wait_until(
                lambda: client.pending_symbols == {"BTCUSDT", "ETHUSDT"}
            )
            assert client._subscribed == set()
            assert client._desired_symbols == {"BTCUSDT", "ETHUSDT"}
            stop.set()
            await task

    async def test_dynamic_listing_only_becomes_confirmed_after_ack(
        self, tmp_path
    ):
        """A new market arriving mid-run is never 'confirmed monitored'
        until its subscribe request is ACKed."""
        client = make_client(tmp_path)
        client.set_symbols(["BTCUSDT"])
        await client.resubscribe()
        req_id = json.loads(client._ws.sent[0])["req_id"]
        client._handle_raw(subscribe_ack(req_id, True))
        assert client._subscribed == {"BTCUSDT"}
        # A new listing appears: subscribe is requested, ticker pushes can
        # arrive, but the symbol must NOT be in the confirmed set.
        client.set_symbols(["BTCUSDT", "FRESHUSDT"])
        await client.resubscribe()
        assert client._subscribed == {"BTCUSDT"}
        assert client.pending_symbols == {"FRESHUSDT"}
        fresh_req = json.loads(client._ws.sent[1])["req_id"]
        # Ticker arriving before the ACK changes nothing.
        client._handle_raw(
            json.dumps(
                {
                    "topic": "tickers.FRESHUSDT",
                    "type": "snapshot",
                    "ts": 1,
                    "data": {"symbol": "FRESHUSDT", "lastPrice": "1"},
                }
            )
        )
        assert client._subscribed == {"BTCUSDT"}
        # Only the successful ACK confirms the new listing.
        client._handle_raw(subscribe_ack(fresh_req, True))
        assert client._subscribed == {"BTCUSDT", "FRESHUSDT"}

    async def test_critical_batch_partial_failure_then_full_recovery(
        self, tmp_path
    ):
        """25 desired symbols, batch size 10: batch 1 ACKs, batch 2 is
        rejected, batch 3 ACKs. Only 15 are confirmed before recovery;
        after the reconnect all 25 are confirmed."""
        connections = 0
        deferred_reject: list[str] = []

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("op") != "subscribe":
                        continue
                    index = int(data["req_id"].rsplit("-", 1)[1])
                    if connections == 1 and index == 2:
                        # Hold the failure ACK back until batch 3's success
                        # ACK has been delivered (server ordering).
                        deferred_reject.append(data["req_id"])
                        continue
                    reject = connections == 1 and index == 2
                    await websocket.send(subscribe_ack(data["req_id"], not reject))
                    if index == 3 and deferred_reject:
                        await websocket.send(
                            subscribe_ack(deferred_reject.pop(0), False)
                        )
            except Exception:
                pass

        symbols = [f"SYM{i:02d}USDT" for i in range(25)]
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port, ws_subscribe_batch_size=10)
            client = BybitWebSocketClient(
                "linear", cfg, on_message=lambda payload: None
            )
            client.set_symbols(symbols)
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            # Batch 1 (10) + batch 3 (5) confirmed; batch 2 (10) rejected
            # and NOT confirmed before the recovery reconnect.
            assert await wait_until(
                lambda: len(client._subscribed) == 15
            ) and connections == 1
            assert client._subscribed == set(symbols[:10]) | set(symbols[20:])
            assert not (set(symbols[10:20]) & client._subscribed)
            # Reconnect restores the full desired universe.
            assert await wait_until(
                lambda: connections >= 2 and client._subscribed == set(symbols),
                timeout=10.0,
            )
            stop.set()
            await task