"""Setup state machines.

The shape of each setup is fixed; the thresholds are not.

Each setup applies filters at trigger time, all expressed as a flat percent
of the level price (PDL/PDH):

  excursion  how far past the level price actually went. Rejects the bar-scale
             chop that dominated the first replay -- 2 bars, a cent beyond.
  reclaim    how convincingly price came back. Rejects a close that limps a
             cent past the level and means nothing.
  combined   excursion + reclaim together. Catches a move that clears both
             floors individually but is still small overall -- two barely-
             there halves that individually pass but never added up to a real
             move.

These filters used to run in ADR units, or ADR-OR-percent once a flat-percent
fallback was added to catch high-ADR names that ADR alone under-caught (a
single ADR coefficient filters volatile names hardest, backwards from the
intent -- excursion size in ADR units correlated negatively with ADR itself
watchlist-wide). Analysis of replayed triggers found the ADR side added little:
setup strength correlated with the flat percent-of-level move, not the
ADR-scaled one. ADR filtering was dropped in favor of percent-only, with the
combined floor added to catch weak-but-technically-passing pairs that the
individual floors let through.

excursion_adr and reclaim_adr are still recorded on the trigger as pure
diagnostics -- for hindsight, since a name that clears its percent filter
today may or may not have cleared an ADR-scaled one too -- but neither
decides the outcome anymore.

avwap and rvol are recorded the same way: pure diagnostics, not filtered on.
Both are context a future setup will likely want as an actual condition
(price vs. AVWAP, an RVOL floor), but no setup does yet, so for now they are
just visible on every trigger for hindsight. They come from Scanner's running
per-symbol state (astor.core.indicators), not from anything a Setup computes
itself -- a Setup only ever reads them, same relationship it already has
with DailyLevels.

Trade-decision context (avwap_pct, the two stop/position-size pairs, the
session extreme) is the same kind of thing: computed from state the Setup
already has access to (DailyLevels, SessionIndicators, its own local
overshoot/undercut), never filtered on, just there for whoever reads the
trigger. Two stops are offered because they answer different questions: PDH/
PDL is the technical level the setup is anchored to, while the session's
actual high/low is where price really got to -- for a big excursion those
diverge a lot, and a trader sizing off the level alone could be sizing for a
tighter stop than the move already proved it could reach.

A bar that breaches the level always arms the setup. The filters are applied
when it would fire, against the *accumulated* excursion, so an undercut that
deepens over several bars is judged on its full extent rather than its first
bar. Failing a filter leaves the setup armed rather than resetting it: the
breach is still real, we are simply still waiting for a reclaim worth reporting.

Excursion currently has no ceiling. TSLA on 2026-08-14 fired O&F at 0.834x ADR
/ 2.82% excursion -- by the trader's read, that excursion is large enough that
the move was already extended before the fail bar closed, and any real setup
there is AVWAP Loss, not O&F. A floor-only excursion filter cannot distinguish
"a fresh test of the level" from "a stale, already-extended move" -- that is
future work, not yet implemented here.
"""

from __future__ import annotations

from typing import ClassVar

from astor.core.indicators import SessionIndicators, compute_rvol
from astor.core.types import BarEvent, DailyLevels, Trigger, VolumeCurve


def _pct_of_level(amount: float, level_value: float) -> float | None:
    """amount expressed as a percent of the level price, e.g. excursion or
    reclaim distance against PDL/PDH."""
    return round(amount / level_value * 100.0, 4) if level_value > 0 else None


def _min_pct_passes(
    amount: float, level_value: float, min_pct: float
) -> tuple[bool, float | None]:
    """Whether a dollar amount (excursion or reclaim distance) clears a flat
    percent-of-level floor. min_pct <= 0 disables the check."""
    pct = _pct_of_level(amount, level_value)
    passes = min_pct <= 0 or (pct is not None and pct >= min_pct)
    return passes, pct


def _position_size(
    entry: float, stop: float, max_loss: float
) -> tuple[int | None, float | None]:
    """Shares and notional dollars sized so that a stop-out at `stop` loses
    at most `max_loss`, entering at `entry`. (None, None) if that can't be
    done meaningfully: no distance between entry and stop (division by zero,
    or the setup and the stop reference happen to coincide), or the risk per
    share is too large to buy even one share within the budget.
    """
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0 or max_loss <= 0:
        return None, None
    shares = int(max_loss / risk_per_share)
    if shares <= 0:
        return None, None
    return shares, round(shares * entry, 2)


class Setup:
    """One setup tracked for one symbol. Instances are stateful."""

    name: ClassVar[str]
    direction: ClassVar[str]

    def on_bar(
        self,
        bar: BarEvent,
        levels: DailyLevels,
        indicators: SessionIndicators | None = None,
        curve: VolumeCurve | None = None,
    ) -> Trigger | None:
        raise NotImplementedError


