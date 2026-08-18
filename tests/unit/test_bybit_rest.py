"""Phase 2 - Bybit REST client tests (offline via httpx MockTransport)."""

from __future__ import annotations

import httpx
import pytest

from app.bybit.rest import (
    BybitAPIError,
    BybitHTTPStatusError,
    BybitMalformedResponse,
    BybitRestClient,
    BybitServerError,
)


async def _noop_sleep(_delay: float) -> None:
    return None


def ok_payload(result) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": result, "time": 1700000000000}


def make_client(handler, **kwargs) -> BybitRestClient:
    defaults = {
        "max_retries": 2,
        "base_backoff": 0.0,
        "jitter": 0.0,
        "sleep_fn": _noop_sleep,
    }
    defaults.update(kwargs)
    return BybitRestClient(transport=httpx.MockTransport(handler), **defaults)


def instruments_result(*items) -> dict:
    return ok_payload({"list": list(items)})


def linear_item(symbol, settle="USDT", status="Trading", contract="LinearPerpetual", base=None):
    return {
        "symbol": symbol,
        "contractType": contract,
        "status": status,
        "baseCoin": base or symbol.replace(settle, ""),
        "quoteCoin": settle,
        "settleCoin": settle,
        "launchTime": "1700000000000",
        "deliveryTime": "0",
    }


def spot_item(symbol, base="BTC", status="Trading"):
    return {
        "symbol": symbol,
        "baseCoin": base,
        "quoteCoin": "USDT",
        "status": status,
    }


class TestInstrumentEndpoints:
    async def test_get_spot_instruments_parsing(self):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=instruments_result(spot_item("BTCUSDT")))

        client = make_client(handler)
        instruments = await client.get_spot_instruments()
        await client.close()
        assert len(instruments) == 1
        assert instruments[0].category == "spot"
        assert instruments[0].symbol == "BTCUSDT"
        assert instruments[0].base_coin == "BTC"
        assert "category=spot" in str(calls[0].url)

    async def test_get_linear_instruments_trading_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=instruments_result(linear_item("ETHUSDT", settle="USDT"))
            )

        client = make_client(handler)
        instruments = await client.get_linear_instruments(status="Trading")
        await client.close()
        assert instruments[0].category == "linear"
        assert instruments[0].settle_coin == "USDT"
        assert instruments[0].status == "Trading"
        assert instruments[0].is_pre_listing is False

    async def test_get_linear_prelaunch_detection(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=instruments_result(
                    linear_item("XYZUSDT", status="PreLaunch", base="XYZ")
                ),
            )

        client = make_client(handler)
        instruments = await client.get_linear_instruments(status="PreLaunch")
        await client.close()
        assert instruments[0].is_pre_listing is True
        assert instruments[0].status == "PreLaunch"

    async def test_usdc_settlement_detected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=instruments_result(linear_item("BTCUSDC", settle="USDC", base="BTC"))
            )

        client = make_client(handler)
        instruments = await client.get_linear_instruments()
        await client.close()
        assert instruments[0].settle_coin == "USDC"
        assert instruments[0].base_coin == "BTC"

    async def test_linear_pagination_across_pages(self):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            cursor = request.url.params.get("cursor")
            if cursor is None:
                return httpx.Response(
                    200,
                    json=ok_payload(
                        {
                            "list": [linear_item("BTCUSDT")],
                            "nextPageCursor": "page2",
                        }
                    ),
                )
            return httpx.Response(
                200,
                json=ok_payload(
                    {"list": [linear_item("ETHUSDT"), linear_item("SOLUSDT")]}
                ),
            )

        client = make_client(handler)
        instruments = await client.get_linear_instruments()
        await client.close()
        assert len(instruments) == 3
        assert [i.symbol for i in instruments] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert len(calls) == 2
        assert calls[1].url.params.get("cursor") == "page2"

    async def test_empty_cursor_terminates_pagination(self):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=instruments_result(linear_item("BTCUSDT")))

        client = make_client(handler)
        instruments = await client.get_linear_instruments()
        await client.close()
        assert len(instruments) == 1
        assert len(calls) == 1


