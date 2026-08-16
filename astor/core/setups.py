"""Setup state machines.

The rules are intentionally loose -- the trader tightens them in response to
recorded triggers rather than guessing thresholds up front. So these encode only
the shape of each setup and record enough context (`extras`) to tighten it later.

The one judgement call that could not be left implicit is whether a single bar
that dips below the level and closes back above it counts. That is a real U&R in
miniature and also most of the noise, so it is an explicit flag defaulting to
"no", and `bars_below` is recorded either way so the distribution can settle it.
"""

from __future__ import annotations

from typing import ClassVar

from astor.core.types import BarEvent, DailyLevels, Trigger


class Setup:
    """One setup tracked for one symbol. Instances are stateful."""

    name: ClassVar[str]
    direction: ClassVar[str]

    def on_bar(self, bar: BarEvent, levels: DailyLevels) -> Trigger | None:
        raise NotImplementedError


class UndercutRally(Setup):
    """Long. Price drops below the previous day's low, then reclaims it.

    ARMED --(bar.low < PDL)--> UNDERCUT --(bar.close > PDL)--> trigger, re-ARMED

    Returning to ARMED after firing means a second undercut-and-reclaim on the
    same day fires again. That is the honest reading of the setup, and the
    trigger count per ticker per day is itself a number worth seeing before
    deciding whether to add a cooldown.
    """

    name = "U&R"
    direction = "long"

    def __init__(self, allow_same_bar_reclaim: bool = False):
        self.allow_same_bar_reclaim = allow_same_bar_reclaim
        self._undercut_low: float | None = None
        self._undercut_start = None
        self._bars_below = 0

    def on_bar(self, bar: BarEvent, levels: DailyLevels) -> Trigger | None:
        pdl = levels.pdl

        if self._undercut_low is None:
            if bar.low < pdl:
                self._undercut_low = bar.low
                self._undercut_start = bar.start
                self._bars_below = 1
                if not (self.allow_same_bar_reclaim and bar.close > pdl):
                    return None
            else:
                return None
        else:
            self._undercut_low = min(self._undercut_low, bar.low)
            self._bars_below += 1
            if bar.close <= pdl:
                return None

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
                "depth_below_level": round(pdl - self._undercut_low, 4),
                "reclaim_close": bar.close,
            },
        )
        self._undercut_low = None
        self._undercut_start = None
        self._bars_below = 0
        return trigger


class OvershootFail(Setup):
    """Short. Price trades above the previous day's high, then loses it.

    ARMED --(bar.high > PDH)--> OVERSHOT --(bar.close < PDH)--> trigger, re-ARMED
    """

    name = "O&F"
    direction = "short"

    def __init__(self, allow_same_bar_fail: bool = False):
        self.allow_same_bar_fail = allow_same_bar_fail
        self._overshoot_high: float | None = None
        self._overshoot_start = None
        self._bars_above = 0

    def on_bar(self, bar: BarEvent, levels: DailyLevels) -> Trigger | None:
        pdh = levels.pdh

        if self._overshoot_high is None:
            if bar.high > pdh:
                self._overshoot_high = bar.high
                self._overshoot_start = bar.start
                self._bars_above = 1
                if not (self.allow_same_bar_fail and bar.close < pdh):
                    return None
            else:
                return None
        else:
            self._overshoot_high = max(self._overshoot_high, bar.high)
            self._bars_above += 1
            if bar.close >= pdh:
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
                "height_above_level": round(self._overshoot_high - pdh, 4),
                "fail_close": bar.close,
            },
        )
        self._overshoot_high = None
        self._overshoot_start = None
        self._bars_above = 0
        return trigger


ALL_SETUPS: list[type[Setup]] = [UndercutRally, OvershootFail]
SETUPS_BY_NAME = {s.name: s for s in ALL_SETUPS}
