"""Shared test fixtures."""
import pytest
from pathlib import Path
from bot.kalshi_client import Market, Order, Position
from bot.strategy import Signal


def make_market(
    ticker="KXBTC-26APR4PM-B95000",
    yes_ask=0.45,
    yes_bid=0.40,
    no_ask=0.55,
    no_bid=0.50,
    close_time="2099-04-26T20:00:00Z",
    last_price=None,
    status="open",
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXBTC",
        status=status,
        close_time=close_time,
        yes_ask=yes_ask,
        yes_bid=yes_bid,
        no_ask=no_ask,
        no_bid=no_bid,
        last_price=last_price,
    )


def make_signal(
    ticker="KXBTC-26APR4PM-B95000",
    side="yes",
    price=0.45,
    gross_edge=0.22,
    edge=0.15,
    fee=0.07,
    theo_prob=0.67,
    strike=95000.0,
    mid_price=0.425,
) -> Signal:
    return Signal(
        ticker=ticker,
        side=side,
        price=price,
        gross_edge=gross_edge,
        edge=edge,
        fee=fee,
        theo_prob=theo_prob,
        strike=strike,
        mid_price=mid_price,
    )


@pytest.fixture
def sample_market():
    return make_market()


@pytest.fixture
def sample_signal():
    return make_signal()
