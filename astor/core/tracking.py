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


# -- trail: the same trade, managed rather than announced -------------------

TrailActionKind = Literal["breakeven", "exit"]


@dataclass(frozen=True, slots=True)
class TrailAction:
    """What the trail rules want done at IBKR after one close. `giveback`
    and `limit` are dollars per share, carried so the alert and the store
    can say why an exit fired without recomputing it."""

    kind: TrailActionKind
    symbol: str
    close: float
    bar_start: datetime
    giveback: float = 0.0
    limit: float = 0.0


def giveback_limit(trade: TrackedTrade) -> float:
    """How far a close may fall back from the best close before `trail`
    exits, in dollars per share:

        best close under +1R       -> 1R
        best close +1R up to +2R   -> 50% of it
        best close +2R and beyond  -> 1R

    The two pieces meet at +2R (half of 2R is 1R), so the limit never jumps
    there. It does jump at +1R, from 1R down to 0.5R -- harmless, since the
    same close moves the stop to breakeven and the exit it implies (+0.5R)
    is above that.
    """
    assert trade.peak is not None
    peak_r = abs(trade.peak - trade.entry) / trade.r
    if 1.0 <= peak_r < 2.0:
        return 0.5 * peak_r * trade.r
    return trade.r


class TrailTracker:
    """The trail rules, on 1-min closes like `SellRuleTracker` but with a
    different giveback rule (`giveback_limit`) that applies from the start,
    not only after +1R: a trade that runs +0.5R and closes back at -0.5R has
    given back 1R and exits instead of waiting for the stop at -1R.

    `peak` starts at entry, so a trade that never trades in its favor
    measures giveback from entry -- which puts the 1R exit at the original
    stop, where the stop order already is.

    Pure: says what should happen. Placing it is `astor.drivers.orders`.
    """

    def __init__(self) -> None:
        self.trades: dict[str, TrackedTrade] = {}

    def track(
        self, symbol: str, entry: float, stop: float, account: str | None = None
    ) -> TrackedTrade:
        trade = TrackedTrade(symbol, entry, stop, account, peak=entry)
        self.trades[symbol] = trade
        return trade

    def untrack(self, symbol: str) -> bool:
        return self.trades.pop(symbol, None) is not None

    def on_close(self, symbol: str, close: float, bar_start: datetime) -> TrailAction | None:
        trade = self.trades.get(symbol)
        if trade is None or trade.state == "done":
            return None
        assert trade.peak is not None
        trade.peak = trade.more_favorable(trade.peak, close)

        if trade.state == "watching" and trade.is_favorable(close):
            trade.state = "armed"
            trade.armed_at = bar_start
            return TrailAction("breakeven", symbol, close, bar_start)

        giveback = abs(trade.peak - close)
        limit = giveback_limit(trade)
        # Rounded first: closes and R are cent prices, and a giveback of
        # exactly the limit must not miss it on float noise.
        if round(giveback, 6) >= round(limit, 6):
            trade.state = "done"
            return TrailAction("exit", symbol, close, bar_start, giveback, limit)
        return None
