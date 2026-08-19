"""Phase 10 - WebSocket client and manager tests against a local fake server."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from app.bybit.websocket import BybitWebSocketClient, WebSocketManager
from app.market.discovery import InstrumentRegistry
from app.market.price_engine import PriceEngine
from app.persistence.repository import InstrumentRepository
from tests.conftest import make_settings


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def ws_config(tmp_path, port: int, **overrides):
    overrides.setdefault("database_path", str(tmp_path / "ws.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    overrides.setdefault("bybit_ws_base_url", f"ws://127.0.0.1:{port}/v5/public")
    overrides.setdefault("ws_heartbeat_interval_seconds", 0.2)
    overrides.setdefault("ws_stale_seconds", 1.0)
    overrides.setdefault("ws_subscribe_batch_size", 10)
    return make_settings(**overrides)


def ticker_snapshot(symbol: str, last_price: str = "100") -> str:
    return json.dumps(
        {
            "topic": f"tickers.{symbol}",
            "type": "snapshot",
            "ts": 1787015820000,
            "data": {
                "symbol": symbol,
                "lastPrice": last_price,
                "prevPrice1h": "95",
                "price24hPcnt": "0.05",
                "turnover24h": "123456",
                "ts": 1787015820000,
            },
        }
    )


def ticker_delta(symbol: str, **fields) -> str:
    return json.dumps(
        {
            "topic": f"tickers.{symbol}",
            "type": "delta",
            "ts": 1787015821000,
            "data": {"symbol": symbol, "ts": 1787015821000, **fields},
        }
    )


def subscribe_ack(data: dict) -> str:
    """Realistic Bybit ACK: echoes the request's req_id."""
    return json.dumps(
        {
            "op": "subscribe",
            "success": True,
            "req_id": data.get("req_id"),
            "ret_msg": "",
        }
    )


