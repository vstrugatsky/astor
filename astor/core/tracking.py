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
from datetime import datetime, time
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
    # trail only: when the end-of-day rule fires for this trade -- one
    # minute before its own session's close (see TrailTracker.track)
    eod_exit: time | None = None
    # When the position was opened, if known -- `trail` measures its best
    # close from here rather than from the moment it was started
    opened_at: datetime | None = None

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

    def gain_r(self, price: float) -> float:
        """Open gain at `price` in R, negative when under water."""
        move = price - self.entry if self.direction == "long" else self.entry - price
        return move / self.r

    def more_favorable(self, a: float, b: float) -> float:
        return max(a, b) if self.direction == "long" else min(a, b)

    @property
    def key(self) -> TradeKey:
        return self.symbol, self.account


# One trade per symbol *per account*: the same symbol can be short in one
# account and long in another, and each is its own trade. None is an account
# this login can't see (ENTRY STOP given by hand).
TradeKey = tuple[str, str | None]


class _Book:
    """The trades a tracker owns, keyed by (symbol, account).

    A trade with no account stands for the symbol in whatever single
    account holds it, so it and an account-specific trade of the same
    symbol never coexist: tracking either replaces the other.
    """

    def __init__(self) -> None:
        self.trades: dict[TradeKey, TrackedTrade] = {}

    def _add(self, trade: TrackedTrade) -> TrackedTrade:
        if trade.account is None:
            self.untrack(trade.symbol)
        else:
            self.trades.pop((trade.symbol, None), None)
        self.trades[trade.key] = trade
        return trade

    def for_symbol(self, symbol: str) -> list[TrackedTrade]:
        return [t for (s, _), t in self.trades.items() if s == symbol]

    def untrack(self, symbol: str, account: str | None = None) -> list[TrackedTrade]:
        """Drop `symbol`'s trade in `account`, or in every account when
        `account` is None. Returns what was dropped."""
        gone = [
            t for t in self.for_symbol(symbol)
            if account is None or t.account in (account, None)
        ]
        for t in gone:
            del self.trades[t.key]
        return gone


@dataclass(frozen=True, slots=True)
class TrackAlert:
    kind: AlertKind
    symbol: str
    close: float
    bar_start: datetime
    account: str | None = None


class SellRuleTracker(_Book):
    """Owns every trade currently being tracked -- one per symbol and
    account; re-tracking one replaces it.
    """

    def track(
        self,
        symbol: str,
        entry: float,
        stop: float,
        account: str | None = None,
        opened_at: datetime | None = None,
    ) -> TrackedTrade:
        return self._add(TrackedTrade(symbol, entry, stop, account, opened_at=opened_at))

    def on_close(self, symbol: str, close: float, bar_start: datetime) -> list[TrackAlert]:
        """Every alert `symbol`'s close sets off, one per account at most."""
        alerts = [self._step(t, close, bar_start) for t in self.for_symbol(symbol)]
        return [a for a in alerts if a is not None]

    def _step(self, trade: TrackedTrade, close: float, bar_start: datetime) -> TrackAlert | None:
        if trade.state == "done":
            return None
        symbol, account = trade.key

        if trade.state == "watching":
            if not trade.is_favorable(close):
                return None
            trade.state = "armed"
            trade.peak = close
            trade.armed_at = bar_start
            return TrackAlert("breakeven", symbol, close, bar_start, account)

        # armed: track the running peak, fire once giveback from it hits 0.5R
        assert trade.peak is not None
        trade.peak = trade.more_favorable(trade.peak, close)
        giveback = abs(trade.peak - close)
        if giveback >= 0.5 * trade.r:
            trade.state = "done"
            return TrackAlert("sell", symbol, close, bar_start, account)
        return None


# -- trail: the same trade, managed rather than announced -------------------

TrailActionKind = Literal["breakeven", "exit", "eod"]

# The end-of-day rule: a trailed trade still open one minute before the
# close exits unless it is up at least EOD_HOLD_R -- for days the trader
# can't get back to the desk. Checked on the bar *ending* at that time (the
# 15:58 bar on a normal day), the last close available before it. The close
# comes from IBKR's schedule per trade, so an early close (13:00) moves the
# exit to 12:59; EOD_EXIT is the fallback when the schedule can't be read.
EOD_EXIT = time(15, 59)
EOD_HOLD_R = 2.0


