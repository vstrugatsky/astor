"""Setup state machines.

Two live setups, betting on opposite things about the same tape. Reversal
(REV-L/REV-S, "right side of a V") fades a leg that has exhausted itself;
AVWAP touch (AVT-L/AVT-S) joins one that is still running, after it pulls
back to daily AVWAP and turns back. They share no state, and both can be
armed on the same symbol at the same time.

U&R and O&F (breach-a-level-then-reclaim-it) were retired after review found
almost none of their real triggers were actionable; the trader's own read is
that a reversal is only worth trading when the leg that precedes it shows
capitulation characteristics -- sharp, on real volume, extended from the
9EMA, and built quickly -- which U&R/O&F's flat excursion/reclaim-of-a-level
framing never captured. Their old breach+reclaim condition survives as a
diagnostic on every Reversal trigger (`meets_ur_condition`/
`meets_of_condition`, computed against PDL/PDH), for a possible future grade
bump -- not a filter.

The shape of the setup is fixed; the thresholds are not, and several of them
(the 9EMA-extension floor, the acceleration ratios) are new enough that they
are recorded on every trigger whether or not they end up gating anything --
the same treatment avwap/rvol got when they were first added, so a threshold
can be picked from replay history instead of guessed once and left alone.

A bar that breaches the level always arms the setup. Filters are applied
when it would fire, against the *accumulated* state, so a move that keeps
building is judged on its full extent rather than its first bar. Failing a
filter leaves the setup armed rather than resetting it: the move is still
real, we are simply still waiting for a fire worth reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import ClassVar

from astor.core.grading import rvol_threshold
from astor.core.indicators import SessionIndicators, compute_rvol
from astor.core.types import (
    BarEvent,
    CapitulationAlert,
    DailyLevels,
    SetupAlert,
    TouchAlert,
    Trigger,
    VolumeCurve,
)


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


def _pctstr(x: float | None) -> str:
    return f"{x:.2f}%" if x is not None else "n/a"


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


@dataclass(frozen=True, slots=True)
class _FilterReadout:
    """Every filter a Reversal candidate has to clear, with the threshold it
    is being held to -- computed once per candidate bar and printed on every
    diagnostic line that bar produces (astor.cli's `explain`).

    One readout shared by all of them, rather than each rejection hand-listing
    the filters that happened to pass before it: a filter that rejects early
    used to hide every number after it, which is exactly what you need when
    you're deciding whether a threshold is set right.
    """

    retrace_pct: float
    min_retrace_pct: float
    max_retrace_pct: float
    elapsed_minutes: float
    min_confirm_minutes: float
    leg_duration_minutes: float
    min_leg_minutes: float
    leg_size: float
    leg_adr: float | None
    min_leg_adr: float
    leg_pct: float | None
    min_leg_pct: float
    min_leg_pct_adr: float
    move_rvol: float | None
    min_move_rvol: float
    ema9_extension_adr: float | None
    min_ema9_extension_adr: float
    accel_price: float | None
    min_accel_price: float
    accel_vol: float | None
    min_accel_vol: float
    # No threshold: recorded and shown, not filtered on, until there is
    # enough of it to know what a good value looks like.
    pace_adr1: float | None
    pace_adr: float | None
    rr: float | None

    def line(self) -> str:
        """Every filter as `value/threshold`, in the order they are checked."""

        def v(x: float | None, nd: int = 2) -> str:
            return f"{x:.{nd}f}" if x is not None else "n/a"

        return "  ".join(
            [
                f"retrace {self.retrace_pct:.1f}%/{self.min_retrace_pct:g}-{self.max_retrace_pct:g}",
                f"elapsed {self.elapsed_minutes:.0f}m/{self.min_confirm_minutes:g}m",
                f"built {self.leg_duration_minutes:.1f}m/{self.min_leg_minutes:g}m",
                f"leg {self.leg_size:.2f} ({v(self.leg_adr)}x/{self.min_leg_adr:g}x ADR, "
                f"{v(self.leg_pct)}%/{self.min_leg_pct:g}% at {self.min_leg_pct_adr:g}x)",
                f"move_rvol {v(self.move_rvol)}/{self.min_move_rvol:g}",
                f"ema9_ext {v(self.ema9_extension_adr, 4)}/{self.min_ema9_extension_adr:g}",
                f"accel_price {v(self.accel_price, 4)}/{self.min_accel_price:g}",
                f"accel_vol {v(self.accel_vol)}/{self.min_accel_vol:g}",
                # ADR(14), spelled out: adr_pct and ADR(1) are both in play
                # elsewhere in this output and a bare "ADR" reads ambiguously.
                # Per *minute*, not per bar -- the divisor is
                # leg_duration_minutes, so the two only coincide at the
                # default --bar-size of 1 min.
                f"pace {v(self.pace_adr, 5)} ADR(14)/min",
                f"R:R {v(self.rr, 1)}",
            ]
        )


def _trace(trace: list[dict] | None, bar: BarEvent, text: str) -> None:
    """Append one human-readable diagnostic line, if a trace list was passed
    in (astor.cli's `explain` command). A no-op otherwise -- every setup's
    real detection path calls this unconditionally at each decision point,
    so the diagnostic can never drift out of sync with what actually
    decided a trigger (or didn't)."""
    if trace is not None:
        trace.append({"time": bar.start, "text": text})


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


# Reversal's own RVOL floor -- deliberately separate from grading.rvol_threshold
# (shared by real-time grading; U&R/O&F used to share it too before they were
# retired). Same ADV-tiered shape, higher thresholds: the trader's read is
# that a capitulation-quality leg needs to stand out more than a routine
# level breach did.
#
# The ADV boundaries are 20% below the round numbers they stand for ($500M
# and $5B), because the ADV they are compared against is computed from IBKR
# volume (astor.drivers.replay.compute_adv), which excludes trades executed
# outside the NBBO and so runs low against every other platform's figure for
# the same name. The discount goes here rather than into compute_adv itself:
# only these liquidity tiers care about the absolute dollar figure, while
# every other consumer of IBKR volume is relative (RVOL divides IBKR volume
# by an average of IBKR volume, so the understatement cancels out) and would
# be made *wrong* by inflating the input.
_REV_RVOL_TIERS = ((4_000_000_000, 1.5), (400_000_000, 1.75))
_REV_RVOL_DEFAULT = 2.0


def _leg_is_big_enough(
    leg_size: float, adr: float, anchor: float,
    min_leg_adr: float, min_leg_pct: float, min_leg_pct_adr: float,
) -> bool:
    """Whether a leg is a big enough move to be worth fading, by either of
    two routes.

    ADR alone is the primary test, but it misreads a high-ADR name: a stock
    that routinely swings 10% has an ADR wide enough that a genuinely large
    move can still come in under the multiple. Real cases, 2026-09-28:
    SHMD's 8.33% move measured 0.85x ADR and SRFM's 11.63% measured 0.76x,
    both rejected as "too small" while being exactly the kind of move the
    setup wants.

    So a leg also qualifies on percent -- but only with an ADR floor still
    under it (min_leg_pct_adr), because percent alone would admit a sleepy
    name's unremarkable drift as readily as a violent one's capitulation.
    Set min_leg_pct to 0 to switch the second route off entirely.
    """
    if adr <= 0 or leg_size <= 0:
        return False
    leg_adr = leg_size / adr
    if leg_adr >= min_leg_adr:
        return True
    leg_pct = _pct_of_level(leg_size, anchor)
    return (
        min_leg_pct > 0
        and leg_adr >= min_leg_pct_adr
        and leg_pct is not None
        and leg_pct >= min_leg_pct
    )


def _pace_adr1(size: float | None, minutes: float, day_range: float) -> float | None:
    """How fast a move was built: its size per minute, as a fraction of the
    day's own range.

    Size alone says nothing about urgency. QSI 2026-10-02 put together a
    leg over three and a half hours; the same leg in twenty minutes is a
    different event, and only the second is the violent flush Reversal is
    meant to fade. Expressed in ADR(1) per minute so it is comparable
    across names, and recorded rather than filtered on -- what separates a
    good leg from a slow one is exactly what there is not enough data to
    say yet.

    Recorded but NOT displayed: for an ordinary leg this collapses to
    1/minutes (see _pace_adr), so showing it would just restate
    leg_duration_minutes, which `explain` already prints as "built Nm".
    pace_adr is what the readout carries.

    `size` is _push_size(), NOT leg_size: it has to span the same stretch
    of time `minutes` does or the ratio means nothing. See _push_size.
    """
    if size is None or minutes <= 0 or day_range <= 0 or size <= 0:
        return None
    return round(size / minutes / day_range, 5)


def _pace_adr(size: float | None, minutes: float, adr: float) -> float | None:
    """The same pace against the prior-session average range instead.

    Recorded alongside _pace_adr1 because for an ordinary Reversal leg the
    ADR(1) version carries no information: such a leg runs between the
    session's own extremes by construction (_session_anchor and
    _session_extreme *are* session_high and session_low), so its size
    equals the day's range and the ratio collapses to 1/minutes exactly --
    true of all twenty REV triggers recorded through 2026-10-02. Against
    ADR(14) the size survives, so a big fast leg and a small fast one are
    told apart: QSI 2026-10-02 (2.04x ADR in 125m) and PACB the same day
    (0.82x in 127m) are indistinguishable in ADR(1) and 2.5x apart here.

    The collapse is a property of the leg *definition*, not of ADR(1): a
    revived leg measures from its own turnaround (see _push_size), its
    push is a genuine sub-move of the day, and the ADR(1) version carries
    information again. pace_adr1 != 1/minutes is exactly the signature of
    a revived leg.

    Both stay recorded; which one predicts better is an empirical question
    there is not enough data to answer yet.
    """
    if size is None or minutes <= 0 or adr <= 0 or size <= 0:
        return None
    return round(size / minutes / adr, 5)


def _reward_to_risk(entry: float, stop: float, exit_price: float) -> float | None:
    """How many times the risk the move is worth, if it goes the way the
    setup says.

    Risk is the distance to the stop; reward is the distance to where the
    setup expects to be out. None when there is no risk to divide by, or
    when the exit sits the wrong side of the entry -- a setup whose target
    is already behind price has nothing to say about reward.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    reward = (exit_price - entry) if entry > stop else (entry - exit_price)
    return round(reward / risk, 2) if reward > 0 else None


def rev_rvol_threshold(adv: float) -> float:
    """The RVOL floor a Reversal leg's move must clear, highest-liquidity
    tier first. Shared between the final retrace filter and the earlier
    capitulation-in-progress check -- both are asking the same "was there
    real volume behind this move" question, just at different points in the
    leg's life.
    """
    for floor, threshold in _REV_RVOL_TIERS:
        if adv > floor:
            return threshold
    return _REV_RVOL_DEFAULT


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
        trace: list[dict] | None = None,
    ) -> Trigger | None:
        raise NotImplementedError

    def pop_alert(self) -> SetupAlert | None:
        """An alert this setup raised on the bar just fed, distinct from a
        Trigger: Reversal's capitulation heads-up, AVT's AVWAP touch. No-op
        default so Scanner can call this on every setup uniformly without
        caring which ones implement it."""
        return None


class _ReversalBase(Setup):
    """Shared shape for ReversalLong/ReversalShort ("right side of V"): a
    sharp directional move on real volume, extended from the 9EMA, built
    quickly -- followed by a retracement (or an EMA9 reclaim) held long
    enough to look like more than a dead-cat bounce.

    This is not anchored to a static level (PDL/PDH) or even a fixed
    session_open. Both ends of the leg are running values off
    SessionIndicators, continuously updated by Scanner before this ever sees
    the bar: `extreme` is the running extreme in the setup's own direction
    (session_low for REV-L, session_high for REV-S) and `anchor` is the
    running extreme in the *opposite* direction (session_high for REV-L,
    session_low for REV-S). Both are seeded straight from bar 1's actual
    high and low (via SessionIndicators.update, before this ever sees the
    bar) -- not bar 1's open, which can differ from its high/low
    substantially and isn't used here at all. A REV-S candidate that dips to
    a fresh session low right after the open, before the actual up-leg
    begins, gets that low as its anchor, since that's where the up-leg
    actually started -- not wherever price happened to open.

    Whenever *either* boundary moves, the leg resets completely: a fresh
    clock, and (per the trader's call) any prior retracement progress is
    void -- the leg's shape just changed, so a partial bounce measured
    against the old shape means nothing anymore. `_leg_bars` (used for the
    acceleration diagnostic, see ReversalLong/ReversalShort's direction
    hooks and the fire path below) follows the same rule: reset to just the
    current bar on a fresh anchor, extended (not reset) while only the
    extreme keeps deepening, and left untouched once neither boundary moves
    -- which freezes it at exactly "every bar of this push, anchor to
    extreme" the moment a retrace candidate bar arrives.

    Timing (when extreme/anchor were set, and the cumulative volume and 9EMA
    at that moment) is read fresh from SessionIndicators at fire time -- via
    `_extreme_at`/`_extreme_cum_volume_at`/`_extreme_ema9_at`/`_anchor_at` --
    rather than tracked locally against `bar`. A Setup added to Live
    mid-session starts with no local history (by design -- it must not
    re-detect a trigger from before it was tracked), so a local "the current
    bar is when this extreme was set" would be wrong for whichever boundary
    had already stabilized before the symbol joined; SessionIndicators stays
    continuous through backfill regardless, so it always has the true
    timestamp. The RVOL/9EMA freeze point only follows `extreme`, not
    `anchor` -- that's still specifically "the moment the push ended," and by
    fire time more volume has accumulated (and price has drifted back toward
    the EMA) during the retrace itself, which would misstate how unusual the
    *original* move actually was. `leg_duration_minutes` (anchor's true
    timestamp to extreme's) is computed the same way, at fire time -- how
    long the whole push actually took, a slow multi-hour drift being a much
    weaker "sharp move" than a fast one even at the same price size, and now
    also a floor (`min_leg_minutes`) rather than just a diagnostic.

    The overnight gap and `prev_close` play no part here, by the trader's
    own call -- a gap-inclusive version of this idea is a distinct setup
    (Gap Reversal, not yet built), not a variant of this one.

    A leg fires at most once: after firing, only a move in either boundary
    away from what was fired on can arm it again. And per the trader's
    call, a leg that already gave back more than max_retrace_pct is dead
    for good -- the right side of the V is gone, regardless of whether price
    has also reclaimed the 9EMA by then -- until a new extreme or anchor
    starts a new leg from scratch.

    "EXT" is used as level_name (there's no PDH/PDL here, so no 3-letter
    level code makes sense beyond "the extreme"); level_value is the leg's
    own extreme, playing the same stop-anchor role PDL/PDH plays for the
    retired U&R/O&F. session_extreme/session_extreme_pct/session_stop_* are
    left unset (unlike U&R/O&F): there is no second, distinct "technical vs.
    actual" stop to show here since the level *is* the session extreme
    already -- a redundant second line would misleadingly imply otherwise.
    """

    name: ClassVar[str]
    direction: ClassVar[str]
    level_name: ClassVar[str] = "EXT"
    # Dynamic extras key for the retired setup's condition -- "meets_ur_condition"
    # for REV-L (U&R was long), "meets_of_condition" for REV-S (O&F was short).
    legacy_name: ClassVar[str]

    def __init__(
        self,
        min_leg_adr: float = 0.9,
        min_leg_pct: float = 6.0,
        min_leg_pct_adr: float = 0.6,
        min_retrace_pct: float = 10.0,
        max_retrace_pct: float = 50.0,
        min_confirm_minutes: float = 0.0,
        min_leg_minutes: float = 4.0,
        min_ema9_extension_adr: float = 0.125,
        min_alert_move_adr: float = 0.01,
        min_accel_price: float = 1.2,
        min_accel_vol: float = 1.0,
        accel_window: int = 3,
        max_loss_dollars: float = 100.0,
    ):
        self.min_leg_adr = min_leg_adr
        self.min_leg_pct = min_leg_pct
        self.min_leg_pct_adr = min_leg_pct_adr
        self.min_retrace_pct = min_retrace_pct
        self.max_retrace_pct = max_retrace_pct
        self.min_confirm_minutes = min_confirm_minutes
        self.min_leg_minutes = min_leg_minutes
        self.min_ema9_extension_adr = min_ema9_extension_adr
        self.min_alert_move_adr = min_alert_move_adr
        self.min_accel_price = min_accel_price
        self.min_accel_vol = min_accel_vol
        self.accel_window = accel_window
        self.max_loss_dollars = max_loss_dollars
        # Both running values -- see the class docstring. anchor is the
        # running extreme in the OPPOSITE direction from extreme. Only the
        # values live here; timing is read fresh from SessionIndicators.
        self._anchor: float | None = None
        self._extreme: float | None = None
        self._retrace_dead = False
        self._fired_extreme: float | None = None
        self._fired_anchor: float | None = None
        # Every bar of the current push, anchor to extreme -- see the class
        # docstring's note on _leg_bars. Used only for the accel_price/
        # accel_vol diagnostics.
        self._leg_bars: list[BarEvent] = []
        # While the leg is dead, tracks the retracement's own running local
        # extreme (the close, in the same direction _more_extreme_anchor
        # already compares) and every bar since it was last set -- so a
        # later revival can seed _leg_bars from where the retracement
        # actually turned around, not from whenever the stale old extreme
        # eventually gets re-broken (which can be many bars later). See
        # on_bar's _leg_bars reset logic for how this gets consumed.
        self._dead_extreme: float | None = None
        self._dead_bars: list[BarEvent] = []
        # When the push currently being measured actually began -- normally
        # the true anchor's own timestamp, but reset to the dead leg's own
        # retracement turnaround (self._dead_bars[0]'s timestamp) on a
        # revival from a genuine death, mirroring _leg_bars' reset exactly.
        # Feeds leg_duration_minutes/min_leg_minutes below: the anchor VALUE
        # (self._anchor) stays the real technical level regardless (still
        # useful as a stop reference), but "how long did this push take"
        # shouldn't count the dead, unrelated stretch before the revival.
        self._push_started_at: datetime | None = None
        # The price as of _push_started_at -- self._anchor on an ordinary
        # leg, the dead leg's retracement turnaround on a revived one. Set
        # in lockstep with _push_started_at in every branch below so the
        # two always describe the same instant. Exists only so `pace` can
        # measure a size over the same stretch of time it divides by; see
        # _push_size.
        self._push_started_price: float | None = None
        # The push_started_at a capitulation alert was already fired for --
        # one alert per push, not per deepening extreme (a fast leg can set
        # a new extreme almost every bar; re-alerting on each of those is
        # noise, not a fresh heads-up). Re-arms only when push_started_at
        # itself changes: a genuinely new leg (new anchor) or a revival
        self._pending_capitulation_alert: CapitulationAlert | None = None
        # (target, stop) of the last alert actually sent for this leg, at the
        # precision the ticket prints them. A refresh that would read
        # identically is not a correction, so it is not sent.
        self._alerted_levels: tuple[float, float] | None = None

    # -- direction-specific hooks, overridden by Long/Short below -----------

    def _session_extreme(self, indicators: SessionIndicators) -> float:
        raise NotImplementedError

    def _more_extreme(self, a: float, b: float) -> float:
        """The more extreme (further from anchor) of two candidates."""
        raise NotImplementedError

    def _extreme_at(self, indicators: SessionIndicators) -> datetime:
        """When the current `_session_extreme` value was set -- session_low_at
        for REV-L, session_high_at for REV-S."""
        raise NotImplementedError

    def _extreme_cum_volume_at(self, indicators: SessionIndicators) -> float:
        """Cumulative volume as of `_extreme_at`."""
        raise NotImplementedError

    def _extreme_ema9_at(self, indicators: SessionIndicators) -> float | None:
        """The 9EMA reading frozen at `_extreme_at` -- ema9_at_low for
        REV-L, ema9_at_high for REV-S."""
        raise NotImplementedError

    def _extreme_close_at(self, indicators: SessionIndicators) -> float | None:
        """The close that best represents how far the push actually got --
        close_before_low for REV-L, close_before_high for REV-S. The
        extreme bar's own close if that bar was still going the leg's own
        direction (a genuine continuation), otherwise the bar before it (the
        extreme bar itself already reversed within itself, so its own close
        would measure the reversal candle instead of the push). Never the
        high/low wick the extreme itself is tracked by, since the EMA line
        is never computed from a wick either -- comparing it to one would
        mix conventions."""
        raise NotImplementedError

    def _ema9_extension(self, extreme_close: float, ema9_at_extreme: float) -> float:
        """How far the leg's extreme finished (by the representative close
        described in _extreme_close_at, not its high/low wick) from the
        frozen 9EMA, in
        the capitulation direction -- positive means further extended
        (below the EMA for REV-L, above it for REV-S)."""
        raise NotImplementedError

    def _ema9_reclaimed_now(self, close: float, ema9_now: float) -> bool:
        """Whether the *current* bar's close has reclaimed the 9EMA on the
        favorable side -- above it for REV-L, below it for REV-S. Uses the
        live ema9, not the frozen one: this is "has price come back by now,"
        not "how extended was the original move"."""
        raise NotImplementedError

    def _session_anchor(self, indicators: SessionIndicators) -> float:
        """The running extreme in the direction OPPOSITE to `_session_extreme`
        -- session_high for REV-L, session_low for REV-S."""
        raise NotImplementedError

    def _more_extreme_anchor(self, a: float, b: float) -> float:
        """The more extreme of two anchor candidates -- the opposite
        comparator from `_more_extreme`."""
        raise NotImplementedError

    def _anchor_at(self, indicators: SessionIndicators) -> datetime:
        """When the current `_session_anchor` value was set -- the opposite
        of `_extreme_at`."""
        raise NotImplementedError

    def _retrace_from(self, close: float, extreme: float) -> float:
        """Distance price has moved back toward (or past) the anchor,
        starting from the extreme -- positive once retracement begins."""
        raise NotImplementedError

    def _legacy_condition(self, levels: DailyLevels, extreme: float, close: float) -> bool:
        """Whether this leg also satisfies the retired U&R (REV-L) / O&F
        (REV-S) breach+reclaim condition against PDL/PDH -- recorded under
        `legacy_name`, not filtered on."""
        raise NotImplementedError

    def _entry_target(
        self, indicators: SessionIndicators, leg_size: float
    ) -> tuple[float, str]:
        """Where the trigger could first fire, and which condition sets it.

        Two conditions can open it -- a min_retrace_pct retrace off the
        extreme, or an EMA9 reclaim -- and whichever price comes first going
        the trade's way is the one to name. The EMA9 leg is a snapshot: the
        line moves every bar, so this is where it would fire *if the EMA9
        stood still*, which it will not. The retrace leg is fixed as long as
        the leg's own boundaries are.

        Shared by the capitulation alert and the filter readout so the price
        quoted while waiting is the price the readout reasons about.
        """
        retrace_target = self._retrace_target(self._extreme, leg_size)
        ema9_now = indicators.ema9
        if ema9_now is not None and self._nearer_target(ema9_now, retrace_target) == ema9_now:
            return ema9_now, "ema9"
        return retrace_target, "retrace"

    def _retrace_target(self, extreme: float, leg_size: float) -> float:
        """The price a min_retrace_pct retracement off the extreme reaches --
        one of the two ways the trigger can open."""
        raise NotImplementedError

    def _nearer_target(self, a: float, b: float) -> float:
        """Whichever of two candidate trigger prices price reaches first
        going the trade's way: the lower for a long retracing up off its
        low, the higher for a short."""
        raise NotImplementedError

    def _extends_push(self, bar: BarEvent) -> bool:
        """Whether this bar is still pushing the leg's way -- close beyond
        open in the push's own direction (down for REV-L, up for REV-S),
        regardless of whether it set a fresh extreme."""
        raise NotImplementedError

    def _reverses_push(self, bar: BarEvent) -> bool:
        """Whether this bar closed against the leg -- the direction the
        reversal itself would go (up for REV-L, down for REV-S). Not simply
        `not _extends_push`: a doji closes neither way, and so is neither
        still pushing nor yet reversing."""
        raise NotImplementedError

    # -- shared state machine ------------------------------------------------

    def _leg_ended_at(self, extreme_at: datetime) -> datetime:
        """When the push stopped pushing -- the close of the last bar still
        going the leg's way.

        Two things this is deliberately not. It is not the extreme bar's
        *start*: a leg of N one-minute bars occupies N minutes of market
        time, and measuring start-to-start counts the gaps between bars
        instead, one short. It is not the extreme bar either: a bar that
        closes the push's way without taking out the extreme (2026-09-16
        ADPT 09:34, o 28.62 c 28.65 under a 28.72 high) is still the leg
        working, as the trader reads it off a chart -- so the run of such
        bars immediately after the extreme counts toward how long the leg
        took to build. The first bar that closes back against the push ends
        it, and nothing later re-opens it.

        The extreme itself is untouched by any of this -- it stays the high
        (or low) price the leg reached, which is what retrace, accel and the
        stop are all measured against.
        """
        ended_at = None
        for b in self._leg_bars:
            if b.start <= extreme_at:
                ended_at = b.end
            elif ended_at is not None and self._extends_push(b):
                ended_at = b.end
            else:
                break
        # No bar of the push itself on hand (a setup bootstrapped mid-leg):
        # fall back to the extreme's own timestamp rather than inventing a
        # bar length for it.
        return ended_at if ended_at is not None else extreme_at

    def _leg_duration_minutes(self, push_started_at: datetime, extreme_at: datetime) -> float:
        """How long the leg took to build: the first push bar's open to the
        last pushing bar's close. Shared by the trigger's own filter and the
        capitulation-in-progress alert, so both read the same number."""
        return abs((self._leg_ended_at(extreme_at) - push_started_at).total_seconds()) / 60.0

    def _push_size(self) -> float | None:
        """The price distance covered over exactly the stretch
        `_leg_duration_minutes` measures: push start to extreme.

        On an ordinary leg this *is* leg_size -- push_started_at is the
        anchor's own timestamp, so the push spans anchor to extreme.

        On a revived leg the two diverge, and `pace` was reading the
        divergence as speed. push_started_at jumps forward to the dead
        leg's retracement turnaround while self._anchor stays the stale
        session-wide level it has held all day, so pace divided the whole
        day's range by only the revival's duration -- a leg that ground
        out its range over four hours and then revived in six minutes
        scored as one of the fastest of the session. The numerator has to
        span the same clock the denominator does.

        Deliberately scoped to pace alone. leg_size still spans anchor to
        extreme everywhere else, because that is what the leg-size filter,
        the stop, and R:R all mean by "the leg" -- so nothing here changes
        which triggers fire.
        """
        if self._push_started_price is None or self._extreme is None:
            return None
        return abs(self._push_started_price - self._extreme)

    def on_bar(
        self,
        bar: BarEvent,
        levels: DailyLevels,
        indicators: SessionIndicators | None = None,
        curve: VolumeCurve | None = None,
        trace: list[dict] | None = None,
    ) -> Trigger | None:
        if indicators is None or indicators.session_open is None:
            return None

        session_extreme = self._session_extreme(indicators)
        session_anchor = self._session_anchor(indicators)

        extreme_moved = self._extreme is None or self._more_extreme(
            session_extreme, self._extreme
        ) != self._extreme
        anchor_moved = self._anchor is None or self._more_extreme_anchor(
            session_anchor, self._anchor
        ) != self._anchor
        # Captured before self._extreme/self._anchor are updated below: was
        # the leg, as it stood coming into this bar, already dead (retraced
        # past max_retrace_pct)? A deeper extreme arriving right after that
        # is a fresh push starting from scratch, not a continuation of the
        # old one -- _leg_bars needs to reset there too, not just on a new
        # anchor. Real case that motivated this: a leg dies past the
        # retrace ceiling, price consolidates for half an hour with the
        # anchor untouched, then a genuinely new, much faster push sets a
        # deeper extreme -- without this, that new push's accel_price/
        # accel_vol got compared against one stale leftover bar from the
        # earlier, unrelated, dead push instead of its own bars.
        #
        # Deliberately NOT reset just because the leg already fired once:
        # a leg that fires, then keeps extending to a deeper extreme
        # without ever dying, is -- by the trader's own call -- still the
        # same continuing move (worth a second alert on, per the re-fire
        # behavior below), not a new attempt. Real case: FSLY 2026-09-09's
        # REV-S fired at 09:59, kept climbing, and fired again at 10:06 on
        # the same anchor -- accel_price/accel_vol for the second fire
        # should cover the whole 09:36-10:05 push, not just the few bars
        # since the first fire.
        #
        # Also deliberately NOT gated on push_active (below) --
        # accel_price/accel_vol are meant to cover the same anchor-to-
        # extreme span leg_duration_minutes reports, not a narrower window.
        # Real case that showed this: SEDG 2026-09-10's REV-S leg spent
        # 09:37-10:55 (78 bars) with its anchor (session low) still setting
        # fresh lows of its own while its extreme (session high) sat on a
        # stale level from before that whole down-then-up move even
        # started -- by the trader's own read, that stale high is REV-L's
        # territory (the leg down), not relevant to scoping REV-S's leg up,
        # so those bars still belong in this leg's accel sample even though
        # push_active (below) is false for all of them.
        leg_was_dead = self._retrace_dead

        if extreme_moved:
            self._extreme = session_extreme
        if anchor_moved:
            self._anchor = session_anchor

        if anchor_moved:
            # A genuinely new leg -- push_started_at is the true anchor's
            # own timestamp.
            self._leg_bars = [bar]
            self._push_started_at = self._anchor_at(indicators)
            self._push_started_price = self._anchor
            self._dead_extreme = None
            self._dead_bars = []
        elif extreme_moved and leg_was_dead:
            if self._dead_bars:
                # The revival's push very likely began earlier than this
                # bar -- right where the dead leg's own retracement turned
                # around (self._dead_bars, tracked below), not wherever the
                # stale old (session-wide) extreme happens to finally get
                # re-broken. Real case: DELL 2026-09-03's REV-S leg died at
                # 09:45, its retracement bottomed at 09:48 and climbed
                # immediately -- but session_high (monotonic, untouched by
                # the death) wasn't re-broken until 09:58, 9 bars after the
                # real push actually began, which had been starving
                # accel_price/accel_vol of exactly the bars that mattered.
                self._leg_bars = self._dead_bars + [bar]
                self._push_started_at = self._dead_bars[0].start
                # Where that turnaround actually was: _dead_extreme is the
                # retracement's own furthest close, set on the bar right
                # before _dead_bars started accumulating, so it is the
                # price at _dead_bars[0].start.
                self._push_started_price = (
                    self._dead_extreme
                    if self._dead_extreme is not None
                    else self._dead_bars[0].open
                )
            else:
                # No observable bars of the turn before the break itself --
                # the revival happened in a single bar (dead retracement's
                # own local extreme was still moving right up to the bar
                # before this one). push_started_at is THIS bar's own
                # timestamp, not the stale, pre-death anchor_at: that anchor
                # timestamp belongs to a leg that already died, and using it
                # here would silently resurrect it -- both as a much-too-
                # long leg_duration_minutes and, for the capitulation alert,
                # as a false "already alerted for this push" match against
                # the dead leg's own earlier alert.
                self._leg_bars = [bar]
                self._push_started_at = bar.start
                # The whole push happened inside this one bar, so it
                # started at this bar's own open -- unless the retracement
                # had already registered a turnaround close (the previous
                # bar's, i.e. the price at bar.start), which is the same
                # instant and the more faithful level.
                self._push_started_price = (
                    self._dead_extreme if self._dead_extreme is not None else bar.open
                )
            self._dead_extreme = None
            self._dead_bars = []
        elif not leg_was_dead:
            # Append every bar of a currently-alive leg, not just the ones
            # that individually set a fresh extreme -- a real push often
            # pauses for a few bars between new highs/lows without dying
            # (retrace still under max_retrace_pct), and those pause bars
            # are still part of its history. Appending only on extreme_moved
            # silently dropped them, which starved accel_price/accel_vol of
            # data on exactly the legs worth measuring: real cases had only
            # 3 (or fewer) of ~40+ actual bars individually set a new high,
            # so "recent" and "earlier" ended up drawn from a handful of
            # cherry-picked bars rather than the leg's real pace.
            self._leg_bars.append(bar)
        else:
            # Still dead: track the retracement's own running local extreme
            # (the close, compared the same direction _more_extreme_anchor
            # already uses -- retracing is a move toward the anchor's own
            # side) and every bar since it was last set. Mirrors _leg_bars'
            # own reset/append shape exactly, just scoped to the dead period.
            dead_extreme_moved = self._dead_extreme is None or self._more_extreme_anchor(
                bar.close, self._dead_extreme
            ) != self._dead_extreme
            if dead_extreme_moved:
                # This bar itself IS the new local extreme of the
                # retracement (still moving toward the anchor) -- not yet a
                # bar of the turn back toward the anchor's opposite side, so
                # it doesn't belong in _dead_bars. Only bars that arrive
                # *after* the local extreme stops moving are the turn
                # itself. Real case that pinned this down: a retracement
                # that bottoms out and the very next bar already breaks back
                # past the old extreme (no bars in between) must seed the
                # revived leg with a clean [] here, not that single bottom
                # bar -- else a same-bar-as-fired-earlier bar from the dead
                # leg's own tail (session-wide monotonic anchor never
                # having moved) leaks into the revived leg's history.
                self._dead_extreme = bar.close
                self._dead_bars = []
            else:
                self._dead_bars.append(bar)

        extreme_at = self._extreme_at(indicators)
        anchor_at = self._anchor_at(indicators)
        # Whether a genuine push -- a move away from the anchor to a fresh
        # extreme -- exists yet, i.e. the extreme's own timestamp is more
        # recent than the anchor's. This gates only whether retrace/death is
        # evaluated below (see the "not push_active" check further down),
        # never _leg_bars -- see the comment above for why those are scoped
        # differently. While the anchor is still catching up past an older,
        # unrelated extreme (SEDG again: the same 78 bars above), "retrace
        # since the extreme" measures nothing real -- price never actually
        # reached that extreme this time around, so a close sitting near the
        # anchor reads as a huge retrace by pure arithmetic (extreme far
        # away, anchor-to-extreme gap still small), not because anything
        # rallied and failed.
        push_active = extreme_at > anchor_at

        if extreme_moved or anchor_moved:
            # Either boundary just moved (or this is the first bar) -- the
            # leg's shape changed, so it resets clean; nothing to check yet.
            self._retrace_dead = False
            leg_size_now = abs(self._anchor - self._extreme)
            if extreme_moved and anchor_moved:
                what = f"new extreme {self._extreme:.2f} and new anchor {self._anchor:.2f}"
            elif extreme_moved:
                what = f"new extreme {self._extreme:.2f} (anchor {self._anchor:.2f})"
            else:
                what = f"anchor moved to {self._anchor:.2f} (extreme {self._extreme:.2f})"
            _trace(trace, bar, f"{what} -- leg={leg_size_now:.2f}")
            if push_active:
                self._maybe_flag_capitulation(bar, levels, indicators, curve)
            # The extreme and the reversal can be one bar: price runs the
            # leg's way in the first seconds and comes back hard in the
            # last ones. Such a bar -- a fresh extreme on the same anchor,
            # closing against its own open -- goes on to be judged as a
            # candidate like any other, on the same thresholds (trader's
            # call, 2026-10-11). Every other boundary move still only resets
            # the leg: a new anchor is a new leg, not a reversal of one.
            same_bar_reversal = (
                extreme_moved and not anchor_moved and push_active and self._reverses_push(bar)
            )
            if not same_bar_reversal:
                return None
        else:
            same_bar_reversal = False

        if not push_active:
            # Anchor hasn't been overtaken by a fresh extreme yet -- see
            # push_active above. Stay armed silently; there is nothing here
            # to trace or judge, since no real push exists to evaluate yet.
            return None

        if self._retrace_dead or (
            self._extreme == self._fired_extreme and self._anchor == self._fired_anchor
        ):
            return None

        leg_size = abs(self._anchor - self._extreme)
        if leg_size <= 0:
            return None

        retrace = self._retrace_from(bar.close, self._extreme)
        retrace_pct = retrace / leg_size * 100.0

        # Still a live capitulation on this bar -- neither dead nor fired --
        # so refresh the heads-up. The numbers it carries go stale every
        # bar: the 9EMA leg of the target redraws, and a deepening extreme
        # moves both the target and the stop. An order resting off the
        # first alert of a leg would be working the wrong levels minutes
        # later, which is the whole reason this repeats rather than
        # announcing once per push. A same-bar reversal already refreshed
        # it above.
        if not same_bar_reversal:
            self._maybe_flag_capitulation(bar, levels, indicators, curve)

        if retrace_pct > self.max_retrace_pct:
            self._retrace_dead = True
            # The leg just died: drop the pending ticket, and forget the
            # levels so a revival alerts again even at the same prices.
            self._pending_capitulation_alert = None
            self._alerted_levels = None
            _trace(
                trace, bar,
                f"retrace {retrace_pct:.1f}% > max {self.max_retrace_pct}% -- "
                f"leg dead until a new, deeper extreme",
            )
            return None

        # Only a bar that closed *against* the leg is a candidate at all: the
        # setup is the reversal, so entering off a bar that closed the leg's
        # own way is entering into the push still running. Checked before any
        # filter -- such a bar is not a candidate being rejected, it is not a
        # candidate. Deliberately after the death check above, which is about
        # where price got to, not what shape the bar was. A doji (close ==
        # open) closes against nothing and is not a candidate either.
        if not self._reverses_push(bar):
            _trace(
                trace, bar,
                f"no candidate @ {bar.close:.2f}: bar did not close against the leg "
                f"(o {bar.open:.2f} c {bar.close:.2f}) -- stay armed",
            )
            return None

        ema9_now = indicators.ema9
        reclaimed = ema9_now is not None and self._ema9_reclaimed_now(bar.close, ema9_now)

        extreme_at = self._extreme_at(indicators)
        anchor_at = self._anchor_at(indicators)
        # Duration is measured from push_started_at, not the raw anchor_at:
        # a leg that died and later revived shouldn't have the dead,
        # unrelated stretch in between counted as part of "how long this
        # push took" -- see _push_started_at's own comment in __init__.
        push_started_at = self._push_started_at if self._push_started_at is not None else anchor_at
        elapsed_minutes = (bar.start - extreme_at).total_seconds() / 60.0
        leg_duration_minutes = self._leg_duration_minutes(push_started_at, extreme_at)
        push_size = self._push_size()

        # Every filter's input is computed here, before the first check, so
        # a rejection can report the whole picture instead of only what it
        # got as far as measuring. Nothing here has side effects, and the
        # work is a dict lookup plus a walk of the last few leg bars.
        adr = levels.adr
        ema9_at_extreme = self._extreme_ema9_at(indicators)
        extreme_close = self._extreme_close_at(indicators)
        move_rvol = (
            compute_rvol(self._extreme_cum_volume_at(indicators), extreme_at, curve)
            if curve
            else None
        )
        move_rvol_floor = rev_rvol_threshold(levels.adv)
        ema9_extension_adr = (
            round(self._ema9_extension(extreme_close, ema9_at_extreme) / adr, 4)
            if adr > 0 and ema9_at_extreme is not None and extreme_close is not None
            else None
        )
        accel_price, accel_vol = self._compute_accel(extreme_at, extreme_close)
        readout = _FilterReadout(
            retrace_pct=retrace_pct,
            min_retrace_pct=self.min_retrace_pct,
            max_retrace_pct=self.max_retrace_pct,
            elapsed_minutes=elapsed_minutes,
            min_confirm_minutes=self.min_confirm_minutes,
            leg_duration_minutes=leg_duration_minutes,
            min_leg_minutes=self.min_leg_minutes,
            leg_size=leg_size,
            leg_adr=leg_size / adr if adr > 0 else None,
            min_leg_adr=self.min_leg_adr,
            min_leg_pct=self.min_leg_pct,
            min_leg_pct_adr=self.min_leg_pct_adr,
            leg_pct=_pct_of_level(leg_size, self._anchor),
            move_rvol=move_rvol,
            min_move_rvol=move_rvol_floor,
            ema9_extension_adr=ema9_extension_adr,
            min_ema9_extension_adr=self.min_ema9_extension_adr,
            accel_price=accel_price,
            min_accel_price=self.min_accel_price,
            accel_vol=accel_vol,
            min_accel_vol=self.min_accel_vol,
            pace_adr1=_pace_adr1(
                push_size,
                leg_duration_minutes,
                indicators.session_high - indicators.session_low,
            ),
            pace_adr=_pace_adr(push_size, leg_duration_minutes, adr),
            rr=_reward_to_risk(
                self._entry_target(indicators, leg_size)[0],
                self._extreme,
                self._session_anchor(indicators),
            ),
        )

        def _candidate(verdict: str) -> None:
            _trace(trace, bar, f"candidate @ {bar.close:.2f}: {readout.line()} -- {verdict}")

        if retrace_pct < self.min_retrace_pct and not reclaimed:
            ema9_str = f"{ema9_now:.2f}" if ema9_now is not None else "n/a"
            _candidate(f"HOLD retrace: below min and EMA9 ({ema9_str}) not reclaimed -- stay armed")
            return None

        if elapsed_minutes < self.min_confirm_minutes:
            _candidate("FAIL elapsed: too early, not held long enough since the extreme")
            return None

        if leg_duration_minutes < self.min_leg_minutes:
            _candidate("FAIL built: too fast to be a real leg")
            return None

        if not _leg_is_big_enough(
            leg_size, adr, self._anchor,
            self.min_leg_adr, self.min_leg_pct, self.min_leg_pct_adr,
        ):
            _candidate(
                f"FAIL leg: needs {self.min_leg_adr}x ADR, or "
                f"{self.min_leg_pct_adr}x with a {self.min_leg_pct:g}% move -- "
                f"neither route clears"
                if adr > 0
                else "FAIL leg: no ADR for this symbol, cannot size the leg"
            )
            return None  # too small a move to be "sharp"

        if move_rvol is None or move_rvol < move_rvol_floor:
            _candidate("FAIL move_rvol: too thin, not enough volume behind the move")
            return None  # not enough volume behind the move

        if self.min_ema9_extension_adr > 0 and (
            ema9_extension_adr is None or ema9_extension_adr < self.min_ema9_extension_adr
        ):
            _candidate("FAIL ema9_ext: not extended enough from the 9EMA at the extreme")
            return None

        if self.min_accel_price > 0 and (
            accel_price is None or accel_price < self.min_accel_price
        ):
            _candidate("FAIL accel_price: not accelerating into the extreme")
            return None

        if self.min_accel_vol > 0 and (
            accel_vol is None or accel_vol < self.min_accel_vol
        ):
            _candidate("FAIL accel_vol: volume not building into the extreme")
            return None

        # The trigger is the answer the alert was waiting for; do not send
        # both for the same bar -- except on a same-bar reversal. Its alert
        # carries the new extreme as the stop, which no earlier alert had,
        # and with trigger notifications off (live.NOTIFY_TRIGGERS) it is
        # the only word of that stop the trader gets.
        if not same_bar_reversal:
            self._pending_capitulation_alert = None
        self._alerted_levels = None

        legacy_condition = self._legacy_condition(levels, self._extreme, bar.close)

        _trace(
            trace, bar,
            f"FIRE @ {bar.close:.2f}: {readout.line()} -- all filters pass"
            f"{' (retrace below min, via EMA9 reclaim)' if reclaimed and retrace_pct < self.min_retrace_pct else ''}",
        )

        avwap = indicators.avwap
        avwap_pct = _pct_of_level(bar.close - avwap, avwap) if avwap else None
        level_stop_pct = _abs_pct_of_level(bar.close - self._extreme, self._extreme)
        level_shares, level_dollars = _position_size(
            bar.close, self._extreme, self.max_loss_dollars
        )
        rvol = (
            compute_rvol(indicators.cum_volume, bar.start, curve)
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
                "anchor_start": anchor_at.isoformat(),
                "leg_extreme": self._extreme,
                "leg_extreme_start": extreme_at.isoformat(),
                "leg_size_adr": round(leg_size / adr, 4) if adr > 0 else None,
                "leg_size_pct": _pct_of_level(leg_size, self._anchor),
                "push_started_at": push_started_at.isoformat(),
                "leg_duration_minutes": round(leg_duration_minutes, 1),
                "retrace_pct": round(retrace_pct, 4),
                "retrace_minutes": round(elapsed_minutes, 1),
                "retrace_via": "ema9_reclaim" if reclaimed and retrace_pct < self.min_retrace_pct else "pct",
                "pace_adr1": _pace_adr1(
                    push_size,
                    leg_duration_minutes,
                    indicators.session_high - indicators.session_low,
                ),
                "pace_adr": _pace_adr(push_size, leg_duration_minutes, adr),
                "exit_target": round(self._session_anchor(indicators), 4),
                "rr": _reward_to_risk(
                    bar.close, self._extreme, self._session_anchor(indicators)
                ),
                "move_rvol": move_rvol,
                "ema9_at_extreme": ema9_at_extreme,
                "extreme_close": extreme_close,
                "ema9_extension_adr": ema9_extension_adr,
                "accel_price": accel_price,
                "accel_vol": accel_vol,
                self.legacy_name: legacy_condition,
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
        self._fired_anchor = self._anchor
        return trigger

    def _compute_accel(
        self, extreme_at: datetime, extreme_close: float | None
    ) -> tuple[float | None, float | None]:
        """accel_price/accel_vol -- shared by the fire path and the
        capitulation-in-progress alert so both gate on (and report) the
        same numbers, computed the same way.

        _leg_bars keeps accumulating past the extreme (a retrace-candidate
        bar can't be told apart from a mid-push pause in real time, so both
        get appended -- see on_bar's accumulation comment); filtering by the
        already-known extreme_at here cleanly excludes any retrace bars
        regardless of how many came before, leaving exactly the push itself.

        accel_price: net close-to-close displacement per bar, recent vs.
        earlier -- not average bar range. Real case that motivated this:
        DELL 2026-09-02's REV-L leg opened with several equally-wide,
        choppy bars (the initial sharp drop), which made the leg's
        *average bar range* a poor baseline -- the last 3 bars' genuine
        ~$9 net move looked unremarkable (0.96) next to that already-wide
        opening, even though net progress had clearly sped up. Rate-based
        and close-to-close (matching the EMA9/retrace convention) cancels
        out wick noise and rewards actual directional speed: needs at
        least 2 earlier bars to form even one earlier "step"; below that,
        same neutral fallback as too-few-bars ever was.

        The recent window's endpoint is extreme_close, not recent[-1]'s own
        close: recent[-1] *is* the extreme bar (push_bars always ends
        there), so if it was itself a reversal candle, its raw close would
        understate the move the exact same way it did for ema9_extension_adr
        -- extreme_close already picks the representative close (its own if
        a genuine continuation, the bar before it otherwise), so reusing it
        here keeps both diagnostics consistent. Real case: FSLY 2026-09-09
        REV-S 10:06 -- the recent window closed choppy and nearly flat
        (10:05 gave back most of its own spike), reading accel_price 0.27,
        while the actual push (via extreme_close, the close before that
        reversal) was still accelerating at 1.42.

        accel_vol has no directional analog (volume doesn't net out), so
        it stays an average-per-bar comparison, same "not enough bars"
        fallback as before.
        """
        push_bars = [b for b in self._leg_bars if b.start <= extreme_at]
        recent = push_bars[-self.accel_window :]
        earlier = push_bars[: -self.accel_window]

        if len(earlier) >= 2:
            recent_end = extreme_close if extreme_close is not None else recent[-1].close
            recent_move = abs(recent_end - earlier[-1].close)
            earlier_move = abs(earlier[-1].close - earlier[0].close)
            earlier_rate = earlier_move / (len(earlier) - 1)
            recent_rate = recent_move / self.accel_window
            accel_price = round(recent_rate / earlier_rate, 4) if earlier_rate > 0 else None
        else:
            accel_price = 1.0

        earlier_for_vol = earlier or push_bars
        recent_vol, earlier_vol = _mean([b.volume for b in recent]), _mean(
            [b.volume for b in earlier_for_vol]
        )
        accel_vol = (
            round(recent_vol / earlier_vol, 4)
            if recent_vol is not None and earlier_vol
            else None
        )
        return accel_price, accel_vol

    # -- capitulation-in-progress alert --------------------------------------

    def _maybe_flag_capitulation(
        self,
        bar: BarEvent,
        levels: DailyLevels,
        indicators: SessionIndicators,
        curve: VolumeCurve | None,
    ) -> None:
        """Same leg-quality bar the eventual trigger requires -- size,
        duration, volume, 9EMA extension -- checked while the leg is still
        extending (extreme still moving), so the trader gets a heads-up
        before any retrace even starts. Deliberately not gated on retrace
        or the confirm wait; those are about the reversal being confirmed,
        not about whether the move itself already looks like capitulation.

        One alert per push (see push_started_at above), not per deepening
        extreme -- a fast leg can set a new extreme on almost every bar,
        and re-alerting on each of those is noise, not a fresh heads-up.
        Re-arms only on a genuinely new leg (new anchor) or a revival after
        a real death, same as the trigger's own leg_duration_minutes.
        """
        assert self._anchor is not None and self._extreme is not None
        extreme_at = self._extreme_at(indicators)
        anchor_at = self._anchor_at(indicators)
        push_started_at = self._push_started_at if self._push_started_at is not None else anchor_at

        leg_size = abs(self._anchor - self._extreme)
        adr = levels.adr
        if not _leg_is_big_enough(
            leg_size, adr, self._anchor,
            self.min_leg_adr, self.min_leg_pct, self.min_leg_pct_adr,
        ):
            return

        leg_duration_minutes = self._leg_duration_minutes(push_started_at, extreme_at)
        if leg_duration_minutes < self.min_leg_minutes:
            return
        push_size = self._push_size()

        move_rvol = (
            compute_rvol(self._extreme_cum_volume_at(indicators), extreme_at, curve)
            if curve
            else None
        )
        if move_rvol is None or move_rvol < rev_rvol_threshold(levels.adv):
            return

        ema9_at_extreme = self._extreme_ema9_at(indicators)
        extreme_close = self._extreme_close_at(indicators)
        ema9_extension_adr = (
            self._ema9_extension(extreme_close, ema9_at_extreme) / adr
            if ema9_at_extreme is not None and extreme_close is not None
            else None
        )
        if self.min_ema9_extension_adr > 0 and (
            ema9_extension_adr is None or ema9_extension_adr < self.min_ema9_extension_adr
        ):
            return

        accel_price, accel_vol = self._compute_accel(extreme_at, extreme_close)
        if self.min_accel_price > 0 and (
            accel_price is None or accel_price < self.min_accel_price
        ):
            return
        if self.min_accel_vol > 0 and (
            accel_vol is None or accel_vol < self.min_accel_vol
        ):
            return

        # Where the trigger could first fire, so the alert can be acted on
        # with a resting stop order instead of waiting for the bar to print.
        # Two conditions can open it -- a min_retrace_pct retrace off the
        # extreme, or an EMA9 reclaim -- and whichever price comes first
        # going the trade's way is the one to name. The EMA9 leg of that is
        # a snapshot: the line moves every bar, so this is where it would
        # fire *if the EMA9 stood still*, which it will not. The retrace leg
        # is fixed as long as the leg's own boundaries are.
        target, target_basis = self._entry_target(indicators, leg_size)
        # Only a *changed* ticket is worth sending, and the two halves of it
        # change for different reasons. The stop is the leg's extreme: when
        # it moves the trade has genuinely moved, so any move at ticket
        # precision re-alerts. The target carries the 9EMA, which drifts a
        # cent or two on its own while nothing is happening -- so that half
        # has to move by min_alert_move_adr before it counts. Without the
        # split, 25 of 62 alerts on 2026-09-21 were a static stop next to a
        # target wobbling with the EMA.
        if self._alerted_levels is not None:
            prev_target, prev_stop = self._alerted_levels
            stop_moved = round(self._extreme, 2) != round(prev_stop, 2)
            target_moved = abs(target - prev_target) > self.min_alert_move_adr * adr
            if not (stop_moved or target_moved):
                return
        self._alerted_levels = (target, self._extreme)
        shares, dollars = _position_size(target, self._extreme, self.max_loss_dollars)
        # Where the trade expects to be out: back at the other end of the
        # day. _session_anchor is exactly that -- the high for a long fading
        # a flush, the low for a short fading a spike.
        exit_target = self._session_anchor(indicators)
        self._pending_capitulation_alert = CapitulationAlert(
            symbol=bar.symbol,
            direction=self.direction,
            bar_start=bar.start,
            bar_end=bar.end,
            price=bar.close,
            leg_extreme=self._extreme,
            anchor=self._anchor,
            leg_size_adr=round(leg_size / adr, 4),
            move_rvol=move_rvol,
            ema9_extension_adr=(
                round(ema9_extension_adr, 4) if ema9_extension_adr is not None else None
            ),
            accel_price=accel_price,
            accel_vol=accel_vol,
            target=round(target, 4),
            target_basis=target_basis,
            exit_target=round(exit_target, 4),
            rr=_reward_to_risk(target, self._extreme, exit_target),
            pace_adr1=_pace_adr1(
                push_size,
                leg_duration_minutes,
                indicators.session_high - indicators.session_low,
            ),
            pace_adr=_pace_adr(push_size, leg_duration_minutes, adr),
            stop=round(self._extreme, 4),
            shares=shares,
            dollars=dollars,
            max_loss_dollars=self.max_loss_dollars,
            session_high=indicators.session_high,
            session_low=indicators.session_low,
        )

    def pop_alert(self) -> CapitulationAlert | None:
        alert, self._pending_capitulation_alert = self._pending_capitulation_alert, None
        return alert


class ReversalLong(_ReversalBase):
    """Long. A sharp move down, on volume, extended from the 9EMA, then a
    retracement back up (or an EMA9 reclaim) held for at least
    min_confirm_minutes -- the right side of a V."""

    name = "REV-L"
    direction = "long"
    legacy_name = "meets_ur_condition"

    def _session_extreme(self, indicators: SessionIndicators) -> float:
        assert indicators.session_low is not None
        return indicators.session_low

    def _more_extreme(self, a: float, b: float) -> float:
        return min(a, b)

    def _extreme_at(self, indicators: SessionIndicators) -> datetime:
        assert indicators.session_low_at is not None
        return indicators.session_low_at

    def _extreme_cum_volume_at(self, indicators: SessionIndicators) -> float:
        assert indicators.cum_volume_at_low is not None
        return indicators.cum_volume_at_low

    def _extreme_ema9_at(self, indicators: SessionIndicators) -> float | None:
        return indicators.ema9_at_low

    def _extreme_close_at(self, indicators: SessionIndicators) -> float | None:
        return indicators.close_before_low

    def _ema9_extension(self, extreme_close: float, ema9_at_extreme: float) -> float:
        return ema9_at_extreme - extreme_close

    def _ema9_reclaimed_now(self, close: float, ema9_now: float) -> bool:
        return close > ema9_now

    def _session_anchor(self, indicators: SessionIndicators) -> float:
        assert indicators.session_high is not None
        return indicators.session_high

    def _more_extreme_anchor(self, a: float, b: float) -> float:
        return max(a, b)

    def _anchor_at(self, indicators: SessionIndicators) -> datetime:
        assert indicators.session_high_at is not None
        return indicators.session_high_at

    def _retrace_from(self, close: float, extreme: float) -> float:
        return close - extreme

    def _retrace_target(self, extreme: float, leg_size: float) -> float:
        return extreme + leg_size * self.min_retrace_pct / 100.0

    def _nearer_target(self, a: float, b: float) -> float:
        return min(a, b)  # REV-L retraces upward, so the lower price comes first

    def _extends_push(self, bar: BarEvent) -> bool:
        return bar.close < bar.open  # REV-L's leg is the move down

    def _reverses_push(self, bar: BarEvent) -> bool:
        return bar.close > bar.open  # ...so REV-L enters off an up bar

    def _legacy_condition(self, levels: DailyLevels, extreme: float, close: float) -> bool:
        return extreme < levels.pdl and close > levels.pdl


class ReversalShort(_ReversalBase):
    """Short. A sharp move up, on volume, extended from the 9EMA, then a
    retracement back down (or an EMA9 reclaim) held for at least
    min_confirm_minutes -- the right side of a mirrored (^-shaped) V."""

    name = "REV-S"
    direction = "short"
    legacy_name = "meets_of_condition"

    def _session_extreme(self, indicators: SessionIndicators) -> float:
        assert indicators.session_high is not None
        return indicators.session_high

    def _more_extreme(self, a: float, b: float) -> float:
        return max(a, b)

    def _extreme_at(self, indicators: SessionIndicators) -> datetime:
        assert indicators.session_high_at is not None
        return indicators.session_high_at

    def _extreme_cum_volume_at(self, indicators: SessionIndicators) -> float:
        assert indicators.cum_volume_at_high is not None
        return indicators.cum_volume_at_high

    def _extreme_ema9_at(self, indicators: SessionIndicators) -> float | None:
        return indicators.ema9_at_high

    def _extreme_close_at(self, indicators: SessionIndicators) -> float | None:
        return indicators.close_before_high

    def _ema9_extension(self, extreme_close: float, ema9_at_extreme: float) -> float:
        return extreme_close - ema9_at_extreme

    def _ema9_reclaimed_now(self, close: float, ema9_now: float) -> bool:
        return close < ema9_now

    def _session_anchor(self, indicators: SessionIndicators) -> float:
        assert indicators.session_low is not None
        return indicators.session_low

    def _more_extreme_anchor(self, a: float, b: float) -> float:
        return min(a, b)

    def _anchor_at(self, indicators: SessionIndicators) -> datetime:
        assert indicators.session_low_at is not None
        return indicators.session_low_at

    def _retrace_from(self, close: float, extreme: float) -> float:
        return extreme - close

    def _retrace_target(self, extreme: float, leg_size: float) -> float:
        return extreme - leg_size * self.min_retrace_pct / 100.0

    def _nearer_target(self, a: float, b: float) -> float:
        return max(a, b)  # REV-S retraces downward, so the higher price comes first

    def _extends_push(self, bar: BarEvent) -> bool:
        return bar.close > bar.open  # REV-S's leg is the move up

    def _reverses_push(self, bar: BarEvent) -> bool:
        return bar.close < bar.open  # ...so REV-S enters off a down bar

    def _legacy_condition(self, levels: DailyLevels, extreme: float, close: float) -> bool:
        return extreme > levels.pdh and close < levels.pdh


@dataclass(frozen=True, slots=True)
class _AVTReadout:
    """Every filter an AVWAP-touch candidate has to clear, with its
    threshold -- same role _FilterReadout plays for Reversal, printed on
    every diagnostic line that bar produces (astor.cli's `explain`).
    """

    day_range_adr: float
    min_day_range_adr: float
    # The same day range as a percent of itself -- the actual size of the
    # move, next to the multiple. Reversal states its leg both ways for
    # exactly this reason (see _leg_is_big_enough); whether AVT wants the
    # same two-route rule is the open question this is here to answer.
    day_range_pct: float | None
    # How much of the range figure came from the overnight gap rather than
    # from today's own high-low. None when the gap added nothing -- saying
    # "incl. 0.00x gap" on every other line would be noise.
    gap_adr: float | None
    rvol: float | None
    min_rvol: float
    touch_adr: float | None  # signed: which side of AVWAP the wick reached
    touch_band_adr: float
    depth_adr: float | None
    min_depth_adr: float
    from_open_adr: float  # signed the trade's way: is the session itself with us
    touch_at: str | None
    recovery_adr: float
    target: float | None
    rr: float | None
    min_day_range_pct: float
    min_day_range_pct_adr: float
    bars_since_touch: int | None
    max_bars_since_touch: int
    already_past_target: bool

    def line(self) -> str:
        def v(x: float | None, nd: int = 3) -> str:
            return f"{x:.{nd}f}" if x is not None else "n/a"

        bars = "none" if self.bars_since_touch is None else f"{self.bars_since_touch}"
        return "  ".join(
            [
                f"range {self.day_range_adr:.2f}x/{self.min_day_range_adr:g}x ADR"
                + f" ({v(self.day_range_pct, 2)}%/{self.min_day_range_pct:g}% at "
                + f"{self.min_day_range_pct_adr:g}x)"
                + (f" incl. {self.gap_adr:.2f}x gap" if self.gap_adr else ""),
                f"rvol {v(self.rvol, 2)}/{self.min_rvol:g}",
                f"from open {self.from_open_adr:+.3f}",
                f"touch {v(self.touch_adr)}/±{self.touch_band_adr:g}"
                + (f" @{self.touch_at}" if self.touch_at else ""),
                f"depth {v(self.depth_adr)}/{self.min_depth_adr:g}",
                f"recovery {v(self.recovery_adr)}"
                + (f" -> target {self.target:.2f}" if self.target is not None else "")
                + (f"  R:R {self.rr:.1f}" if self.rr is not None else ""),
                f"bars since touch {bars}/{self.max_bars_since_touch}",
                f"already past target {'yes' if self.already_past_target else 'no'}",
            ]
        )


# How far outside the touch band still counts as worth mentioning in a
# trace. Diagnostic only -- it changes nothing about what fires, so it is a
# constant rather than another knob.
_NEAR_TOUCH = 3.0


class _AVWAPTouchBase(Setup):
    """AVT-L/AVT-S -- a significant move on elevated volume, a pullback that
    touches daily AVWAP, then a quick continuation the way the move was
    already going. The entry is that continuation; the appeal is that the
    stop sits just the other side of AVWAP, so the risk is small relative to
    the move already proven.

    Distinct from Reversal in what it is betting on: Reversal fades an
    exhausted leg, this one joins a live one. They share no state and can
    both be armed on the same symbol at once.

    The rule that does the most work here is the last one, and it is not
    about distance. A pullback that merely *sits* beyond the recovery
    threshold never qualifies -- the trigger bar has to cross into it from
    the other side. Without that, two things fire that plainly should not:
    price drifting flat while AVWAP descends onto it (2026-09-18 FIVN 09:42
    -- the gap to AVWAP closed entirely from the AVWAP side, price highs
    were flat across the six bars), and a single wick up to AVWAP from
    price that had been parked 0.4 ADR away for twelve minutes, closing
    back where it started (FIVN 14:52). Both clear every distance test.
    Neither is a continuation, because nothing continued.
    """

    name: ClassVar[str]
    direction: ClassVar[str]

    def __init__(
        self,
        min_day_range_adr: float = 0.8,
        min_day_range_pct: float = 6.0,
        min_day_range_pct_adr: float = 0.6,
        max_yard_pct: float = 10.0,
        touch_band_adr: float = 0.05,
        min_depth_adr: float = 0.15,
        recovery_adr: float = 0.05,
        max_bars_since_touch: int = 5,
        max_break_adr: float = 0.05,
        rearm_adr: float | None = 0.15,
        stop_adr: float = 0.05,
        earliest_touch: time = time(9, 34),
        require_deeper_touch: bool = True,
        exit_depth_mult: float = 1.5,
        max_loss_dollars: float = 100.0,
    ):
        self.min_day_range_adr = min_day_range_adr
        self.min_day_range_pct = min_day_range_pct
        self.min_day_range_pct_adr = min_day_range_pct_adr
        self.max_yard_pct = max_yard_pct
        self.touch_band_adr = touch_band_adr
        self.min_depth_adr = min_depth_adr
        self.recovery_adr = recovery_adr
        self.max_bars_since_touch = max_bars_since_touch
        self.max_break_adr = max_break_adr
        self.rearm_adr = rearm_adr
        self.stop_adr = stop_adr
        self.earliest_touch = earliest_touch
        self.require_deeper_touch = require_deeper_touch
        self.exit_depth_mult = exit_depth_mult
        self.max_loss_dollars = max_loss_dollars

        # `_touch_bar` is the most recent bar to reach the band -- what the
        # staleness window counts from. `_pullback_bar` is the one that got
        # *furthest* into the pullback, which is what "the touch" means when
        # reporting one. The stop no longer comes from it (it is AVWAP-
        # anchored now, see stop_adr), but the depth it reached is still the
        # honest description of the pullback. Measurements are kept rather
        # than recomputed at fire time, since by then AVWAP has moved and the
        # trigger bar's own wick is elsewhere.
        self._touch_bar: BarEvent | None = None
        self._pullback_bar: BarEvent | None = None
        self._bars_since_touch = 0
        self._touch_adr: float | None = None
        self._touch_depth_adr: float | None = None
        # Set when a close lands too far the wrong side of AVWAP: this is a
        # touch setup, not an AVWAP-breakdown setup, so this interaction with
        # AVWAP is done. A fresh extreme starts a new leg; with `rearm_adr`
        # set, so does price leaving AVWAP again the trade's way -- the next
        # interaction is judged on its own, not on the one that broke.
        self._dead = False
        self._prev_extreme: float | None = None
        # Whether the previous evaluated bar already closed beyond the
        # recovery threshold -- the transition rule above.
        self._prev_close: float | None = None
        # The pullback that last spoke -- how deep it got, and which bar it
        # was. A leg that keeps nudging AVWAP at the same depth is one
        # setup, not six: ALGM 2026-10-02 fired twelve AVT-L triggers and
        # WOLF four in seven minutes, all off pullbacks no deeper than the
        # one before. Requiring the next pullback to go further leaves the
        # first entry and drops the echoes. Cleared when a new extreme or a
        # dead leg starts the count over -- deliberately NOT cleared when a
        # trigger fires, since that is the case it exists to gate.
        self._spoken_depth: float | None = None
        self._spoken_pullback: datetime | None = None
        # One AVWAP-touch alert per pullback, not per in-band bar. That is
        # `first_of_this_pullback` at the touch site -- `_touch_bar` is None
        # between pullbacks and set within one, so it latches on its own. An
        # extra per-leg latch used to sit here too and was strictly harmful:
        # it never cleared when a trigger fired, so the second pullback of a
        # leg went silent. On 2026-09-21 that cost 9 of 20 triggers their
        # heads-up -- the exact thing this alert exists to prevent.
        self._pending_alert: TouchAlert | None = None

    # -- direction hooks -----------------------------------------------------

    def _extreme(self, indicators: SessionIndicators) -> float | None:
        """The end of the move being joined: the day's high for a long (the
        move was up), the day's low for a short."""
        raise NotImplementedError

    def _pullback_wick(self, bar: BarEvent) -> float:
        """The end of the bar that reaches back toward AVWAP -- the low of a
        pullback in an up move, the high of one in a down move."""
        raise NotImplementedError

    def _beyond(self, price: float, avwap: float) -> float:
        """How far `price` sits the trade's way of AVWAP. Negative means the
        wrong side."""
        raise NotImplementedError

    def _exit_from_touch(self, touch: float, extreme: float) -> float:
        """A measured move: the pullback's own depth, times
        exit_depth_mult, projected from the touch.

        One expression for both directions -- for a short the extreme sits
        below the touch, so the same arithmetic walks the target down
        instead of up.
        """
        return touch + self.exit_depth_mult * (extreme - touch)

    def _may_speak(self, depth: float) -> bool:
        """Whether this pullback has earned an alert or a trigger.

        The pullback that already spoke may speak again -- that is the alert
        followed by its own trigger, one setup reporting twice. A *different*
        pullback has to have gone deeper than it.
        """
        if not self.require_deeper_touch or self._spoken_depth is None:
            return True
        if self._pullback_bar is not None and self._pullback_bar.start == self._spoken_pullback:
            return True
        return depth > self._spoken_depth

    def _mark_spoken(self, depth: float) -> None:
        self._spoken_depth = depth
        self._spoken_pullback = (
            self._pullback_bar.start if self._pullback_bar is not None else None
        )

    def _target_from_touch(self, touch: float, avwap: float, yard: float) -> float:
        """The price the trigger would fire at: recovery_adr of today's
        range past the pullback, the trade's way.

        Measured from the touch rather than from AVWAP, because a fixed
        distance past AVWAP is the wrong thing to ask of a pullback that
        only grazed the band -- it was already most of the way there, and
        waiting for the full distance gave up the entry. When the touch
        overshot *through* AVWAP, AVWAP is the anchor instead: price has to
        reclaim the line before the distance past it counts for anything.
        So the anchor is whichever of the two sits further the trade's way.

        AVWAP drifts bar to bar, so like every other level here this is
        where it would fire if AVWAP stood still.
        """
        raise NotImplementedError

    def _stop_from_avwap(self, avwap: float, yard: float) -> float:
        """Where the stop sits: `stop_adr` of today's range the far side of
        AVWAP. Anchored to AVWAP rather than to the pullback's low, because
        the trade's thesis is that AVWAP held -- if price is back through it
        by this much, the thesis is wrong, whatever the pullback's own low
        happened to be. It is also exactly where max_break_adr calls the leg
        dead, so the stop and the invalidation agree by construction: the
        bar that stops the trade out is the bar that ends the leg, and a leg
        cannot stop you out and then go on to offer a second entry.
        """
        raise NotImplementedError

    # -- the state machine ---------------------------------------------------

    def on_bar(
        self,
        bar: BarEvent,
        levels: DailyLevels,
        indicators: SessionIndicators | None = None,
        curve: VolumeCurve | None = None,
        trace: list[dict] | None = None,
    ) -> Trigger | None:
        adr = levels.adr
        if indicators is None or adr <= 0:
            return None
        avwap = indicators.avwap
        extreme = self._extreme(indicators)
        if avwap is None or extreme is None:
            return None
        if indicators.session_high is None or indicators.session_low is None:
            return None

        # Two yardsticks, deliberately. The day-range filter asks "has this
        # session moved enough to be worth joining", which only means
        # anything against the prior-session average -- ADR(14). Everything
        # describing the pullback's shape (touch, depth, recovery, the
        # wrong-side break, the stop) is a fraction of *this* day's move, so
        # it scales with the range the day has actually made: ADR(1). On
        # 2026-09-21 META ran past 2x ADR(14), and measuring its pullback
        # against the smaller yardstick understated every part of it.
        day_range = indicators.session_high - indicators.session_low
        if day_range <= 0:
            return None
        # ...and the yardstick is capped. Past a certain width the pullback
        # geometry stops scaling with the day: on a name that has already
        # run 25%, a tenth of that range is a vast distance to ask price to
        # travel before the entry, and a stop that far beyond AVWAP is no
        # longer a tight stop. Capping at max_yard_pct of price is what the
        # trader described as switching to fixed percents above 10% -- the
        # two are the same rule, since 0.05 of a 10% range *is* 0.5%, and
        # capping keeps it continuous at the boundary instead of a jump.
        yard = min(day_range, self.max_yard_pct / 100.0 * indicators.session_low)

        if self._touch_bar is not None:
            self._bars_since_touch += 1

        # A fresh extreme is a new move, so whatever happened to the last one
        # -- died, fired, or simply went stale -- stops applying.
        if self._prev_extreme is not None and extreme != self._prev_extreme:
            self._dead = False
            self._touch_bar = None
            self._pullback_bar = None
            self._bars_since_touch = 0
            self._spoken_depth = None
            self._spoken_pullback = None
        self._prev_extreme = extreme

        # Distance past AVWAP, still the measure the wrong-side break uses.
        recovery = self._beyond(bar.close, avwap) / yard
        prev_close, self._prev_close = self._prev_close, bar.close

        # Before the touch floor AVWAP is an average of a few bars and today's
        # range is a bar or two wide, so a "break" is noise -- and a leg dead
        # from 09:32 stays dead all day when the extreme was set at the open
        # (TMUS 2026-10-09). No touch can count before the floor anyway.
        after_floor = bar.start.time() >= self.earliest_touch
        if self._dead and self.rearm_adr is not None and recovery >= self.rearm_adr:
            # Back past AVWAP the trade's way, clear of it: whatever touches
            # AVWAP next is a new interaction. This bar is the departure, not
            # a touch, so evaluation starts with the next one.
            _trace(
                trace, bar,
                f"leg re-armed @ {bar.close:.2f}: closed {recovery:.3f} ADR(1) past AVWAP "
                f"{avwap:.2f} the trade's way (min {self.rearm_adr}) -- the next touch is "
                f"a new interaction",
            )
            self._dead = False
            return None
        if after_floor and -recovery > self.max_break_adr:
            if not self._dead:
                _trace(
                    trace, bar,
                    f"leg dead @ {bar.close:.2f}: closed {-recovery:.3f} ADR(1) the wrong "
                    f"side of AVWAP {avwap:.2f} (max {self.max_break_adr}) -- this is a "
                    f"touch setup, not a breakdown; waiting for a new extreme"
                    + (
                        f" or a close {self.rearm_adr} past AVWAP the trade's way"
                        if self.rearm_adr is not None else ""
                    ),
                )
            self._dead = True
            self._touch_bar = None
            self._pullback_bar = None
            self._spoken_depth = None
            self._spoken_pullback = None
            return None
        if self._dead:
            return None

        wick = self._pullback_wick(bar)
        wick_adr = (wick - avwap) / yard
        wick_depth_adr = abs(extreme - wick) / yard
        # The session itself has to be going the trade's way: this is a
        # continuation setup, so a long wants the day up on its open and a
        # short wants it down. Measured at the touch, which is the moment the
        # pullback is being judged as one worth joining.
        from_open_adr = (
            self._beyond(bar.close, indicators.session_open) / yard
            if indicators.session_open
            else 0.0
        )
        in_band = abs(wick_adr) <= self.touch_band_adr and wick_depth_adr >= self.min_depth_adr
        with_session = from_open_adr > 0
        # AVWAP sits on top of price for the first few minutes of a session,
        # so everything "touches" it before the day has shape. How few is a
        # judgement that has moved: 09:40 at first, then 09:37, now 09:34,
        # because a gapper arrives with its move already made -- TER
        # 2026-10-02 qualified at 09:34 on 0.91x ADR of which 0.63x was the
        # overnight gap, and waiting on an intraday range it had no need to
        # build just meant missing it.
        # What counts as "the move" for deciding the day is worth joining.
        # An overnight gap is part of the move: a name that gapped 0.4x ADR
        # and has added 0.4x since the open has travelled 0.8x from where it
        # last closed, even though its intraday range is only half that.
        # Measured to the extreme in the trade's own direction, so a gap up
        # followed by a slide down does not count as an up move.
        qualifying_size = max(day_range, abs(extreme - levels.prev_close))
        day_range_adr = qualifying_size / adr  # the one ADR(14) measure that stays
        # What the gap contributed: how far the figure exceeds today's own
        # high-low. In the clean case -- gapped one way, continued the same
        # way -- this is the gap itself; when the day traded back through
        # its open it is less, which is the honest number either way.
        gap_adr = (qualifying_size - day_range) / adr if adr > 0 else 0.0
        # (high/low - 1), the same arithmetic ADR% itself is built from, so
        # the two percents are directly comparable.
        day_range_pct = _pct_of_level(day_range, indicators.session_low)
        rvol = compute_rvol(indicators.cum_volume, bar.start, curve) if curve else None
        min_rvol = rvol_threshold(levels.adv)
        if in_band and with_session and after_floor:
            first_of_this_pullback = self._touch_bar is None
            self._touch_bar = bar
            self._bars_since_touch = 0
            # Deepest wick of this pullback wins -- see _pullback_bar.
            if self._pullback_bar is None or wick_depth_adr > (self._touch_depth_adr or 0.0):
                self._pullback_bar = bar
                self._touch_adr = wick_adr
                self._touch_depth_adr = wick_depth_adr
            # The heads-up. AVT's premise is that price leaves AVWAP fast, so
            # waiting for the recovery bar to print can be too late to act on
            # -- this says "it is at AVWAP now" while the trade is still
            # there to take. Gated on the same day-quality filters the
            # trigger uses, since a touch on a day not worth joining is not
            # worth interrupting anyone for. One per pullback.
            if (
                first_of_this_pullback
                and self._may_speak(wick_depth_adr)
                and _leg_is_big_enough(
                    qualifying_size, adr, indicators.session_low,
                    self.min_day_range_adr, self.min_day_range_pct,
                    self.min_day_range_pct_adr,
                )
                and rvol is not None
                and rvol >= min_rvol
            ):
                stop = self._stop_from_avwap(avwap, yard)
                # Sized at the price the trigger would fire, not at the
                # touch: the touch is the warning, the recovery line is
                # where the trade actually goes on.
                target = self._target_from_touch(wick, avwap, yard)
                shares, dollars = _position_size(target, stop, self.max_loss_dollars)
                exit_target = self._exit_from_touch(wick, extreme)
                self._mark_spoken(wick_depth_adr)
                self._pending_alert = TouchAlert(
                    symbol=bar.symbol,
                    direction=self.direction,
                    bar_start=bar.start,
                    bar_end=bar.end,
                    price=bar.close,
                    avwap=round(avwap, 4),
                    touch_adr=round(wick_adr, 4),
                    depth_adr=round(wick_depth_adr, 4),
                    day_range_adr=round(day_range_adr, 4),
                    day_range_pct=day_range_pct,
                    from_open_adr=round(from_open_adr, 4),
                    rvol=rvol,
                    target=round(target, 4),
                    target_basis="recovery",
                    exit_target=round(exit_target, 4),
                    rr=_reward_to_risk(target, stop, exit_target),
                    stop=round(stop, 4),
                    shares=shares,
                    dollars=dollars,
                    max_loss_dollars=self.max_loss_dollars,
                    session_high=indicators.session_high,
                    session_low=indicators.session_low,
                )
        elif in_band:
            # Would have been a touch on distance alone -- say why it wasn't,
            # rather than leaving a silent gap where a trigger was expected.
            why = (
                f"before the {self.earliest_touch:%H:%M} floor"
                if not after_floor
                else f"session is not going the trade's way (from open {from_open_adr:+.3f} ADR)"
            )
            _trace(
                trace, bar,
                f"touch ignored @ {bar.close:.2f}: reached the band "
                f"({wick_adr:+.3f}) but {why}",
            )

        readout = _AVTReadout(
            day_range_adr=day_range_adr,
            min_day_range_adr=self.min_day_range_adr,
            day_range_pct=day_range_pct,
            gap_adr=gap_adr if gap_adr > 0.005 else None,
            rvol=rvol,
            min_rvol=min_rvol,
            # The qualifying touch's own measurements, not this bar's -- see
            # _touch_adr in __init__. Falls back to this bar's while nothing
            # has touched yet, so a near-miss still reads honestly.
            touch_adr=self._touch_adr if self._touch_bar is not None else wick_adr,
            touch_band_adr=self.touch_band_adr,
            depth_adr=self._touch_depth_adr if self._touch_bar is not None else wick_depth_adr,
            min_depth_adr=self.min_depth_adr,
            from_open_adr=from_open_adr,
            touch_at=(
                f"{self._pullback_bar.start:%H:%M}" if self._pullback_bar is not None else None
            ),
            recovery_adr=recovery,
            target=(
                self._target_from_touch(self._pullback_wick(self._pullback_bar), avwap, yard)
                if self._pullback_bar is not None
                else None
            ),
            rr=(
                _reward_to_risk(
                    self._target_from_touch(
                        self._pullback_wick(self._pullback_bar), avwap, yard
                    ),
                    self._stop_from_avwap(avwap, yard),
                    self._exit_from_touch(self._pullback_wick(self._pullback_bar), extreme),
                )
                if self._pullback_bar is not None
                else None
            ),
            min_day_range_pct=self.min_day_range_pct,
            min_day_range_pct_adr=self.min_day_range_pct_adr,
            bars_since_touch=None if self._touch_bar is None else self._bars_since_touch,
            max_bars_since_touch=self.max_bars_since_touch,
            already_past_target=(
                prev_close is not None
                and self._pullback_bar is not None
                and self._beyond(
                    prev_close,
                    self._target_from_touch(
                        self._pullback_wick(self._pullback_bar), avwap, yard
                    ),
                ) >= 0
            ),
        )

        def candidate(verdict: str) -> None:
            _trace(trace, bar, f"candidate @ {bar.close:.2f}: {readout.line()} -- {verdict}")

        if self._touch_bar is None:
            # Nothing has reached the band since the last extreme. Mostly
            # silent -- tracing all 390 bars of a session would bury the
            # decisions -- but two cases are worth a line.
            if recovery >= self.recovery_adr and prev_close is not None and (
                self._beyond(prev_close, avwap) / yard < self.recovery_adr
            ):
                # Price crossed into the recovery zone anyway: the shape that
                # looks like a trigger on a chart but has no pullback behind
                # it.
                candidate("FAIL touch: nothing reached the AVWAP band since the last extreme")
            elif trace is not None and abs(wick_adr) <= _NEAR_TOUCH * self.touch_band_adr:
                # A near miss. Without this, "missed the band by a hair" and
                # "never came close" look identical from the outside, since
                # both are simply absent. Deliberately NOT gated on the day
                # qualifying: when the range is what is actually holding
                # things up, that is the answer being looked for, so the
                # line carries it rather than suppressing itself. Real case:
                # TER 2026-10-02 09:57, wick 0.069 against a 0.05 band on a
                # day only 0.71x ADR wide -- two reasons, no trace of either.
                _trace(
                    trace, bar,
                    f"near touch @ {bar.close:.2f}: wick {wick_adr:+.3f} vs "
                    f"±{self.touch_band_adr:g}, range {day_range_adr:.2f}x/"
                    f"{self.min_day_range_adr:g}x ADR"
                    + (f" incl. {gap_adr:.2f}x gap" if gap_adr > 0.005 else "")
                    + " -- closest this pullback came, not a touch",
                )
            return None
        if self._bars_since_touch > self.max_bars_since_touch:
            candidate("FAIL bars since touch: the pullback is stale, waiting for a new one")
            self._touch_bar = None
            self._pullback_bar = None
            return None
        touch_extreme = self._pullback_wick(self._pullback_bar)
        target = self._target_from_touch(touch_extreme, avwap, yard)
        if self._beyond(bar.close, target) < 0:
            candidate("HOLD recovery: has not yet reached the target -- stay armed")
            return None
        # The crossing rule: price has to arrive at the target on this bar,
        # not already have been sitting past it.
        if prev_close is not None and self._beyond(prev_close, target) >= 0:
            candidate(
                "FAIL already recovered: price was past the target before this bar, "
                "so nothing continued here"
            )
            return None
        if not _leg_is_big_enough(
            qualifying_size, adr, indicators.session_low,
            self.min_day_range_adr, self.min_day_range_pct, self.min_day_range_pct_adr,
        ):
            candidate("FAIL range: the day has not moved enough to be worth joining")
            return None
        if rvol is None or rvol < min_rvol:
            candidate("FAIL rvol: not enough volume behind the day")
            return None
        if not self._may_speak(self._touch_depth_adr or 0.0):
            candidate(
                f"FAIL depth: this pullback reached "
                f"{self._touch_depth_adr or 0.0:.3f}, no deeper than the "
                f"{self._spoken_depth:.3f} already acted on -- same setup, not a new one"
            )
            return None

        stop = self._stop_from_avwap(avwap, yard)
        shares, dollars = _position_size(bar.close, stop, self.max_loss_dollars)
        candidate("FIRE -- all filters pass")
        self._mark_spoken(self._touch_depth_adr or 0.0)

        touch_at = self._pullback_bar.start
        self._touch_bar = self._pullback_bar = None  # one trigger per pullback
        avwap_pct = _pct_of_level(bar.close - avwap, avwap)
        return Trigger(
            symbol=bar.symbol,
            setup=self.name,
            direction=self.direction,
            trigger_price=bar.close,
            bar_start=bar.start,
            bar_end=bar.end,
            level_name="AVW",
            level_value=round(stop, 4),
            extras={
                "avwap": avwap,
                "avwap_pct": avwap_pct,
                "adr_pct": levels.adr_pct,
                "rvol": rvol,
                "rvol_floor": min_rvol,
                "day_range_adr": round(day_range_adr, 4),
                "day_range_pct": day_range_pct,
                "touch_adr": round(self._touch_adr, 4) if self._touch_adr is not None else None,
                "touch_at": touch_at.isoformat(),
                "touch_price": touch_extreme,
                "depth_adr": (
                    round(self._touch_depth_adr, 4)
                    if self._touch_depth_adr is not None
                    else None
                ),
                "recovery_adr": round(recovery, 4),
                "from_open_adr": round(from_open_adr, 4),
                "exit_target": round(self._exit_from_touch(touch_extreme, extreme), 4),
                "rr": _reward_to_risk(
                    bar.close, stop, self._exit_from_touch(touch_extreme, extreme)
                ),
                "bars_since_touch": self._bars_since_touch,
                "session_open": indicators.session_open,
                "session_high": indicators.session_high,
                "session_low": indicators.session_low,
                "max_loss_dollars": self.max_loss_dollars,
                # Aliased under the names Notify's trigger line already reads,
                # so the console alert needs no AVT-specific code for the
                # stop/size framing (astor.drivers.live._format_trigger_line).
                "reclaim_pct": _abs_pct_of_level(bar.close - stop, stop),
                "level_stop_pct": _abs_pct_of_level(bar.close - stop, stop),
                "level_stop_shares": shares,
                "level_stop_dollars": dollars,
            },
        )


    def pop_alert(self) -> TouchAlert | None:
        alert, self._pending_alert = self._pending_alert, None
        return alert


class AVWAPTouchLong(_AVWAPTouchBase):
    """Long. The move up is the day's high; the pullback comes down to AVWAP
    and the entry is the push back up off it."""

    name = "AVT-L"
    direction = "long"

    def _extreme(self, indicators: SessionIndicators) -> float | None:
        return indicators.session_high

    def _pullback_wick(self, bar: BarEvent) -> float:
        return bar.low

    def _beyond(self, price: float, avwap: float) -> float:
        return price - avwap

    def _target_from_touch(self, touch: float, avwap: float, yard: float) -> float:
        return max(touch, avwap) + self.recovery_adr * yard

    def _stop_from_avwap(self, avwap: float, yard: float) -> float:
        return avwap - self.stop_adr * yard


class AVWAPTouchShort(_AVWAPTouchBase):
    """Short. Mirror of AVWAPTouchLong: the move down is the day's low, the
    pullback comes up to AVWAP, the entry is the drop back off it."""

    name = "AVT-S"
    direction = "short"

    def _extreme(self, indicators: SessionIndicators) -> float | None:
        return indicators.session_low

    def _pullback_wick(self, bar: BarEvent) -> float:
        return bar.high

    def _beyond(self, price: float, avwap: float) -> float:
        return avwap - price

    def _target_from_touch(self, touch: float, avwap: float, yard: float) -> float:
        return min(touch, avwap) - self.recovery_adr * yard

    def _stop_from_avwap(self, avwap: float, yard: float) -> float:
        return avwap + self.stop_adr * yard


ALL_SETUPS: list[type[Setup]] = [
    ReversalLong,
    ReversalShort,
    AVWAPTouchLong,
    AVWAPTouchShort,
]
SETUPS_BY_NAME = {s.name: s for s in ALL_SETUPS}
