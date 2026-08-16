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

from astor.core.setups import ALL_SETUPS, Setup
from astor.core.types import BarEvent, DailyLevels, Trigger

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

        self.triggers: list[Trigger] = []
        self.bars_seen = 0
        self.bars_evaluated = 0
        self.skipped_zero_volume = 0
        self.skipped_no_levels: set[str] = set()

    def on_bar(self, bar: BarEvent) -> list[Trigger]:
        """Feed one completed bar. Returns any triggers it produced."""
        self.bars_seen += 1

        # Zero-volume bars are dropped before any setup sees them. IBKR pads
        # thin tickers with zero-volume bars that carry OHLC forward, which
        # would otherwise arm a setup on a minute in which nothing traded.
        # Dropping them also deletes exactly the bars a live stream would never
        # have emitted, which is what keeps Replay and Live in agreement.
        if bar.volume <= 0:
            self.skipped_zero_volume += 1
            return []

        levels = self.levels.get(bar.symbol)
        if levels is None:
            self.skipped_no_levels.add(bar.symbol)
            return []

        self.bars_evaluated += 1
        fired: list[Trigger] = []
        for cls in self.setup_classes:
            key = (bar.symbol, cls.name)
            setup = self._state.get(key)
            if setup is None:
                setup = cls(**self.setup_kwargs.get(cls.name, {}))
                self._state[key] = setup
            trigger = setup.on_bar(bar, levels)
            if trigger is not None:
                fired.append(trigger)

        self.triggers.extend(fired)
        return fired
