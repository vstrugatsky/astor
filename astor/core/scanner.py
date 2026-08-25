"""The scanner core. Both Replay and Live drive this same object.

It consumes one BarEvent at a time and has no way to look ahead: it holds no
dataset, opens no connection, and never reads the wall clock. Whatever a driver
feeds it is the entirety of what it knows. That is the property that makes a
Replay result mean something about Live.

This is also the single choke point where zero-volume bars are dropped, so
neither driver can bypass the filter.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from astor.core.grading import grade_trigger
from astor.core.indicators import SessionIndicators
from astor.core.setups import ALL_SETUPS, Setup
from astor.core.types import BarEvent, DailyLevels, Trigger, VolumeCurve

logger = logging.getLogger(__name__)


class Scanner:
    def __init__(
        self,
        levels: dict[str, DailyLevels],
        setup_classes: list[type[Setup]] | None = None,
        setup_kwargs: dict | None = None,
    ):
        self.levels = levels
        self.setup_classes = setup_classes or ALL_SETUPS
        self.setup_kwargs = setup_kwargs or {}
        self._state: dict[tuple[str, str], Setup] = {}

        # Populated externally by the driver, same convention as `levels`.
        # curves is read by a future RVOL-consuming setup; nothing here uses
        # it directly yet.
        self.indicators: dict[str, SessionIndicators] = {}
        self.curves: dict[str, VolumeCurve] = {}

        self.triggers: list[Trigger] = []
        self.bars_seen = 0
        self.bars_evaluated = 0
        self.skipped_zero_volume = 0
        self.skipped_no_levels: set[str] = set()

    def _gate(self, bar: BarEvent) -> DailyLevels | None:
        """Bookkeeping shared by on_bar and backfill_bar: count the bar,
        drop zero-volume padding, and skip symbols with no reference levels
        (not on the watchlist). Returns the bar's levels, or None if the bar
        should go no further.
        """
        self.bars_seen += 1

        # Zero-volume bars are dropped before any setup or indicator sees
        # them. IBKR pads thin tickers with zero-volume bars that carry OHLC
        # forward, which would otherwise arm a setup -- or skew AVWAP -- on a
        # minute in which nothing traded. Dropping them also deletes exactly
        # the bars a live stream would never have emitted, which is what
        # keeps Replay and Live in agreement.
        if bar.volume <= 0:
            self.skipped_zero_volume += 1
            return None

        levels = self.levels.get(bar.symbol)
        if levels is None:
            self.skipped_no_levels.add(bar.symbol)
            return None
        return levels

    def on_bar(self, bar: BarEvent) -> list[Trigger]:
        """Feed one completed bar. Returns any triggers it produced."""
        levels = self._gate(bar)
        if levels is None:
            return []

        indicators = self.indicators.setdefault(bar.symbol, SessionIndicators(bar.symbol))
        indicators.update(bar)
        curve = self.curves.get(bar.symbol)

        self.bars_evaluated += 1
        fired: list[Trigger] = []
        for cls in self.setup_classes:
            key = (bar.symbol, cls.name)
            setup = self._state.get(key)
            if setup is None:
                setup = cls(**self.setup_kwargs.get(cls.name, {}))
                self._state[key] = setup
            trigger = setup.on_bar(bar, levels, indicators, curve)
            if trigger is not None:
                auto_grade = grade_trigger(trigger, levels, indicators)
                trigger = replace(
                    trigger, extras={**trigger.extras, "auto_grade": auto_grade}
                )
                fired.append(trigger)

        self.triggers.extend(fired)
        return fired

    def backfill_bar(self, bar: BarEvent) -> None:
        """Feed one bar that happened before this symbol joined the live
        scan (or before a Replay run's start-time window). Indicators (AVWAP,
        RVOL's running volume) stay continuous from the first bar of the day;
        Setup state does not -- a setup added mid-session starts clean, as if
        the bars before it never happened, so it is never evaluated here.
        """
        levels = self._gate(bar)
        if levels is None:
            return
        self.indicators.setdefault(bar.symbol, SessionIndicators(bar.symbol)).update(bar)