class UndercutRally(Setup):
    """Long. Price drops below the previous day's low, then reclaims it.

    ARMED --(bar.low < PDL)--> UNDERCUT --(bar.close > PDL, filters pass)--> fire
    """

    name = "U&R"
    direction = "long"

    def __init__(
        self,
        allow_same_bar_reclaim: bool = False,
        min_excursion_pct: float = 0.0,
        min_reclaim_pct: float = 0.0,
        min_combined_pct: float = 0.0,
        max_loss_dollars: float = 100.0,
    ):
        self.allow_same_bar_reclaim = allow_same_bar_reclaim
        self.min_excursion_pct = min_excursion_pct
        self.min_reclaim_pct = min_reclaim_pct
        self.min_combined_pct = min_combined_pct
        self.max_loss_dollars = max_loss_dollars
        self._undercut_low: float | None = None
        self._undercut_start = None
        self._bars_below = 0  # bars whose low actually broke the level
        self._bars_armed = 0  # bars since the breach, however price behaved

    def on_bar(
        self,
        bar: BarEvent,
        levels: DailyLevels,
        indicators: SessionIndicators | None = None,
        curve: VolumeCurve | None = None,
    ) -> Trigger | None:
        pdl = levels.pdl

        if self._undercut_low is None:
            if bar.low >= pdl:
                return None
            self._undercut_low = bar.low
            self._undercut_start = bar.start
            self._bars_below = 1
            self._bars_armed = 1
            if not (self.allow_same_bar_reclaim and bar.close > pdl):
                return None
        else:
            self._undercut_low = min(self._undercut_low, bar.low)
            self._bars_armed += 1
            # Only bars that actually broke the level count as time spent below
            # it. Counting every armed bar conflates "price held under the level
            # for 39 minutes" with "the setup has been waiting 52 minutes".
            if bar.low < pdl:
                self._bars_below += 1
            if bar.close <= pdl:
                return None

        excursion = pdl - self._undercut_low
        reclaim = bar.close - pdl
        adr = levels.adr

        excursion_ok, excursion_pct = _min_pct_passes(
            excursion, pdl, self.min_excursion_pct
        )
        if not excursion_ok:
            return None  # stay armed: too shallow
        reclaim_ok, reclaim_pct = _min_pct_passes(reclaim, pdl, self.min_reclaim_pct)
        if not reclaim_ok:
            return None  # stay armed: wait for a more convincing reclaim
        combined_pct = (
            round(excursion_pct + reclaim_pct, 4)
            if excursion_pct is not None and reclaim_pct is not None
            else None
        )
        if self.min_combined_pct > 0 and (
            combined_pct is None or combined_pct < self.min_combined_pct
        ):
            return None  # stay armed: each half is fine, the whole move isn't

        avwap = indicators.avwap if indicators else None
        avwap_pct = _pct_of_level(bar.close - avwap, avwap) if avwap else None
        level_shares, level_dollars = _position_size(bar.close, pdl, self.max_loss_dollars)
        session_low = indicators.session_low if indicators else None
        session_extreme_pct = (
            _pct_of_level(bar.close - session_low, session_low) if session_low else None
        )
        session_shares, session_dollars = (
            _position_size(bar.close, session_low, self.max_loss_dollars)
            if session_low
            else (None, None)
        )

        trigger = Trigger(
            symbol=bar.symbol,
            setup=self.name,
            direction=self.direction,
            trigger_price=bar.close,
            bar_start=bar.start,
            bar_end=bar.end,
            level_name="PDL",
            level_value=pdl,
            extras={
                "undercut_low": self._undercut_low,
                "undercut_start": self._undercut_start.isoformat(),
                "bars_below": self._bars_below,
                "bars_armed": self._bars_armed,
                "adr_pct": levels.adr_pct,
                "excursion_adr": round(excursion / adr, 4) if adr > 0 else None,
                "excursion_pct": excursion_pct,
                "reclaim_adr": round(reclaim / adr, 4) if adr > 0 else None,
                "reclaim_pct": reclaim_pct,
                "combined_pct": combined_pct,
                "avwap": avwap,
                "avwap_pct": avwap_pct,
                "rvol": compute_rvol(indicators.cum_volume, bar, curve)
                if indicators and curve
                else None,
                "max_loss_dollars": self.max_loss_dollars,
                "level_stop_shares": level_shares,
                "level_stop_dollars": level_dollars,
                "session_extreme": session_low,
                "session_extreme_pct": session_extreme_pct,
                "session_stop_shares": session_shares,
                "session_stop_dollars": session_dollars,
            },
        )
        self._undercut_low = None
        self._undercut_start = None
        self._bars_below = 0
        self._bars_armed = 0
        return trigger


