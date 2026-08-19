"""Phase J3 - pong/control traffic must never reset ticker freshness.

Contract (plan section 7): connection heartbeat freshness
(``_last_any_message_*``) and market ticker freshness
(``_last_ticker_*``) are tracked separately. Pongs and subscription
ACKs update only the connection timestamps; only ticker frames update
the ticker timestamps. The stale watchdog and market health use ticker
freshness, so a dead ticker feed can never hide behind healthy pongs.
"""

from __future__ import annotations

import asyncio
import json

from websockets.asyncio.server import serve

from app.bybit.websocket import BybitWebSocketClient
from tests.conftest import make_settings


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


def ws_config(tmp_path, port: int, **overrides):
    overrides.setdefault("database_path", str(tmp_path / "ws.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    overrides.setdefault("bybit_ws_base_url", f"ws://127.0.0.1:{port}/v5/public")
    overrides.setdefault("ws_heartbeat_interval_seconds", 0.2)
    overrides.setdefault("ws_stale_seconds", 60.0)
    overrides.setdefault("ws_subscribe_batch_size", 10)
    return make_settings(**overrides)


def make_client(tmp_path, **overrides) -> BybitWebSocketClient:
    cfg = ws_config(tmp_path, 1, **overrides)
    client = BybitWebSocketClient("linear", cfg, on_message=lambda payload: None)
    client._ws = FakeWs()
    return client


def pong_payload() -> str:
    return json.dumps({"op": "pong"})


def ticker_payload(symbol: str) -> str:
    return json.dumps(
        {
            "topic": f"tickers.{symbol}",
            "type": "snapshot",
            "ts": 1787015820000,
            "data": {"symbol": symbol, "lastPrice": "100"},
        }
    )


def subscribe_ack(req_id: str, success: bool) -> str:
    return json.dumps({"op": "subscribe", "success": success, "req_id": req_id})


async def drain_incoming(websocket) -> None:
    async for _message in websocket:
        pass


async def push_frames(websocket, payload: str, interval: float = 0.03) -> None:
    try:
        while True:
            await websocket.send(payload)
            await asyncio.sleep(interval)
    except Exception:
        pass


class TestFreshnessTimestamps:
    async def test_pong_updates_connection_freshness_not_ticker_freshness(
        self, tmp_path
    ):
        client = make_client(tmp_path)
        assert client.last_any_message_at is None
        assert client.last_ticker_at is None
        client._handle_raw(pong_payload())
        assert client.last_any_message_at is not None
        assert client.last_ticker_at is None
        assert client._last_any_message_monotonic is not None
        assert client._last_ticker_monotonic is None

    async def test_subscription_ack_updates_any_message_not_ticker_timestamp(
        self, tmp_path
    ):
        client = make_client(tmp_path)
        client.set_symbols(["BTCUSDT"])
        await client.resubscribe()
        req_id = json.loads(client._ws.sent[0])["req_id"]
        assert client.last_ticker_at is None
        client._handle_raw(subscribe_ack(req_id, True))
        assert client.last_any_message_at is not None
        assert client.last_ticker_at is None
        assert client._last_ticker_monotonic is None

    async def test_ticker_updates_both_any_message_and_ticker_freshness(
        self, tmp_path
    ):
        client = make_client(tmp_path)
        client._handle_raw(ticker_payload("BTCUSDT"))
        assert client.last_any_message_at is not None
        assert client.last_ticker_at is not None
        assert client._last_any_message_monotonic is not None
        assert client._last_ticker_monotonic is not None

    async def test_continuous_pong_without_ticker_eventually_triggers_ticker_stale_reconnect(
        self, tmp_path
    ):
        """Critical test: ACK the subscription, then push only pongs. The
        connection-level traffic stays fresh but ticker freshness goes
        stale, so the client must reconnect."""
        connections = 0

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                async def serve_one():
                    async for message in websocket:
                        data = json.loads(message)
                        if data.get("op") == "subscribe":
                            await websocket.send(subscribe_ack(data["req_id"], True))

                drain = asyncio.create_task(serve_one())
                await push_frames(websocket, pong_payload())
                drain.cancel()
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port, ws_stale_seconds=0.2)
            client = BybitWebSocketClient(
                "spot", cfg, on_message=lambda payload: None
            )
            client.set_symbols(["BTCUSDT"])
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            # Connection freshness must stay alive (pongs flow)...
            assert await wait_until(
                lambda: client.connected and client.last_any_message_at is not None,
                timeout=10.0,
            )
            # ...while ticker staleness forces a reconnect.
            assert await wait_until(lambda: connections >= 2, timeout=10.0)
            stop.set()
            await task