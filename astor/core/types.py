"""The vocabulary shared by every driver and every setup.

BarEvent is deliberately the *only* thing a setup ever sees. Nothing here holds
a reference to a dataset, a broker connection, or a clock -- that is what makes
a Replay run and a Live run indistinguishable from inside a setup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True)
class BarEvent:
    """One completed bar.

    `start` is the bar's timestamp as IBKR reports it (the bar's *start*).
    `end` is when the bar completed, and therefore the earliest instant a live
    system could have evaluated it. Setups are handed only completed bars.
    """

    symbol: str
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class DailyLevels:
    """Prior-session reference levels and volatility. RTH only, per the setups.

    `adr_pct` is the Deepvue-compatible definition -- the mean of (High/Low - 1)
    over the prior N sessions, as a percent. It is stored as a percent rather
    than dollars so it reconciles with the research platform and stays
    comparable across tickers; the dollar figure the filters actually use is
    derived from it against the prior close.
    """

    symbol: str
    session: date  # the session these levels were taken from
    pdh: float
    pdl: float
    adr_pct: float = 0.0
    prev_close: float = 0.0
    adr_sessions: int = 0
    adv: float = 0.0  # average dollar volume (close * volume), same N-session window

    @property
    def adr(self) -> float:
        """ADR in dollars, anchored to the prior close.

        Anchored to the prior close rather than the live price so the filter
        threshold is a constant for the whole session -- otherwise the bar that
        triggers a setup would move the yardstick used to judge it.
        """
        return self.adr_pct / 100.0 * self.prev_close


@dataclass(frozen=True, slots=True)
class VolumeCurve:
    """A symbol's expected cumulative share volume by minute-of-day, averaged
    over its last N RTH sessions.

    RVOL divides a bar's cumulative volume-to-date directly against this --
    an absolute share count per minute bucket, not a percent of the day --
    so no separate ADV figure is needed.

    Keyed by bar *start* time-of-day (naive, tz stripped), matching how bars
    are keyed everywhere else IB minute-of-day alignment matters (e.g.
    ReplayDriver.stream). Sparse: a symbol with fewer than a full session's
    cached bars simply has fewer keys, and lookups for a missing minute
    return None rather than a fabricated value.
    """

    symbol: str
    sessions: int  # how many sessions this curve was actually averaged over
    cum_volume_by_minute: dict[time, float] = field(default_factory=dict)

    def avg_cum_volume_at(self, t: time) -> float | None:
        return self.cum_volume_by_minute.get(t)


@dataclass(frozen=True, slots=True)
class Trigger:
    """A setup firing on a specific bar.

    `extras` carries the context the trader needs to tighten loose rules later
    -- how deep the undercut went, how long it lasted. Recording it now is what
    makes "develop the rules organically" possible without re-running history.
    """

    symbol: str
    setup: str
    direction: str
    trigger_price: float
    bar_start: datetime
    bar_end: datetime
    level_name: str
    level_value: float
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapitulationAlert:
    """A Reversal leg still extending (no retrace yet) that already clears
    the same leg-quality bar the eventual trigger will require -- a heads-up
    to prepare size/entry before the reversal confirms, not a trade signal
    itself. See astor.core.setups._ReversalBase.pop_alert.
    """

    kind: ClassVar[str] = "capitulation"

    symbol: str
    direction: str
    bar_start: datetime
    bar_end: datetime
    price: float
    leg_extreme: float
    anchor: float
    leg_size_adr: float
    move_rvol: float | None
    ema9_extension_adr: float | None
    accel_price: float | None
    accel_vol: float | None
    # What it would take to act on this before the trigger prints: the price
    # the trigger could first fire at, the stop that trade would carry, and
    # the size that pair allows. See _ReversalBase._maybe_flag_capitulation.
    target: float | None
    target_basis: str  # 'ema9' | 'retrace' -- which condition sets the price
    exit_target: float | None  # where the setup expects to be out
    rr: float | None           # reward over risk, from target to exit
    stop: float
    shares: int | None
    dollars: float | None
    max_loss_dollars: float

    def store_extras(self) -> dict[str, Any]:
        """What the alerts table records beyond the columns every alert
        shares (astor.core.store.record_alert)."""
        return {
            "leg_extreme": self.leg_extreme,
            "anchor": self.anchor,
            "leg_size_adr": self.leg_size_adr,
            "move_rvol": self.move_rvol,
            "ema9_extension_adr": self.ema9_extension_adr,
            "accel_price": self.accel_price,
            "accel_vol": self.accel_vol,
            "target": self.target,
            "target_basis": self.target_basis,
            "exit_target": self.exit_target,
            "rr": self.rr,
            "stop": self.stop,
            "level_stop_shares": self.shares,
            "level_stop_dollars": self.dollars,
            "max_loss_dollars": self.max_loss_dollars,
        }


@dataclass(frozen=True, slots=True)
class TouchAlert:
    """An AVWAP touch that has happened, before any continuation has -- the
    same relationship to an AVT trigger that CapitulationAlert has to a
    Reversal one. AVT's whole premise is that price leaves AVWAP quickly, so
    waiting for the recovery bar to print can be too late to react; this
    says "it is at AVWAP now, with the session and the day's range behind
    it" and leaves the decision to the trader.

    One per pullback, not per in-band bar. See
    astor.core.setups._AVWAPTouchBase.pop_alert.
    """

    kind: ClassVar[str] = "avwap_touch"

    symbol: str
    direction: str
    bar_start: datetime
    bar_end: datetime
    price: float
    avwap: float
    touch_adr: float       # signed: which side of AVWAP the wick reached
    depth_adr: float
    day_range_adr: float   # in ADR(14), the "is this day worth joining" gauge
    day_range_pct: float | None  # the same range as a percent, for comparison
    from_open_adr: float
    rvol: float | None
    # Same trade-placement trio as CapitulationAlert: the price an AVT
    # trigger could first fire at (the recovery threshold, off AVWAP), the
    # stop that trade would carry, and the size that pair allows. shares is
    # sized at `target`, not at the touch price -- the touch is not where
    # the trade goes on.
    target: float
    target_basis: str
    exit_target: float | None
    rr: float | None
    stop: float            # where the trade's stop would sit, off AVWAP
    shares: int | None
    dollars: float | None
    max_loss_dollars: float

    def store_extras(self) -> dict[str, Any]:
        return {
            "avwap": self.avwap,
            "touch_adr": self.touch_adr,
            "depth_adr": self.depth_adr,
            "day_range_adr": self.day_range_adr,
            "day_range_pct": self.day_range_pct,
            "from_open_adr": self.from_open_adr,
            "rvol": self.rvol,
            "target": self.target,
            "target_basis": self.target_basis,
            "exit_target": self.exit_target,
            "rr": self.rr,
            "stop": self.stop,
            "level_stop_shares": self.shares,
            "level_stop_dollars": self.dollars,
            "max_loss_dollars": self.max_loss_dollars,
        }


# Either kind of setup-produced alert. Both carry `kind` and store_extras(),
# which is all a driver needs to record one; printing is the only place that
# cares which is which.
SetupAlert = CapitulationAlert | TouchAlert
