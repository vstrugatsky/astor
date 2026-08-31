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

Trade-decision context (avwap_pct, level_stop_pct, the two stop/position-size
pairs, the session extreme) is the same kind of thing: computed from state
the Setup already has access to (DailyLevels, SessionIndicators, its own
local overshoot/undercut), never filtered on, just there for whoever reads
the trigger. Two stops are offered because they answer different questions:
PDH/PDL is the technical level the setup is anchored to, while the session's
actual high/low is where price really got to -- for a big excursion those
diverge a lot, and a trader sizing off the level alone could be sizing for a
tighter stop than the move already proved it could reach.

avwap_pct is signed (positive = above, negative = below) since that's a
meaningfully directional comparison. level_stop_pct and session_extreme_pct
are unsigned: a stop's distance is a positive number a trader thinks in, not
something whose sign should carry the long/short direction -- and a clean
positive distance is also what padding a stop by some percent (likely coming
later) will want to add to.

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

from astor.core.grading import rvol_threshold
from astor.core.indicators import SessionIndicators, compute_rvol
from astor.core.types import BarEvent, DailyLevels, Trigger, VolumeCurve


def _pct_of_level(amount: float, level_value: float) -> float | None:
    """amount expressed as a percent of the level price, e.g. excursion or
    reclaim distance against PDL/PDH."""
    return round(amount / level_value * 100.0, 4) if level_value > 0 else None


