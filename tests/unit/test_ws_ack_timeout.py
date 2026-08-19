"""Phase J2 - ACK timeout must not depend on receive silence.

Contract (plan section 6): a missing subscription ACK must be detected
even while unrelated WebSocket traffic (tickers, pongs) is arriving
continuously. ACK expiry is checked after every received frame AND after
a receive timeout, so continuous market traffic can never suppress the
watchdog. Timeout -> reconnect (never silent delete + continue).

The fake servers PUSH valid frames on their own every 0.03s (so recv()
never times out) and drain incoming client messages so the server-side
receive queue cannot overflow.
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


def ticker_payload(symbol: str) -> str:
    return json.dumps(
        {
            "topic": f"tickers.{symbol}",
            "type": "snapshot",
            "ts": 1787015820000,
            "data": {"symbol": symbol, "lastPrice": "100"},
        }
    )


def pong_payload() -> str:
    return json.dumps({"op": "pong"})


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


def ws_config(tmp_path, port: int, **overrides):
    overrides.setdefault("database_path", str(tmp_path / "ws.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    overrides.setdefault("bybit_ws_base_url", f"ws://127.0.0.1:{port}/v5/public")
    overrides.setdefault("ws_heartbeat_interval_seconds", 0.2)
    overrides.setdefault("ws_stale_seconds", 60.0)
    overrides.setdefault("ws_subscribe_batch_size", 10)
    overrides.setdefault("ws_subscription_ack_timeout_seconds", 0.1)
    return make_settings(**overrides)


class TestAckTimeoutUnderContinuousTraffic:
    async def test_ack_timeout_fires_while_other_market_messages_are_flowing(
        self, tmp_path
    ):
        """Server never ACKs but sends a valid ticker every 0.03s: the
        pending subscription must still time out and force a reconnect."""
        connections = 0

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                drain = asyncio.create_task(drain_incoming(websocket))
                await push_frames(websocket, ticker_payload("BTCUSDT"))
                drain.cancel()
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
            assert await wait_until(lambda: connections >= 2, timeout=10.0)
            assert client._subscribed == set()
            stop.set()
            await task

    async def test_ack_timeout_reconnects_even_with_continuous_pong_frames(
        self, tmp_path
    ):
        """The server pushes pong frames every 0.03s without any client
        ping (so recv() never times out): pongs are valid frames, yet the
        ACK watchdog must still fire and force a reconnect."""
        connections = 0

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                drain = asyncio.create_task(drain_incoming(websocket))
                await push_frames(websocket, pong_payload())
                drain.cancel()
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
            assert await wait_until(lambda: connections >= 2, timeout=10.0)
            assert client._subscribed == set()
            stop.set()
            await task

    async def test_ack_timeout_reconnects_even_with_continuous_ticker_frames(
        self, tmp_path
    ):
        """An un-ACKed subscription must never stay pending forever under
        continuous ticker traffic."""
        connections = 0

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                drain = asyncio.create_task(drain_incoming(websocket))
                await push_frames(websocket, ticker_payload("BTCUSDT"))
                drain.cancel()
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
            assert await wait_until(lambda: connections >= 2, timeout=10.0)
            assert client._subscribed == set()
            stop.set()
            await task

    async def test_ack_timeout_logs_expired_req_id(self, tmp_path, caplog):
        connections = 0
        req_ids: list[str] = []

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("op") == "subscribe":
                        req_ids.append(data["req_id"])
                        drain = asyncio.create_task(drain_incoming(websocket))
                        await push_frames(websocket, ticker_payload("BTCUSDT"))
                        drain.cancel()
                        break
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
            assert await wait_until(lambda: connections >= 2, timeout=10.0)
            assert req_ids
            logged = [
                r
                for r in caplog.records
                if r.getMessage().startswith("event=ws_subscription_ack_timeout")
            ]
            assert any(req_ids[0] in r.getMessage() for r in logged)
            stop.set()
            await task

    async def test_pending_never_silently_deleted_without_reconnect(
        self, tmp_path
    ):
        """The timed-out batch must stay registered while the connection is
        alive: recovery happens through the reconnect path, never by
        silently dropping pending state and continuing on the same socket."""
        connections = 0

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                drain = asyncio.create_task(drain_incoming(websocket))
                await push_frames(websocket, ticker_payload("BTCUSDT"))
                drain.cancel()
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
            # Pending is present while connection 1 is alive...
            assert await wait_until(
                lambda: client.pending_symbols == {"BTCUSDT"}, timeout=10.0
            )
            # ...and only the reconnect clears it (the client reconnects
            # instead of silently deleting the batch on a live socket).
            assert await wait_until(lambda: connections >= 2, timeout=10.0)
            assert client._subscribed == set()
            stop.set()
            await task