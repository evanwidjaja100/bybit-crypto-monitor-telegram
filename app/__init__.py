"""Bybit Live Momentum Monitor.

Monitors Bybit Spot and Linear (USDT/USDC settled) markets, computes
1-hour price momentum, and notifies a Telegram channel when the number
of unique qualifying base coins is between 1 and 3 (inclusive).

This is a monitoring and notification system only. It never places
trades, manages positions, or accesses private Bybit endpoints.
"""

__version__ = "1.0.0"