class TestClient:
    async def test_connects_and_subscribes_topics(self, tmp_path):
        received: list[dict] = []
        connections = []

        async def handler(websocket):
            connections.append(1)
            try:
                async for message in websocket:
                    data = json.loads(message)
                    received.append(data)
                    if data.get("op") == "ping":
                        await websocket.send(json.dumps({"op": "pong"}))
                    elif data.get("op") == "subscribe":
                        await websocket.send(
                            subscribe_ack(data)
                        )
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
            assert await wait_until(
                lambda: any(
                    d.get("op") == "subscribe" for d in received
                )
            )
            subscribe = next(d for d in received if d.get("op") == "subscribe")
            assert subscribe["args"] == ["tickers.BTCUSDT", "tickers.ETHUSDT"]
            stop.set()
            await task

    async def test_batches_subscriptions_by_limit(self, tmp_path):
        received: list[dict] = []
        connections = []

        async def handler(websocket):
            connections.append(1)
            try:
                async for message in websocket:
                    data = json.loads(message)
                    received.append(data)
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port, ws_subscribe_batch_size=10)
            client = BybitWebSocketClient(
                "linear", cfg, on_message=lambda payload: None
            )
            client.set_symbols([f"SYM{i}USDT" for i in range(15)])
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            assert await wait_until(
                lambda: sum(
                    1 for d in received if d.get("op") == "subscribe"
                )
                == 2
            )
            subscribe_ops = [d for d in received if d.get("op") == "subscribe"]
            assert len(subscribe_ops[0]["args"]) == 10
            assert len(subscribe_ops[1]["args"]) == 5
            stop.set()
            await task

    async def test_ticker_messages_delivered(self, tmp_path):
        received: list[dict] = []
        connections = []

        async def handler(websocket):
            connections.append(1)
            try:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("op") == "subscribe":
                        await websocket.send(ticker_snapshot("BTCUSDT", "150"))
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port)
            client = BybitWebSocketClient(
                "spot", cfg, on_message=received.append
            )
            client.set_symbols(["BTCUSDT"])
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            assert await wait_until(lambda: len(received) == 1)
            payload = received[0]
            assert payload["category"] == "spot"
            assert payload["symbol"] == "BTCUSDT"
            assert payload["type"] == "snapshot"
            assert payload["data"]["lastPrice"] == "150"
            stop.set()
            await task

    async def test_heartbeat_ping_sent(self, tmp_path):
        received: list[dict] = []
        connections = []

        async def handler(websocket):
            connections.append(1)
            try:
                async for message in websocket:
                    data = json.loads(message)
                    received.append(data)
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port, ws_heartbeat_interval_seconds=0.2)
            client = BybitWebSocketClient(
                "spot", cfg, on_message=lambda payload: None
            )
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            assert await wait_until(
                lambda: any(d.get("op") == "ping" for d in received)
            )
            stop.set()
            await task

    async def test_stale_stream_triggers_reconnect(self, tmp_path):
        connections: list[int] = []

        async def handler(websocket):
            connections.append(1)
            try:
                # Never send anything: stream goes stale.
                async for _message in websocket:
                    pass
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port, ws_stale_seconds=0.5)
            client = BybitWebSocketClient(
                "spot", cfg, on_message=lambda payload: None
            )
            client.set_symbols(["BTCUSDT"])
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            assert await wait_until(lambda: len(connections) >= 2)
            stop.set()
            await task

    async def test_disconnect_recovers_and_resubscribes(self, tmp_path):
        connections = 0
        subscribe_ops: list[list[str]] = []

        async def handler(websocket):
            nonlocal connections
            connections += 1
            try:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("op") == "subscribe":
                        subscribe_ops.append(data["args"])
                        await websocket.send(
                            subscribe_ack(data)
                        )
                        if connections == 1:
                            await websocket.send(ticker_snapshot("BTCUSDT"))
                            await websocket.close()
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
            assert await wait_until(lambda: connections >= 2)
            assert await wait_until(lambda: len(subscribe_ops) >= 2)
            assert subscribe_ops[1] == ["tickers.BTCUSDT"]
            stop.set()
            await task

    async def test_new_symbol_subscribed_while_running(self, tmp_path):
        received: list[dict] = []
        connections = []

        async def handler(websocket):
            connections.append(1)
            try:
                async for message in websocket:
                    data = json.loads(message)
                    received.append(data)
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port)
            client = BybitWebSocketClient(
                "linear", cfg, on_message=lambda payload: None
            )
            client.set_symbols(["BTCUSDT"])
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            assert await wait_until(
                lambda: any(
                    d.get("op") == "subscribe" for d in received
                )
            )
            client.set_symbols(["BTCUSDT", "SOLUSDT"])
            await client.resubscribe()
            assert await wait_until(
                lambda: any(
                    d.get("op") == "subscribe" and "tickers.SOLUSDT" in d.get("args", [])
                    for d in received
                )
            )
            stop.set()
            await task


