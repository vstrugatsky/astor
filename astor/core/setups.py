"""Setup state machines.

The shape of each setup is fixed; the thresholds are not. Both filters are
expressed in ADR units rather than dollars or percent, so one number means the
same thing on MARA ($0.79 ADR) as on AMD ($28.69 ADR) -- which is what makes a
single watchlist-wide setting sensible.

Each setup applies two independent tests at trigger time:

  excursion  how far past the level price actually went. Rejects the bar-scale
             chop that dominated the first replay -- 2 bars, a cent beyond.
             Passes on EITHER an ADR-scaled or a flat-percent basis -- see
             below for why one measure alone under-serves high-ADR names.
  reclaim    how convincingly price came back. Rejects a close that limps a
             cent past the level and means nothing. ADR-scaled only.

Excursion started ADR-only, like reclaim. It under-caught high-ADR names: AEVA
(11% ADR) undercut PDL by $0.20 and rallied cleanly, a textbook U&R, but $0.20
was only 0.081x its $2.47 ADR against a 0.1x threshold -- missed by five cents.
Checking watchlist-wide, excursion size in ADR units correlated negatively with
ADR itself: the more volatile a name, the smaller a fraction of its ADR its
undercuts tend to run. A single ADR coefficient therefore filters volatile
names hardest, backwards from the intent. A flat percent-of-level floor doesn't
have that problem, so excursion now passes if EITHER measure clears its bar --
whichever is more lenient for that name on that day.

Reclaim keeps the ADR-only design: a percent-of-level floor doesn't obviously
fit it the same way, and AMD's false O&F (a PDH reclaim misread as a fail, not
a chop problem) wasn't a threshold failure to begin with.

Both ADR-unit values are recorded, plus excursion_pct. The dollar figures they
derive from are deliberately not stored: they are not comparable across a
watchlist, and the absolute prices that do matter -- the level and the trigger
-- are already columns on the trigger row.

A bar that breaches the level always arms the setup. The filters are applied
when it would fire, against the *accumulated* excursion, so an undercut that
deepens over several bars is judged on its full extent rather than its first
bar. Failing a filter leaves the setup armed rather than resetting it: the
breach is still real, we are simply still waiting for a reclaim worth reporting.

If ADR is unavailable (adr <= 0), any ADR-scaled check that is configured drops
out of its OR rather than auto-passing or auto-failing: excursion falls back to
percent-only if that is also configured, and reclaim (ADR-only, no fallback)
skips filtering for that symbol, as before. The CLI reports which symbols lack
ADR so this is visible rather than inferred.
"""

from __future__ import annotations

from typing import ClassVar

from astor.core.types import BarEvent, DailyLevels, Trigger


def _pct_of_level(amount: float, level_value: float) -> float | None:
    """amount expressed as a percent of the level price, e.g. excursion or
    reclaim distance against PDL/PDH. Always computed where possible -- this is
    a diagnostic recorded regardless of whether anything filters on it."""
    return round(amount / level_value * 100.0, 4) if level_value > 0 else None


def _excursion_passes(
    excursion: float,
    level_value: float,
    adr: float,
    min_excursion_adr: float,
    min_excursion_pct: float,
) -> tuple[bool, float | None]:
    """Whether an excursion clears its ADR-scaled OR flat-percent floor.

    Each configured, applicable check contributes to an OR: passing either is
    enough. A configured check that is inapplicable (ADR unscalable) is left
    out of the OR rather than counted as a pass or a fail -- so with both
    checks configured, an unscalable ADR degrades to percent-only rather than
    silently disabling the whole filter. Configuring neither disables the
    filter entirely, matching earlier ADR-only behaviour when only
    min_excursion_adr is set.
    """
    excursion_pct = _pct_of_level(excursion, level_value)

    if min_excursion_adr <= 0 and min_excursion_pct <= 0:
        return True, excursion_pct

    checks = []
    if min_excursion_adr > 0 and adr > 0:
        checks.append(excursion >= min_excursion_adr * adr)
    if min_excursion_pct > 0 and excursion_pct is not None:
        checks.append(excursion_pct >= min_excursion_pct)

    passes = True if not checks else any(checks)
    return passes, excursion_pct


class Setup:
    """One setup tracked for one symbol. Instances are stateful."""

    name: ClassVar[str]
    direction: ClassVar[str]

    def on_bar(self, bar: BarEvent, levels: DailyLevels) -> Trigger | None:
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
        min_excursion_adr: float = 0.0,
        min_excursion_pct: float = 0.0,
        min_reclaim_adr: float = 0.0,
    ):
        self.allow_same_bar_reclaim = allow_same_bar_reclaim
        self.min_excursion_adr = min_excursion_adr
        self.min_excursion_pct = min_excursion_pct
        self.min_reclaim_adr = min_reclaim_adr
        self._undercut_low: float | None = None
        self._undercut_start = None
        self._bars_below = 0  # bars whose low actually broke the level
        self._bars_armed = 0  # bars since the breach, however price behaved

    def on_bar(self, bar: BarEvent, levels: DailyLevels) -> Trigger | None:
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

        excursion_ok, excursion_pct = _excursion_passes(
            excursion, pdl, adr, self.min_excursion_adr, self.min_excursion_pct
        )
        if not excursion_ok:
            return None  # stay armed: too shallow by every applicable measure
        reclaim_pct = _pct_of_level(reclaim, pdl)
        if adr > 0 and reclaim < self.min_reclaim_adr * adr:
            return None  # stay armed: wait for a more convincing reclaim

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
        min_excursion_adr: float = 0.0,
        min_excursion_pct: float = 0.0,
        min_reclaim_adr: float = 0.0,
    ):
        self.allow_same_bar_fail = allow_same_bar_fail
        self.min_excursion_adr = min_excursion_adr
        self.min_excursion_pct = min_excursion_pct
        self.min_reclaim_adr = min_reclaim_adr
        self._overshoot_high: float | None = None
        self._overshoot_start = None
        self._bars_above = 0  # bars whose high actually broke the level
        self._bars_armed = 0  # bars since the breach, however price behaved

    def on_bar(self, bar: BarEvent, levels: DailyLevels) -> Trigger | None:
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

        excursion_ok, excursion_pct = _excursion_passes(
            excursion, pdh, adr, self.min_excursion_adr, self.min_excursion_pct
        )
        if not excursion_ok:
            return None
        reclaim_pct = _pct_of_level(reclaim, pdh)
        if adr > 0 and reclaim < self.min_reclaim_adr * adr:
            return None

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
            },
        )
        self._overshoot_high = None
        self._overshoot_start = None
        self._bars_above = 0
        self._bars_armed = 0
        return trigger


ALL_SETUPS: list[type[Setup]] = [UndercutRally, OvershootFail]
SETUPS_BY_NAME = {s.name: s for s in ALL_SETUPS}
