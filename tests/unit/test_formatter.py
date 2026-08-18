"""Phase 8 - Telegram message formatting tests."""

from __future__ import annotations

import pytest

from app.alerts.formatter import (
    HEADER_COMPOSITION,
    HEADER_HOURLY,
    HEADER_TRANSITION,
    format_alert,
)
from app.market.deduplication import QualifyingSet, RepresentativeMarket
from app.market.momentum import MomentumValue
from tests.conftest import make_settings


def mv(
    base: str,
    change_1h: float,
    *,
    category: str = "linear",
    settle: str = "USDT",
    last: float | None = 0.08421,
    mark: float | None = None,
    ch24: float | None = None,
    funding: float | None = None,
    turnover: float | None = None,
    contract_type: str | None = "LinearPerpetual",
    quote: str | None = None,
) -> MomentumValue:
    return MomentumValue(
        category=category,
        symbol=f"{base}{settle}",
        base_coin=base,
        change_1h=change_1h,
        status="OK",
        last_price=last,
        mark_price=mark,
        change_24h=ch24,
        turnover_24h=turnover,
        funding_rate=funding,
        contract_type=contract_type,
        settle_coin=settle,
        quote_coin=quote or settle,
    )


def qset(*values: MomentumValue) -> QualifyingSet:
    return QualifyingSet(
        [
            RepresentativeMarket(base_coin=v.base_coin, representative=v)
            for v in values
        ]
    )


def cfg(**overrides):
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    return make_settings(**overrides)


class TestCountLine:
    def test_one_coin(self):
        message = format_alert(qset(mv("XYZ", 9.42)), cfg(), now=1787015820)
        assert "1 / 3 qualifying coins" in message

    def test_two_coins(self):
        message = format_alert(
            qset(mv("XYZ", 9.42), mv("ABC", 6.17)), cfg(), now=1787015820
        )
        assert "2 / 3 qualifying coins" in message

    def test_three_coins(self):
        message = format_alert(
            qset(mv("XYZ", 9.42), mv("ABC", 6.17), mv("QRS", 5.5)),
            cfg(),
            now=1787015820,
        )
        assert "3 / 3 qualifying coins" in message

    def test_never_4_of_3(self):
        with pytest.raises(ValueError):
            format_alert(
                qset(
                    mv("A", 6.0),
                    mv("B", 6.0),
                    mv("C", 6.0),
                    mv("D", 6.0),
                ),
                cfg(),
                now=1787015820,
            )


class TestContent:
    def test_header_and_footer(self):
        message = format_alert(qset(mv("XYZ", 9.42)), cfg(), now=1787015820)
        assert HEADER_TRANSITION in message
        assert "Rule:" in message
        assert "1-3 unique coins > +5% / 1H" in message
        assert "Updated: " in message and "UTC" in message

    def test_hourly_header(self):
        message = format_alert(
            qset(mv("XYZ", 9.42)), cfg(), now=1787015820, kind="hourly"
        )
        assert HEADER_HOURLY in message

    def test_composition_header(self):
        message = format_alert(
            qset(mv("XYZ", 9.42)), cfg(), now=1787015820, kind="composition"
        )
        assert HEADER_COMPOSITION in message

    def test_linear_block_fields(self):
        message = format_alert(
            qset(
                mv(
                    "XYZ",
                    9.42,
                    mark=0.08410,
                    ch24=18.30,
                    funding=0.018,
                    turnover=42800000.0,
                )
            ),
            cfg(),
            now=1787015820,
        )
        assert "USDT Perpetual" in message
        assert "1H: +9.42%" in message
        assert "Price: $0.08421" in message
        assert "24H: +18.30%" in message
        assert "Mark: $0.0841" in message
        assert "Funding: +0.018%" in message
        assert "24H Turnover: $42.80M" in message

    def test_spot_block_omits_derivative_fields(self):
        message = format_alert(
            qset(
                mv(
                    "XYZ",
                    5.9,
                    category="spot",
                    settle="USDT",
                    ch24=4.2,
                    turnover=1000.0,
                )
            ),
            cfg(),
            now=1787015820,
        )
        assert "Spot" in message
        assert "Mark:" not in message
        assert "Funding:" not in message
        assert "24H Turnover: $1.00K" in message

    def test_usdc_perpetual_label(self):
        message = format_alert(
            qset(mv("ABC", 6.17, settle="USDC")), cfg(), now=1787015820
        )
        assert "USDC Perpetual" in message

    def test_representative_others_listed(self):
        from app.market.deduplication import RepresentativeMarket

        main = mv("XYZ", 8.4)
        others = [mv("XYZ", 8.1, settle="USDC"), mv("XYZ", 5.9, category="spot")]
        qualifying = QualifyingSet(
            [RepresentativeMarket(base_coin="XYZ", representative=main, others=others)]
        )
        message = format_alert(qualifying, cfg(), now=1787015820)
        assert "also: XYZUSDC +8.10%, XYZUSDT +5.90%" in message

    def test_timestamp_uses_provided_now(self):
        message = format_alert(qset(mv("XYZ", 9.42)), cfg(), now=1787015820)
        assert "Updated: 2026-08-18 01:17 UTC" in message

    def test_negative_change_shown(self):
        message = format_alert(
            qset(mv("XYZ", -10.0, last=9.0)), cfg(), now=1787015820
        )
        assert "1H: -10.00%" in message