class TestTickerEndpoints:
    def _linear_ticker_payload(self):
        return ok_payload(
            {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "lastPrice": "65234.5",
                        "markPrice": "65235.1",
                        "indexPrice": "65230.9",
                        "prevPrice1h": "64000.0",
                        "price24hPcnt": "0.0432",
                        "turnover24h": "1234567.89",
                        "volume24h": "18.5",
                        "fundingRate": "0.0001",
                        "openInterest": "125.3",
                        "timestamp": "1700000000000",
                    }
                ]
            }
        )

    async def test_get_linear_tickers(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "category=linear" in str(request.url)
            return httpx.Response(200, json=self._linear_ticker_payload())

        client = make_client(handler)
        tickers = await client.get_linear_tickers()
        await client.close()
        assert len(tickers) == 1
        assert tickers[0].category == "linear"
        assert tickers[0].prev_price_1h == 64000.0
        assert tickers[0].change_24h == 4.32

    async def test_get_spot_tickers(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "category=spot" in str(request.url)
            return httpx.Response(
                200,
                json=ok_payload(
                    {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "lastPrice": "65234.5",
                                "price24hPcnt": "-0.0210",
                                "timestamp": "1700000000000",
                            }
                        ]
                    }
                ),
            )

        client = make_client(handler)
        tickers = await client.get_spot_tickers()
        await client.close()
        assert tickers[0].category == "spot"
        assert tickers[0].prev_price_1h is None


class TestServerTimeAndAnnouncements:
    async def test_get_server_time(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"timeSecond": "1700000000", "timeNano": "0"},
                },
            )

        client = make_client(handler)
        server_time = await client.get_server_time()
        await client.close()
        assert server_time == 1700000000

    async def test_get_announcements(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "announcements" in str(request.url)
            return httpx.Response(
                200,
                json=ok_payload(
                    {
                        "list": [
                            {
                                "id": "1001",
                                "title": "Bybit Lists XYZUSDT on Spot",
                                "type": "new_crypto_assets",
                                "description": "details",
                                "dateTimestamp": "1700000000000",
                            }
                        ]
                    }
                ),
            )

        client = make_client(handler)
        announcements = await client.get_announcements(limit=10)
        await client.close()
        assert len(announcements) == 1
        assert announcements[0].id == "1001"
        assert "XYZUSDT" in announcements[0].title


class TestFailureHandling:
    async def test_timeout_then_success_retries(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectTimeout("connect timed out", request=request)
            return httpx.Response(200, json=instruments_result(spot_item("BTCUSDT")))

        client = make_client(handler)
        instruments = await client.get_spot_instruments()
        await client.close()
        assert len(instruments) == 1
        assert calls["n"] == 2

    async def test_http_500_then_success_retries(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, text="internal server error")
            return httpx.Response(200, json=instruments_result(spot_item("BTCUSDT")))

        client = make_client(handler)
        instruments = await client.get_spot_instruments()
        await client.close()
        assert len(instruments) == 1
        assert calls["n"] == 2

    async def test_retries_exhausted_raises(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, text="unavailable")

        client = make_client(handler)
        with pytest.raises(BybitServerError):
            await client.get_spot_instruments()
        await client.close()
        assert calls["n"] == 3  # 1 initial + 2 retries

    async def test_nonzero_retcode_raises_api_error(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200, json={"retCode": 10001, "retMsg": "bad request", "result": {}}
            )

        client = make_client(handler)
        with pytest.raises(BybitAPIError) as exc_info:
            await client.get_spot_instruments()
        await client.close()
        assert exc_info.value.ret_code == 10001
        assert calls["n"] == 1  # not retried

    async def test_retryable_retcode_then_success(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200, json={"retCode": 10006, "retMsg": "rate limit", "result": {}}
                )
            return httpx.Response(200, json=instruments_result(spot_item("BTCUSDT")))

        client = make_client(handler)
        instruments = await client.get_spot_instruments()
        await client.close()
        assert len(instruments) == 1
        assert calls["n"] == 2

    async def test_malformed_json_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>not json</html>")

        client = make_client(handler)
        with pytest.raises(BybitMalformedResponse):
            await client.get_spot_instruments()
        await client.close()

    async def test_http_400_is_not_retried(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, text="bad request")

        client = make_client(handler)
        with pytest.raises(BybitHTTPStatusError):
            await client.get_spot_instruments()
        await client.close()
        assert calls["n"] == 1

    async def test_http_429_is_retried(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, text="too many requests")
            return httpx.Response(200, json=instruments_result(spot_item("BTCUSDT")))

        client = make_client(handler)
        instruments = await client.get_spot_instruments()
        await client.close()
        assert len(instruments) == 1
        assert calls["n"] == 2

    async def test_backoff_delay_bounded_and_increasing(self):
        client = BybitRestClient(
            base_url="https://api.bybit.com",
            base_backoff=1.0,
            jitter=0.0,
            max_backoff=4.0,
            sleep_fn=_noop_sleep,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=ok_payload({"list": []}))
            ),
        )
        assert client._backoff_delay(1) == 1.0
        assert client._backoff_delay(2) == 2.0
        assert client._backoff_delay(3) == 4.0  # capped at max_backoff
        await client.close()

    async def test_client_rejects_requests_after_close(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=ok_payload({"list": []}))

        client = make_client(handler)
        await client.get_spot_instruments()
        await client.close()
        with pytest.raises(Exception):
            await client.get_spot_instruments()