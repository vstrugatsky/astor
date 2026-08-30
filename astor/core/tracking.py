"""Sell-rule tracking for open trades.

Two rules, both measured off 1-min bar *closes* only -- never a bar's high or
low, so a wick can't arm or fire a rule the close itself never confirmed:

    1. Price closes at or beyond +1R in the trade's favor -- notify to move
       the stop to breakeven.
    2. After that, price closes back giving up 0.5R from the best close seen
       since arming (a trailing giveback, not a fixed level) -- notify to
       sell.

R is a dollar-per-share figure, not a percent: R = |entry - stop|. Framing it
as "the percent difference between entry and stop" (as the trader does) and
computing it as a dollar distance are the same thing here -- the percent is
just entry-relative context for the same |entry - stop| gap, so there is
nothing to convert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

State = Literal["watching", "armed", "done"]
AlertKind = Literal["breakeven", "sell"]


@dataclass
class TrackedTrade:
    """One position being watched for sell-rule alerts.

    `peak` is the best (most favorable) close seen since arming -- None until
    the trade arms. Direction is inferred from stop vs. entry rather than
    passed in, since the two are never allowed to disagree in this codebase.
    """

    symbol: str
    entry: float
    stop: float
    account: str | None = None
    state: State = "watching"
    peak: float | None = None
    armed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.stop == self.entry:
            raise ValueError(f"{self.symbol}: stop cannot equal entry (R would be zero)")

    @property
    def direction(self) -> str:
        return "long" if self.stop < self.entry else "short"

    @property
    def r(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def target_1r(self) -> float:
        return self.entry + self.r if self.direction == "long" else self.entry - self.r

    def is_favorable(self, close: float) -> bool:
        return close >= self.target_1r if self.direction == "long" else close <= self.target_1r

    def more_favorable(self, a: float, b: float) -> float:
        return max(a, b) if self.direction == "long" else min(a, b)


@dataclass(frozen=True, slots=True)
class TrackAlert:
    kind: AlertKind
    symbol: str
    close: float
    bar_start: datetime


class SellRuleTracker:
    """Owns every trade currently being tracked, keyed by symbol -- one
    tracked trade per symbol at a time; re-tracking a symbol replaces it.
    """

    def __init__(self) -> None:
        self.trades: dict[str, TrackedTrade] = {}

    def track(
        self, symbol: str, entry: float, stop: float, account: str | None = None
    ) -> TrackedTrade:
        trade = TrackedTrade(symbol, entry, stop, account)
        self.trades[symbol] = trade
        return trade

    def untrack(self, symbol: str) -> bool:
        return self.trades.pop(symbol, None) is not None

    def on_close(self, symbol: str, close: float, bar_start: datetime) -> TrackAlert | None:
        trade = self.trades.get(symbol)
        if trade is None or trade.state == "done":
            return None

        if trade.state == "watching":
            if not trade.is_favorable(close):
                return None
            trade.state = "armed"
            trade.peak = close
            trade.armed_at = bar_start
            return TrackAlert("breakeven", symbol, close, bar_start)

        # armed: track the running peak, fire once giveback from it hits 0.5R
        assert trade.peak is not None
        trade.peak = trade.more_favorable(trade.peak, close)
        giveback = abs(trade.peak - close)
        if giveback >= 0.5 * trade.r:
            trade.state = "done"
            return TrackAlert("sell", symbol, close, bar_start)
        return None
