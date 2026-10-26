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

from dataclasses import dataclass, field, replace
from datetime import datetime, time
from typing import Literal

from astor.core.exit_plan import ExitPlan, Leg

State = Literal["watching", "armed", "done"]
AlertKind = Literal["breakeven", "sell", "eod", "hold"]


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
    # Which rules take it out, for what share, against what R (exit_plan)
    plan: ExitPlan = field(default_factory=ExitPlan)
    legs_done: set[str] = field(default_factory=set)
    # avwap: armed once a close has been on the trade's side of AVWAP
    avwap_armed: bool = False
    # trail: whole shares per rule (by Leg.key), fixed when it starts
    leg_shares: dict[str, int] = field(default_factory=dict)
    # the close rule has had its say for the day: an exit, or the notice
    # that the trade is held overnight
    eod_done: bool = False

    @property
    def close_level(self) -> float | None:
        """The price the close rule turns on: entry plus the plan's hold R
        (2R unless 'close 2.5R') -- under it at the close a long is sold;
        None with 'close exit'."""
        hold = self.plan.close_hold_r
        if hold is None:
            return None
        return self.entry + hold * self.r if self.direction == "long" else self.entry - hold * self.r

    def __post_init__(self) -> None:
        if self.stop == self.entry:
            raise ValueError(f"{self.symbol}: stop cannot equal entry (R would be zero)")

    @property
    def direction(self) -> str:
        return "long" if self.stop < self.entry else "short"

    @property
    def r(self) -> float:
        """R in dollars a share: the stop distance, times the plan's R
        coefficient (1.5R for a stop set tight)."""
        return abs(self.entry - self.stop) * self.plan.r_mult

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

    def open_leg(self, rule: str) -> Leg | None:
        """The plan's leg for `rule`, if it has one that hasn't fired."""
        leg = self.plan.leg(rule)  # type: ignore[arg-type]
        return leg if leg is not None and rule not in self.legs_done else None

    @property
    def finished(self) -> bool:
        """Every rule has fired and no shares ride the stop."""
        return all(g.key in self.legs_done for g in self.plan.legs)

    def avwap_lost(self, close: float, avwap: float | None, yard: float | None) -> bool:
        """The avwap rule on one close: arms once a close is on the trade's
        side of AVWAP, then fires on a close `avwap_adr` x ADR(1) (`yard`)
        past it the other way. A trade begun on the wrong side waits."""
        leg = self.open_leg("avwap")
        if leg is None or avwap is None or not yard:
            return False
        long = self.direction == "long"
        if not self.avwap_armed:
            self.avwap_armed = close > avwap if long else close < avwap
            return False
        limit = leg.avwap_adr * yard
        return close < avwap - limit if long else close > avwap + limit


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
    rule: str | None = None  # a sell: which rule fired
    # as on TrailAction: why a sell fired, and whether it is all that's left
    giveback: float = 0.0
    limit: float = 0.0
    whole: bool = False


# -- trail: the same trade, managed rather than announced -------------------

TrailActionKind = Literal["breakeven", "exit", "eod", "hold"]