class TestManager:
    async def test_delta_merges_into_snapshot(self, tmp_path, db):
        connections = []

        async def handler(websocket):
            connections.append(1)
            try:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("op") == "subscribe":
                        await websocket.send(ticker_snapshot("BTCUSDT", "100"))
                        await websocket.send(
                            ticker_delta(
                                "BTCUSDT", fundingRate="0.00018", markPrice="99.5"
                            )
                        )
                        await websocket.send(ticker_delta("BTCUSDT", lastPrice="105"))
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port)
            price_engine = PriceEngine(cfg)
            repo = InstrumentRepository(db)
            from app.bybit.models import Instrument

            await repo.upsert_many(
                [
                    Instrument(
                        category="spot", symbol="BTCUSDT", base_coin="BTC", status="Trading"
                    )
                ],
                now=100,
            )
            registry = InstrumentRegistry(repo)
            manager = WebSocketManager(registry, price_engine, cfg)
            stop = asyncio.Event()
            tasks = await manager.start(stop)
            await manager.sync_subscriptions()
            assert await wait_until(
                lambda: (
                    ticker := price_engine.get("spot", "BTCUSDT")
                )
                is not None
                and ticker.funding_rate == 0.00018
                and ticker.last_price == 105
                and ticker.mark_price == 99.5
                and ticker.prev_price_1h == 95
            )
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_sync_subscriptions_from_registry(self, tmp_path, db):
        received: list[dict] = []
        connections = []

        async def handler(websocket):
            connections.append(1)
            try:
                async for message in websocket:
                    data = json.loads(message)
                    received.append(data)
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port, enable_linear_usdc=False)
            repo = InstrumentRepository(db)
            from app.bybit.models import Instrument

            await repo.upsert_many(
                [
                    Instrument(
                        category="spot", symbol="BTCUSDT", base_coin="BTC", status="Trading"
                    ),
                    Instrument(
                        category="spot", symbol="ETHUSDT", base_coin="ETH", status="Trading"
                    ),
                    Instrument(
                        category="spot", symbol="OLDUSDT", base_coin="OLD", status="Removed"
                    ),
                    Instrument(
                        category="linear", symbol="BTCUSDT", base_coin="BTC",
                        status="Trading", settle_coin="USDT",
                    ),
                    Instrument(
                        category="linear", symbol="USDCX", base_coin="USDCX",
                        status="Trading", settle_coin="USDC",
                    ),
                    Instrument(
                        category="linear", symbol="ETHUSDC", base_coin="ETH",
                        status="Trading", settle_coin="USDC",
                    ),
                ],
                now=100,
            )
            price_engine = PriceEngine(cfg)
            manager = WebSocketManager(
                InstrumentRegistry(repo), price_engine, cfg
            )
            stop = asyncio.Event()
            tasks = await manager.start(stop)
            count = await manager.sync_subscriptions()
            # spot: 2 trading, linear: USDT-settled only (USDC excluded by
            # default config; Removed status excluded).
            assert count == 3
            assert await wait_until(
                lambda: any(
                    d.get("op") == "subscribe"
                    and sorted(d.get("args", []))
                    == ["tickers.BTCUSDT", "tickers.ETHUSDT"]
                    for d in received
                )
            )
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_unsubscribes_removed_symbols(self, tmp_path, db):
        received: list[dict] = []

        async def handler(websocket):
            try:
                async for message in websocket:
                    data = json.loads(message)
                    received.append(data)
                    if data.get("op") == "subscribe":
                        await websocket.send(subscribe_ack(data))
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = ws_config(tmp_path, port)
            repo = InstrumentRepository(db)
            from app.bybit.models import Instrument

            await repo.upsert_many(
                [
                    Instrument(
                        category="spot", symbol="BTCUSDT", base_coin="BTC", status="Trading"
                    ),
                    Instrument(
                        category="spot", symbol="ETHUSDT", base_coin="ETH", status="Trading"
                    ),
                ],
                now=100,
            )
            price_engine = PriceEngine(cfg)
            manager = WebSocketManager(
                InstrumentRegistry(repo), price_engine, cfg
            )
            stop = asyncio.Event()
            tasks = await manager.start(stop)
            assert await manager.sync_subscriptions() == 2
            await wait_until(
                lambda: any(d.get("op") == "subscribe" for d in received)
            )
            # ETHUSDT is no longer desired (removed from the registry).
            await repo.upsert_many(
                [
                    Instrument(
                        category="spot", symbol="ETHUSDT", base_coin="ETH", status="Removed"
                    )
                ],
                now=101,
            )
            assert await manager.sync_subscriptions() == 1
            assert await wait_until(
                lambda: any(
                    d.get("op") == "unsubscribe"
                    and sorted(d.get("args", [])) == ["tickers.ETHUSDT"]
                    for d in received
                )
            )
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