def _abs_pct_of_level(amount: float, level_value: float) -> float | None:
    """Magnitude version of _pct_of_level, for stop-distance fields
    (level_stop_pct, session_extreme_pct) -- a trader thinks of a stop's
    distance as a positive number, not something whose sign carries the
    long/short direction. (Left as a separate helper, not baked into those
    two call sites, since padding a stop by some percent is likely coming
    and will want to add to a clean positive distance, not fight a sign.)
    """
    pct = _pct_of_level(amount, level_value)
    return abs(pct) if pct is not None else None


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
        level_stop_pct = _abs_pct_of_level(bar.close - pdl, pdl)
        level_shares, level_dollars = _position_size(bar.close, pdl, self.max_loss_dollars)
        session_open = indicators.session_open if indicators else None
        session_high = indicators.session_high if indicators else None
        session_low = indicators.session_low if indicators else None
        session_extreme_pct = (
            _abs_pct_of_level(bar.close - session_low, session_low) if session_low else None
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
                "level_stop_pct": level_stop_pct,
                "level_stop_shares": level_shares,
                "level_stop_dollars": level_dollars,
                "session_open": session_open,
                "session_high": session_high,
                "session_low": session_low,
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
        level_stop_pct = _abs_pct_of_level(bar.close - pdh, pdh)
        level_shares, level_dollars = _position_size(bar.close, pdh, self.max_loss_dollars)
        session_open = indicators.session_open if indicators else None
        session_high = indicators.session_high if indicators else None
        session_low = indicators.session_low if indicators else None
        session_extreme_pct = (
            _abs_pct_of_level(bar.close - session_high, session_high) if session_high else None
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
                "level_stop_pct": level_stop_pct,
                "level_stop_shares": level_shares,
                "level_stop_dollars": level_dollars,
                "session_open": session_open,
                "session_high": session_high,
                "session_low": session_low,
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


class _ReversalBase(Setup):
    """Shared shape for ReversalLong/ReversalShort ("right side of V"): a
    sharp directional move on real volume, followed by a retracement that
    lands in a middle band -- not so shallow it never really reversed, not
    so deep the whole move already round-tripped -- held long enough to
    look like more than a dead-cat bounce.

    Unlike U&R/O&F, this is not anchored to a static level (PDL/PDH). The
    "leg" is the session's own developing extreme -- SessionIndicators'
    session_low/session_high, continuously updated by Scanner before this
    ever sees the bar. Whenever a new, deeper extreme forms, the leg resets
    completely: a fresh anchor check, a fresh clock, and (per the trader's
    call) any prior retracement progress is void -- the V's bottom just
    moved, so a partial bounce that hadn't fired yet means nothing anymore.

    The leg's anchor is always `session_open` -- the overnight gap and
    `prev_close` play no part here, by the trader's own call. A gap-inclusive
    version of this idea is a distinct setup (Gap Reversal, not yet built),
    not a variant of this one.

    RVOL is evaluated frozen at the extreme bar, not at fire time: by fire
    time more volume has accumulated during the retracement itself, which
    would understate or overstate how unusual the *original move* actually
    was depending on how that later volume behaved. `move_rvol` is that
    frozen figure; `rvol` (the same diagnostic name U&R/O&F use, and what
    Scanner's real-time grading reads) is the ordinary current-bar value.

    A leg fires at most once: after firing, only a new, deeper extreme
    (never the one just fired on) can arm it again. And per the trader's
    call, a leg that already gave back more than max_retrace_pct is dead
    for good -- the right side of the V is gone -- until a new extreme
    starts a new leg from scratch.

    "EXT" is used as level_name (there's no PDH/PDL here, so no 3-letter
    level code makes sense beyond "the extreme"); level_value is the leg's
    own extreme, playing the same stop-anchor role PDL/PDH plays for U&R/O&F.
    session_extreme/session_extreme_pct/session_stop_* are left unset
    (unlike U&R/O&F): there is no second, distinct "technical vs. actual"
    stop to show here since the level *is* the session extreme already --
    a redundant second line would misleadingly imply otherwise.
    """

    name: ClassVar[str]
    direction: ClassVar[str]
    level_name: ClassVar[str] = "EXT"

    def __init__(
        self,
        min_leg_adr: float = 0.5,
        min_retrace_pct: float = 25.0,
        max_retrace_pct: float = 50.0,
        min_confirm_minutes: float = 6.0,
        max_loss_dollars: float = 100.0,
    ):
        self.min_leg_adr = min_leg_adr
        self.min_retrace_pct = min_retrace_pct
        self.max_retrace_pct = max_retrace_pct
        self.min_confirm_minutes = min_confirm_minutes
        self.max_loss_dollars = max_loss_dollars
        self._anchor: float | None = None
        self._extreme: float | None = None
        self._extreme_bar: BarEvent | None = None
        self._extreme_cum_volume: float = 0.0
        self._retrace_dead = False
        self._fired_extreme: float | None = None

    # -- direction-specific hooks, overridden by Long/Short below -----------

    def _session_extreme(self, indicators: SessionIndicators) -> float:
        raise NotImplementedError

    def _more_extreme(self, a: float, b: float) -> float:
        """The more extreme (further from anchor) of two candidates."""
        raise NotImplementedError

    def _retrace_from(self, close: float, extreme: float) -> float:
        """Distance price has moved back toward (or past) the anchor,
        starting from the extreme -- positive once retracement begins."""
        raise NotImplementedError

    # -- shared state machine ------------------------------------------------

    def on_bar(
        self,
        bar: BarEvent,
        levels: DailyLevels,
        indicators: SessionIndicators | None = None,
        curve: VolumeCurve | None = None,
    ) -> Trigger | None:
        if indicators is None or indicators.session_open is None:
            return None

        if self._anchor is None:
            self._anchor = indicators.session_open

        session_extreme = self._session_extreme(indicators)
        if self._extreme is None or self._more_extreme(
            session_extreme, self._extreme
        ) != self._extreme:
            # A new, deeper extreme just formed (or this is the first bar) --
            # the leg resets clean, and there is nothing to check this bar.
            self._extreme = session_extreme
            self._extreme_bar = bar
            self._extreme_cum_volume = indicators.cum_volume
            self._retrace_dead = False
            return None

        if self._retrace_dead or self._extreme == self._fired_extreme:
            return None

        leg_size = abs(self._anchor - self._extreme)
        if leg_size <= 0:
            return None

        retrace = self._retrace_from(bar.close, self._extreme)
        retrace_pct = retrace / leg_size * 100.0

        if retrace_pct > self.max_retrace_pct:
            self._retrace_dead = True
            return None
        if retrace_pct < self.min_retrace_pct:
            return None

        assert self._extreme_bar is not None
        elapsed_minutes = (bar.start - self._extreme_bar.start).total_seconds() / 60.0
        if elapsed_minutes < self.min_confirm_minutes:
            return None

        adr = levels.adr
        if adr <= 0 or leg_size < self.min_leg_adr * adr:
            return None  # too small a move to be "sharp"

        move_rvol = (
            compute_rvol(self._extreme_cum_volume, self._extreme_bar, curve)
            if curve
            else None
        )
        if move_rvol is None or move_rvol < rvol_threshold(levels.adv):
            return None  # not enough volume behind the move

        avwap = indicators.avwap
        avwap_pct = _pct_of_level(bar.close - avwap, avwap) if avwap else None
        level_stop_pct = _abs_pct_of_level(bar.close - self._extreme, self._extreme)
        level_shares, level_dollars = _position_size(
            bar.close, self._extreme, self.max_loss_dollars
        )
        rvol = (
            compute_rvol(indicators.cum_volume, bar, curve)
            if curve
            else None
        )

        trigger = Trigger(
            symbol=bar.symbol,
            setup=self.name,
            direction=self.direction,
            trigger_price=bar.close,
            bar_start=bar.start,
            bar_end=bar.end,
            level_name=self.level_name,
            level_value=self._extreme,
            extras={
                "anchor": self._anchor,
                "leg_extreme": self._extreme,
                "leg_extreme_start": self._extreme_bar.start.isoformat(),
                "leg_size_adr": round(leg_size / adr, 4) if adr > 0 else None,
                "leg_size_pct": _pct_of_level(leg_size, self._anchor),
                "retrace_pct": round(retrace_pct, 4),
                "retrace_minutes": round(elapsed_minutes, 1),
                "move_rvol": move_rvol,
                "adr_pct": levels.adr_pct,
                "avwap": avwap,
                "avwap_pct": avwap_pct,
                "rvol": rvol,
                # Aliased under U&R/O&F's own diagnostic name too, so the
                # live console alert (astor.drivers.live, setup-agnostic)
                # renders a stop line without any Reversal-specific code.
                "reclaim_pct": level_stop_pct,
                "max_loss_dollars": self.max_loss_dollars,
                "level_stop_pct": level_stop_pct,
                "level_stop_shares": level_shares,
                "level_stop_dollars": level_dollars,
                "session_open": indicators.session_open,
                "session_high": indicators.session_high,
                "session_low": indicators.session_low,
            },
        )
        self._fired_extreme = self._extreme
        return trigger


class ReversalLong(_ReversalBase):
    """Long. A sharp move down, on volume, then a 25-50% retracement back up
    held for at least min_confirm_minutes -- the right side of a V."""

    name = "REV-L"
    direction = "long"

    def _session_extreme(self, indicators: SessionIndicators) -> float:
        assert indicators.session_low is not None
        return indicators.session_low

    def _more_extreme(self, a: float, b: float) -> float:
        return min(a, b)

    def _retrace_from(self, close: float, extreme: float) -> float:
        return close - extreme


class ReversalShort(_ReversalBase):
    """Short. A sharp move up, on volume, then a 25-50% retracement back
    down held for at least min_confirm_minutes -- the right side of a
    mirrored (^-shaped) V."""

    name = "REV-S"
    direction = "short"

    def _session_extreme(self, indicators: SessionIndicators) -> float:
        assert indicators.session_high is not None
        return indicators.session_high

    def _more_extreme(self, a: float, b: float) -> float:
        return max(a, b)

    def _retrace_from(self, close: float, extreme: float) -> float:
        return extreme - close


ALL_SETUPS: list[type[Setup]] = [UndercutRally, OvershootFail, ReversalLong, ReversalShort]
SETUPS_BY_NAME = {s.name: s for s in ALL_SETUPS}
