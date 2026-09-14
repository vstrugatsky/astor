"""Setup state machines.

Reversal (REV-L/REV-S, "right side of a V") is the only setup left here.
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

from datetime import datetime
from typing import ClassVar

from astor.core.indicators import SessionIndicators, compute_rvol
from astor.core.types import BarEvent, CapitulationAlert, DailyLevels, Trigger, VolumeCurve


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
_REV_RVOL_TIERS = ((5_000_000_000, 1.5), (500_000_000, 1.75))
_REV_RVOL_DEFAULT = 2.0


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

    def pop_capitulation_alert(self) -> CapitulationAlert | None:
        """A leg-in-progress heads-up, distinct from a Trigger -- see
        _ReversalBase. No-op default so Scanner can call this on every
        setup uniformly without caring which ones implement it."""
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
        min_leg_adr: float = 1.0,
        min_retrace_pct: float = 10.0,
        max_retrace_pct: float = 50.0,
        min_confirm_minutes: float = 0.0,
        min_leg_minutes: float = 4.0,
        min_ema9_extension_adr: float = 0.05,
        accel_window: int = 3,
        max_loss_dollars: float = 100.0,
    ):
        self.min_leg_adr = min_leg_adr
        self.min_retrace_pct = min_retrace_pct
        self.max_retrace_pct = max_retrace_pct
        self.min_confirm_minutes = min_confirm_minutes
        self.min_leg_minutes = min_leg_minutes
        self.min_ema9_extension_adr = min_ema9_extension_adr
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
        # The last _extreme value a capitulation alert was already fired
        # for, so a leg that keeps deepening bar-by-bar doesn't re-alert
        # every single bar -- only once per distinct (deeper) extreme.
        self._capitulation_alerted_extreme: float | None = None
        self._pending_capitulation_alert: CapitulationAlert | None = None

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

    # -- shared state machine ------------------------------------------------

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

        if anchor_moved or (extreme_moved and leg_was_dead):
            self._leg_bars = [bar]
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
        # else: the leg is dead -- inert, nothing worth adding.

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
            return None

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

        if retrace_pct > self.max_retrace_pct:
            self._retrace_dead = True
            _trace(
                trace, bar,
                f"retrace {retrace_pct:.1f}% > max {self.max_retrace_pct}% -- "
                f"leg dead until a new, deeper extreme",
            )
            return None

        ema9_now = indicators.ema9
        reclaimed = ema9_now is not None and self._ema9_reclaimed_now(bar.close, ema9_now)
        if retrace_pct < self.min_retrace_pct and not reclaimed:
            _trace(
                trace, bar,
                f"candidate @ {bar.close:.2f}: retrace {retrace_pct:.1f}% < min "
                f"{self.min_retrace_pct}% and EMA9 ({ema9_now}) not reclaimed -- stay armed",
            )
            return None

        extreme_at = self._extreme_at(indicators)
        elapsed_minutes = (bar.start - extreme_at).total_seconds() / 60.0
        if elapsed_minutes < self.min_confirm_minutes:
            _trace(
                trace, bar,
                f"candidate @ {bar.close:.2f}: retrace {retrace_pct:.1f}% in band, "
                f"elapsed {elapsed_minutes:.0f}m < min {self.min_confirm_minutes}m -- too early",
            )
            return None

        anchor_at = self._anchor_at(indicators)
        leg_duration_minutes = abs((extreme_at - anchor_at).total_seconds()) / 60.0
        if leg_duration_minutes < self.min_leg_minutes:
            _trace(
                trace, bar,
                f"candidate @ {bar.close:.2f}: retrace {retrace_pct:.1f}% elapsed "
                f"{elapsed_minutes:.0f}m OK, but leg built in {leg_duration_minutes:.1f}m "
                f"< min {self.min_leg_minutes}m -- too fast to be a real leg",
            )
            return None

        adr = levels.adr
        if adr <= 0 or leg_size < self.min_leg_adr * adr:
            _trace(
                trace, bar,
                f"candidate @ {bar.close:.2f}: retrace {retrace_pct:.1f}% elapsed "
                f"{elapsed_minutes:.0f}m built {leg_duration_minutes:.1f}m OK, but leg "
                f"{leg_size:.2f} < required "
                f"{self.min_leg_adr * adr if adr > 0 else float('nan'):.2f} "
                f"({self.min_leg_adr}x ADR ${adr:.2f}) -- leg too small",
            )
            return None  # too small a move to be "sharp"

        move_rvol = (
            compute_rvol(self._extreme_cum_volume_at(indicators), extreme_at, curve)
            if curve
            else None
        )
        if move_rvol is None or move_rvol < rev_rvol_threshold(levels.adv):
            _trace(
                trace, bar,
                f"candidate @ {bar.close:.2f}: retrace {retrace_pct:.1f}% elapsed "
                f"{elapsed_minutes:.0f}m leg {leg_size:.2f} OK, but move_rvol "
                f"{move_rvol} < required {rev_rvol_threshold(levels.adv)} -- too thin",
            )
            return None  # not enough volume behind the move

        ema9_at_extreme = self._extreme_ema9_at(indicators)
        extreme_close = self._extreme_close_at(indicators)
        ema9_extension_adr = (
            round(self._ema9_extension(extreme_close, ema9_at_extreme) / adr, 4)
            if ema9_at_extreme is not None and extreme_close is not None
            else None
        )
        if self.min_ema9_extension_adr > 0 and (
            ema9_extension_adr is None or ema9_extension_adr < self.min_ema9_extension_adr
        ):
            _trace(
                trace, bar,
                f"candidate @ {bar.close:.2f}: retrace {retrace_pct:.1f}% elapsed "
                f"{elapsed_minutes:.0f}m leg {leg_size:.2f} move_rvol {move_rvol} OK, but "
                f"ema9 extension {ema9_extension_adr} < min {self.min_ema9_extension_adr} "
                f"ADR -- not extended enough",
            )
            return None

        # _leg_bars keeps accumulating past the extreme (a retrace-candidate
        # bar can't be told apart from a mid-push pause in real time, so both
        # get appended -- see the accumulation comment above); filtering by
        # the already-known extreme_at here cleanly excludes any retrace
        # bars regardless of how many came before this one fired, leaving
        # exactly the push itself.
        push_bars = [b for b in self._leg_bars if b.start <= extreme_at]
        recent = push_bars[-self.accel_window :]
        earlier = push_bars[: -self.accel_window]

        # accel_price: net close-to-close displacement per bar, recent vs.
        # earlier -- not average bar range. Real case that motivated this:
        # DELL 2026-09-02's REV-L leg opened with several equally-wide,
        # choppy bars (the initial sharp drop), which made the leg's
        # *average bar range* a poor baseline -- the last 3 bars' genuine
        # ~$9 net move looked unremarkable (0.96) next to that already-wide
        # opening, even though net progress had clearly sped up. Rate-based
        # and close-to-close (matching the EMA9/retrace convention) cancels
        # out wick noise and rewards actual directional speed: needs at
        # least 2 earlier bars to form even one earlier "step"; below that,
        # same neutral fallback as too-few-bars ever was.
        if len(earlier) >= 2:
            recent_move = abs(recent[-1].close - earlier[-1].close)
            earlier_move = abs(earlier[-1].close - earlier[0].close)
            earlier_rate = earlier_move / (len(earlier) - 1)
            recent_rate = recent_move / self.accel_window
            accel_price = round(recent_rate / earlier_rate, 4) if earlier_rate > 0 else None
        else:
            accel_price = 1.0

        # accel_vol has no directional analog (volume doesn't net out), so
        # it stays an average-per-bar comparison, same "not enough bars"
        # fallback as before.
        earlier_for_vol = earlier or push_bars
        recent_vol, earlier_vol = _mean([b.volume for b in recent]), _mean(
            [b.volume for b in earlier_for_vol]
        )
        accel_vol = (
            round(recent_vol / earlier_vol, 4)
            if recent_vol is not None and earlier_vol
            else None
        )

        legacy_condition = self._legacy_condition(levels, self._extreme, bar.close)

        _trace(
            trace, bar,
            f"FIRE @ {bar.close:.2f}: retrace {retrace_pct:.1f}%"
            f"{' (via EMA9 reclaim)' if reclaimed and retrace_pct < self.min_retrace_pct else ''} "
            f"elapsed {elapsed_minutes:.0f}m leg {leg_size:.2f} (built over "
            f"{leg_duration_minutes:.0f}m) move_rvol {move_rvol} ema9_ext {ema9_extension_adr}",
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
                "leg_duration_minutes": round(leg_duration_minutes, 1),
                "retrace_pct": round(retrace_pct, 4),
                "retrace_minutes": round(elapsed_minutes, 1),
                "retrace_via": "ema9_reclaim" if reclaimed and retrace_pct < self.min_retrace_pct else "pct",
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
        """
        assert self._anchor is not None and self._extreme is not None
        if self._extreme == self._capitulation_alerted_extreme:
            return

        leg_size = abs(self._anchor - self._extreme)
        adr = levels.adr
        if leg_size <= 0 or adr <= 0 or leg_size < self.min_leg_adr * adr:
            return

        extreme_at = self._extreme_at(indicators)
        anchor_at = self._anchor_at(indicators)
        leg_duration_minutes = abs((extreme_at - anchor_at).total_seconds()) / 60.0
        if leg_duration_minutes < self.min_leg_minutes:
            return

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

        self._capitulation_alerted_extreme = self._extreme
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
        )

    def pop_capitulation_alert(self) -> CapitulationAlert | None:
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

    def _legacy_condition(self, levels: DailyLevels, extreme: float, close: float) -> bool:
        return extreme > levels.pdh and close < levels.pdh


ALL_SETUPS: list[type[Setup]] = [ReversalLong, ReversalShort]
SETUPS_BY_NAME = {s.name: s for s in ALL_SETUPS}