class OvershootFail(Setup):
    """Short. Price trades above the previous day's high, then loses it.

    ARMED --(bar.high > PDH)--> OVERSHOT --(bar.close < PDH, filters pass)--> fire
    """

    name = "O&F"
    direction = "short"

    def __init__(
        self,
        allow_same_bar_fail: bool = False,
        min_excursion_pct: float = 0.0,
        min_reclaim_pct: float = 0.0,
        min_combined_pct: float = 0.0,
        max_loss_dollars: float = 100.0,
    ):
        self.allow_same_bar_fail = allow_same_bar_fail
        self.min_excursion_pct = min_excursion_pct
        self.min_reclaim_pct = min_reclaim_pct
        self.min_combined_pct = min_combined_pct
        self.max_loss_dollars = max_loss_dollars
        self._overshoot_high: float | None = None
        self._overshoot_start = None
        self._bars_above = 0  # bars whose high actually broke the level
        self._bars_armed = 0  # bars since the breach, however price behaved

    def on_bar(
        self,
        bar: BarEvent,
        levels: DailyLevels,
        indicators: SessionIndicators | None = None,
        curve: VolumeCurve | None = None,
    ) -> Trigger | None:
        pdh = levels.pdh

        if self._overshoot_high is None:
            if bar.high <= pdh:
                return None
            self._overshoot_high = bar.high
            self._overshoot_start = bar.start
            self._bars_above = 1
            self._bars_armed = 1
            if not (self.allow_same_bar_fail and bar.close < pdh):
                return None
        else:
            self._overshoot_high = max(self._overshoot_high, bar.high)
            self._bars_armed += 1
            if bar.high > pdh:
                self._bars_above += 1
            if bar.close >= pdh:
                return None

        excursion = self._overshoot_high - pdh
        reclaim = pdh - bar.close
        adr = levels.adr

        excursion_ok, excursion_pct = _min_pct_passes(
            excursion, pdh, self.min_excursion_pct
        )
        if not excursion_ok:
            return None  # stay armed: too shallow
        reclaim_ok, reclaim_pct = _min_pct_passes(reclaim, pdh, self.min_reclaim_pct)
        if not reclaim_ok:
            return None  # stay armed: wait for a more convincing fail
        combined_pct = (
            round(excursion_pct + reclaim_pct, 4)
            if excursion_pct is not None and reclaim_pct is not None
            else None
        )
        if self.min_combined_pct > 0 and (
            combined_pct is None or combined_pct < self.min_combined_pct
        ):
            return None  # stay armed: each half is fine, the whole move isn't

        avwap = indicators.avwap if indicators else None
        avwap_pct = _pct_of_level(bar.close - avwap, avwap) if avwap else None
        level_shares, level_dollars = _position_size(bar.close, pdh, self.max_loss_dollars)
        session_high = indicators.session_high if indicators else None
        session_extreme_pct = (
            _pct_of_level(bar.close - session_high, session_high) if session_high else None
        )
        session_shares, session_dollars = (
            _position_size(bar.close, session_high, self.max_loss_dollars)
            if session_high
            else (None, None)
        )

        trigger = Trigger(
            symbol=bar.symbol,
            setup=self.name,
            direction=self.direction,
            trigger_price=bar.close,
            bar_start=bar.start,
            bar_end=bar.end,
            level_name="PDH",
            level_value=pdh,
            extras={
                "overshoot_high": self._overshoot_high,
                "overshoot_start": self._overshoot_start.isoformat(),
                "bars_above": self._bars_above,
                "bars_armed": self._bars_armed,
                "adr_pct": levels.adr_pct,
                "excursion_adr": round(excursion / adr, 4) if adr > 0 else None,
                "excursion_pct": excursion_pct,
                "reclaim_adr": round(reclaim / adr, 4) if adr > 0 else None,
                "reclaim_pct": reclaim_pct,
                "combined_pct": combined_pct,
                "avwap": avwap,
                "avwap_pct": avwap_pct,
                "rvol": compute_rvol(indicators.cum_volume, bar, curve)
                if indicators and curve
                else None,
                "max_loss_dollars": self.max_loss_dollars,
                "level_stop_shares": level_shares,
                "level_stop_dollars": level_dollars,
                "session_extreme": session_high,
                "session_extreme_pct": session_extreme_pct,
                "session_stop_shares": session_shares,
                "session_stop_dollars": session_dollars,
            },
        )
        self._overshoot_high = None
        self._overshoot_start = None
        self._bars_above = 0
        self._bars_armed = 0
        return trigger


ALL_SETUPS: list[type[Setup]] = [UndercutRally, OvershootFail]
SETUPS_BY_NAME = {s.name: s for s in ALL_SETUPS}
