"""Phase 2 - Bybit JSON normalizer tests."""

from __future__ import annotations

from app.bybit.normalizer import parse_announcement, parse_instrument, parse_ticker

SPOT_RAW = {
    "symbol": "BTCUSDT",
    "baseCoin": "BTC",
    "quoteCoin": "USDT",
    "status": "Trading",
}

LINEAR_RAW = {
    "symbol": "ETHUSDT",
    "contractType": "LinearPerpetual",
    "status": "Trading",
    "baseCoin": "ETH",
    "quoteCoin": "USDT",
    "settleCoin": "USDT",
    "launchTime": "1700000000000",
    "deliveryTime": "0",
}

USDC_RAW = {
    "symbol": "BTCUSDC",
    "contractType": "LinearPerpetual",
    "status": "Trading",
    "baseCoin": "BTC",
    "quoteCoin": "USDC",
    "settleCoin": "USDC",
    "launchTime": "1700000000000",
    "deliveryTime": "0",
}

PRELAUNCH_RAW = {
    "symbol": "XYZUSDT",
    "contractType": "LinearPerpetual",
    "status": "PreLaunch",
    "baseCoin": "XYZ",
    "quoteCoin": "USDT",
    "settleCoin": "USDT",
    "launchTime": "1700000000000",
    "deliveryTime": "0",
}


class TestInstrumentParsing:
    def test_spot_instrument_parsing(self):
        instrument = parse_instrument("spot", SPOT_RAW)
        assert instrument.category == "spot"
        assert instrument.symbol == "BTCUSDT"
        assert instrument.base_coin == "BTC"
        assert instrument.quote_coin == "USDT"
        assert instrument.settle_coin is None
        assert instrument.status == "Trading"
        assert instrument.is_pre_listing is False
        assert instrument.identity == ("spot", "BTCUSDT")

    def test_linear_instrument_parsing(self):
        instrument = parse_instrument("linear", LINEAR_RAW)
        assert instrument.category == "linear"
        assert instrument.symbol == "ETHUSDT"
        assert instrument.base_coin == "ETH"
        assert instrument.contract_type == "LinearPerpetual"
        assert instrument.status == "Trading"
        assert instrument.is_pre_listing is False

    def test_usdt_settlement_detected(self):
        instrument = parse_instrument("linear", LINEAR_RAW)
        assert instrument.settle_coin == "USDT"

    def test_usdc_settlement_detected(self):
        instrument = parse_instrument("linear", USDC_RAW)
        assert instrument.settle_coin == "USDC"

    def test_prelaunch_detected(self):
        instrument = parse_instrument("linear", PRELAUNCH_RAW)
        assert instrument.status == "PreLaunch"
        assert instrument.is_pre_listing is True

    def test_launch_time_converted_to_seconds(self):
        instrument = parse_instrument("linear", LINEAR_RAW)
        assert instrument.launch_time == 1700000000

    def test_missing_fields_do_not_crash(self):
        instrument = parse_instrument("spot", {"symbol": "BTCUSDT"})
        assert instrument.base_coin == ""
        assert instrument.status == "Unknown"
        assert instrument.launch_time is None
        assert instrument.is_pre_listing is False

    def test_status_transition_trading_detected(self):
        assert parse_instrument("linear", LINEAR_RAW).status == "Trading"
        assert parse_instrument("linear", PRELAUNCH_RAW).status == "PreLaunch"


class TestTickerParsing:
    LINEAR_TICKER = {
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

    SPOT_TICKER = {
        "symbol": "BTCUSDT",
        "lastPrice": "65234.5",
        "price24hPcnt": "-0.0210",
        "turnover24h": "1000000.0",
        "volume24h": "15.2",
        "timestamp": "1700000000000",
    }

    def test_linear_ticker_parsing(self):
        ticker = parse_ticker("linear", self.LINEAR_TICKER)
        assert ticker.category == "linear"
        assert ticker.symbol == "BTCUSDT"
        assert ticker.last_price == 65234.5
        assert ticker.mark_price == 65235.1
        assert ticker.index_price == 65230.9
        assert ticker.prev_price_1h == 64000.0
        assert ticker.change_24h == 4.32
        assert ticker.turnover_24h == 1234567.89
        assert ticker.volume_24h == 18.5
        assert ticker.funding_rate == 0.0001
        assert ticker.open_interest == 125.3
        assert ticker.timestamp == 1700000000

    def test_spot_ticker_has_no_derivative_fields(self):
        ticker = parse_ticker("spot", self.SPOT_TICKER)
        assert ticker.category == "spot"
        assert ticker.last_price == 65234.5
        assert ticker.prev_price_1h is None
        assert ticker.mark_price is None
        assert ticker.funding_rate is None
        assert ticker.open_interest is None
        assert ticker.change_24h == -2.10

    def test_non_numeric_price_becomes_none(self):
        raw = {"symbol": "BTCUSDT", "lastPrice": "not-a-number"}
        ticker = parse_ticker("linear", raw)
        assert ticker.last_price is None

    def test_nan_price_becomes_none(self):
        raw = {"symbol": "BTCUSDT", "lastPrice": "NaN"}
        ticker = parse_ticker("linear", raw)
        assert ticker.last_price is None

    def test_zero_is_preserved_not_None(self):
        raw = {"symbol": "BTCUSDT", "lastPrice": "0"}
        ticker = parse_ticker("linear", raw)
        assert ticker.last_price == 0.0

    def test_empty_ticker(self):
        ticker = parse_ticker("spot", {})
        assert ticker.symbol == ""
        assert ticker.last_price is None
        assert ticker.timestamp == 0


class TestAnnouncementParsing:
    def test_announcement_parsing(self):
        raw = {
            "id": "12345",
            "title": "Bybit Lists XYZUSDT",
            "description": "details",
            "type": "new_crypto_assets",
            "dateTimestamp": "1700000000000",
        }
        announcement = parse_announcement(raw)
        assert announcement.id == "12345"
        assert announcement.title == "Bybit Lists XYZUSDT"
        assert announcement.announcement_type == "new_crypto_assets"
        assert announcement.timestamp == 1700000000