@dataclass(frozen=True, slots=True)
class TrailAction:
    """What the trail rules want done at IBKR after one close. `giveback`
    and `limit` are dollars per share, carried so the alert and the store
    can say why an exit fired without recomputing it."""

    kind: TrailActionKind
    symbol: str
    close: float
    bar_start: datetime
    account: str | None = None
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


class TrailTracker(_Book):
    """The trail rules, on 1-min closes like `SellRuleTracker` but with a
    different giveback rule (`giveback_limit`) that applies from the start,
    not only after +1R: a trade that runs +0.5R and closes back at -0.5R has
    given back 1R and exits instead of waiting for the stop at -1R.

    `peak` starts at entry, so a trade that never trades in its favor
    measures giveback from entry -- which puts the 1R exit at the original
    stop, where the stop order already is. A trail started on a trade
    already under way is handed the best close since entry instead
    (`track(peak=...)`), so a giveback that happened before the command
    still counts.

    Pure: says what should happen. Placing it is `astor.drivers.orders`.
    """

    def __init__(self, eod_exit: time | None = EOD_EXIT) -> None:
        super().__init__()
        self.eod_exit = eod_exit

    def track(
        self,
        symbol: str,
        entry: float,
        stop: float,
        account: str | None = None,
        eod_exit: time | None = None,
        opened_at: datetime | None = None,
        peak: float | None = None,
    ) -> TrackedTrade:
        """`eod_exit` is this trade's own cutoff, from its session's close;
        None takes the tracker's default. `peak` is the best close already
        seen since entry, if any -- never worse than entry itself."""
        trade = TrackedTrade(
            symbol, entry, stop, account, eod_exit=eod_exit or self.eod_exit, opened_at=opened_at
        )
        trade.peak = entry if peak is None else trade.more_favorable(entry, peak)
        return self._add(trade)

    def on_close(
        self, symbol: str, close: float, bar_start: datetime, bar_end: datetime | None = None
    ) -> list[TrailAction]:
        """Every action `symbol`'s close calls for, one per account at most."""
        actions = [self._step(t, close, bar_start, bar_end) for t in self.for_symbol(symbol)]
        return [a for a in actions if a is not None]

    def _step(
        self, trade: TrackedTrade, close: float, bar_start: datetime, bar_end: datetime | None
    ) -> TrailAction | None:
        if trade.state == "done":
            return None
        symbol, account = trade.key
        assert trade.peak is not None
        trade.peak = trade.more_favorable(trade.peak, close)

        # Ahead of breakeven: a trade reaching +1R on this bar still exits
        # if it is under 2R -- there is no session left for the stop to work.
        if (
            trade.eod_exit is not None
            and bar_end is not None
            and bar_end.time() >= trade.eod_exit
            and trade.gain_r(close) < EOD_HOLD_R
        ):
            trade.state = "done"
            return TrailAction("eod", symbol, close, bar_start, account)

        # The exit is checked before breakeven: on a trade that ran past +1R
        # before trail was started, the first close may already have given
        # back too much, and the exit takes the stops with it anyway. On a
        # trade trailed from entry the two can't land on the same close --
        # the close that first reaches +1R is the peak, with nothing given back.
        giveback = abs(trade.peak - close)
        limit = giveback_limit(trade)
        # Rounded first: closes and R are cent prices, and a giveback of
        # exactly the limit must not miss it on float noise.
        if round(giveback, 6) >= round(limit, 6):
            trade.state = "done"
            return TrailAction("exit", symbol, close, bar_start, account, giveback, limit)

        # Off the peak, not this close: a trade handed a peak already past
        # +1R gets its breakeven on the first close, wherever that is.
        if trade.state == "watching" and trade.is_favorable(trade.peak):
            trade.state = "armed"
            trade.armed_at = bar_start
            return TrailAction("breakeven", symbol, close, bar_start, account)
        return None