# The end-of-day rule: a trailed trade still open one minute before the
# close exits unless it is up at least its plan's close_hold_r -- 2R
# unless 'close 2.5R' or 'close exit' says otherwise -- for days the trader
# can't get back to the desk. Checked on the bar *ending* at that time (the
# 15:58 bar on a normal day), the last close available before it. The close
# comes from IBKR's schedule per trade, so an early close (13:00) moves the
# exit to 12:59; EOD_EXIT is the fallback when the schedule can't be read.
EOD_EXIT = time(15, 59)


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
    rule: str | None = None  # an exit: which rule's shares go
    # an exit: the last rule, so the whole remaining position goes -- not
    # just its share, which rounding or a partly filled stop may have left
    # short of what is held
    whole: bool = False


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
        plan: ExitPlan | None = None,
        leg_shares: dict[str, int] | None = None,
    ) -> TrackedTrade:
        """`eod_exit` is this trade's own cutoff, from its session's close;
        None takes the tracker's default. `peak` is the best close already
        seen since entry, if any -- never worse than entry itself."""
        trade = TrackedTrade(
            symbol, entry, stop, account, eod_exit=eod_exit or self.eod_exit, opened_at=opened_at,
            plan=plan or ExitPlan(), leg_shares=dict(leg_shares or {}),
        )
        trade.peak = entry if peak is None else trade.more_favorable(entry, peak)
        return self._add(trade)

    def on_close(
        self, symbol: str, close: float, bar_start: datetime, bar_end: datetime | None = None,
        avwap: float | None = None, yard: float | None = None,
    ) -> list[TrailAction]:
        """Every action `symbol`'s close calls for, in the order they are to
        be carried out. `avwap` and `yard` (ADR(1)) feed the avwap rule;
        without them it waits."""
        return [
            a for t in self.for_symbol(symbol)
            for a in self._step(t, close, bar_start, bar_end, avwap, yard)
        ]

    def _step(
        self, trade: TrackedTrade, close: float, bar_start: datetime, bar_end: datetime | None,
        avwap: float | None, yard: float | None,
    ) -> list[TrailAction]:
        if trade.state == "done":
            return []
        symbol, account = trade.key
        assert trade.peak is not None
        trade.peak = trade.more_favorable(trade.peak, close)

        # Ahead of breakeven: a trade reaching +1R on this bar still exits
        # if it is under 2R -- there is no session left for the stop to work.
        # The plan's `close` takes it out at the close whatever the gain.
        held: list[TrailAction] = []
        if (
            trade.eod_exit is not None
            and bar_end is not None
            and bar_end.time() >= trade.eod_exit
            and not trade.eod_done
        ):
            trade.eod_done = True
            if trade.plan.close_hold_r is None or trade.gain_r(close) < trade.plan.close_hold_r:
                trade.state = "done"
                return [TrailAction("eod", symbol, close, bar_start, account)]
            # Held: said once, so the trader knows it was a decision. The
            # other rules still run on this close.
            held = [TrailAction("hold", symbol, close, bar_start, account, limit=trade.close_level or 0.0)]

        # The exits are checked before breakeven: on a trade that ran past
        # +1R before trail was started, the first close may already have
        # given back too much. On a trade trailed from entry the giveback
        # and breakeven can't land on the same close -- the close that first
        # reaches +1R is the peak, with nothing given back.
        actions = held
        if trade.open_leg("gb") is not None:
            giveback = abs(trade.peak - close)
            limit = giveback_limit(trade)
            # Rounded first: closes and R are cent prices, and a giveback of
            # exactly the limit must not miss it on float noise.
            if round(giveback, 6) >= round(limit, 6):
                trade.legs_done.add("gb")
                actions.append(TrailAction("exit", symbol, close, bar_start, account, giveback, limit, "gb"))
        leg = trade.open_leg("avwap")
        if leg is not None and trade.avwap_lost(close, avwap, yard):
            assert avwap is not None and yard is not None
            trade.legs_done.add("avwap")
            actions.append(TrailAction(
                "exit", symbol, close, bar_start, account,
                abs(close - avwap), leg.avwap_adr * yard, "avwap",
            ))
        # A level from the chart: these shares go on a close beyond it
        for g in trade.plan.legs:
            if g.rule != "level" or g.level is None or g.key in trade.legs_done:
                continue
            if (close < g.level) if trade.direction == "long" else (close > g.level):
                trade.legs_done.add(g.key)
                actions.append(TrailAction("exit", symbol, close, bar_start, account,
                                           abs(close - g.level), 0.0, g.key))
        if trade.finished:
            trade.state = "done"
            if actions:
                actions[-1] = replace(actions[-1], whole=True)
            return actions

        # Off the peak, not this close: a trade handed a peak already past
        # +1R gets its breakeven on the first close, wherever that is.
        if trade.state == "watching" and trade.is_favorable(trade.peak):
            trade.state = "armed"
            trade.armed_at = bar_start
            actions.append(TrailAction("breakeven", symbol, close, bar_start, account))
        return actions


class SellRuleTracker(TrailTracker):
    """`track`: trail's rules exactly -- the same giveback, breakeven,
    avwap and close rules, the same plan -- announced instead of acted on.
    Each action trail would take at IBKR becomes a notice for the trader
    to take it by hand, so the two never disagree about a trade.

    (Until 2026-10-19 track had rules of its own: nothing before +1R, then
    a 0.5R giveback off the peak, and no end-of-day rule.)
    """

    _KIND: dict[str, AlertKind] = {"breakeven": "breakeven", "exit": "sell", "eod": "eod", "hold": "hold"}

    def track(  # type: ignore[override]
        self,
        symbol: str,
        entry: float,
        stop: float,
        account: str | None = None,
        opened_at: datetime | None = None,
        plan: ExitPlan | None = None,
        eod_exit: time | None = None,
        peak: float | None = None,
        leg_shares: dict[str, int] | None = None,
    ) -> TrackedTrade:
        return super().track(symbol, entry, stop, account, eod_exit, opened_at, peak, plan, leg_shares)

    def on_close(  # type: ignore[override]
        self, symbol: str, close: float, bar_start: datetime, bar_end: datetime | None = None,
        avwap: float | None = None, yard: float | None = None,
    ) -> list[TrackAlert]:
        return [
            TrackAlert(
                self._KIND[a.kind], a.symbol, a.close, a.bar_start, a.account,
                a.rule, a.giveback, a.limit, a.whole,
            )
            for a in super().on_close(symbol, close, bar_start, bar_end, avwap, yard)
        ]
