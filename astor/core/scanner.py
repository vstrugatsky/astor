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
from astor.core.types import BarEvent, DailyLevels, SetupAlert, Trigger, VolumeCurve

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
        # Full-run history, mirrors self.triggers -- pop_alerts()
        # drains a separate per-on_bar-call buffer for callers that want just
        # what this call produced (Replay/Live), same relationship on_bar's
        # return value has to self.triggers.
        # Every setup-produced alert, whatever kind: Reversal's
        # capitulation heads-up and AVT's AVWAP touch both land here, and
        # each carries its own `kind` and store_extras() so drivers need no
        # per-kind branching to record one.
        self.alerts: list[SetupAlert] = []
        self._pending_alerts: list[SetupAlert] = []
        self.bars_seen = 0
        self.bars_evaluated = 0
        self.skipped_zero_volume = 0
        self.bars_backfilled = 0
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

    def on_bar(
        self, bar: BarEvent, trace: dict[str, list[dict]] | None = None
    ) -> list[Trigger]:
        """Feed one completed bar. Returns any triggers it produced.

        `trace`, if given, collects a human-readable diagnostic line per
        setup per decision point (astor.cli's `explain` command) -- keyed by
        setup name, since more than one setup can be active for a symbol.
        None (the default, what Replay/Live pass) costs nothing extra.
        """
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
            setup_trace = trace.setdefault(cls.name, []) if trace is not None else None
            trigger = setup.on_bar(bar, levels, indicators, curve, trace=setup_trace)
            if trigger is not None:
                auto_grade = grade_trigger(trigger, levels, indicators)
                trigger = replace(
                    trigger, extras={**trigger.extras, "auto_grade": auto_grade}
                )
                fired.append(trigger)
            alert = setup.pop_alert()
            if alert is not None:
                self._pending_alerts.append(alert)

        self.triggers.extend(fired)
        self.alerts.extend(self._pending_alerts)
        return fired

    def pop_alerts(self) -> list[SetupAlert]:
        """Setup alerts produced by the most recent on_bar() call --
        drained, not peeked, so a caller (Replay/Live) that calls this once
        per on_bar() never double-handles one."""
        alerts, self._pending_alerts = self._pending_alerts, []
        return alerts

    def backfill_bar(self, bar: BarEvent) -> None:
        """Feed one bar that happened before this symbol joined the live
        scan (or before a Replay run's start-time window).

        Everything is brought up to date -- indicators *and* setup state --
        but nothing is announced: whatever a setup would have fired or
        flagged on these bars is computed and discarded, since the trader
        was not watching this symbol then and a trigger from before that is
        not a trade.

        The setups used to be skipped here entirely, and the cost was
        silent. A setup's own bar history feeds accel_price/accel_vol, so a
        symbol added mid-session judged its leg on the handful of bars since
        the add rather than the whole leg -- and with fewer than
        accel_window + 2 of them, accel_price returns its neutral 1.0, which
        the 1.2 floor then rejects as though it had been measured. Real
        case: RXRX 2026-09-30 added around 10:18 produced no capitulation
        alert and no REV-S trigger, while the same leg added three minutes
        earlier produced both. Everything else about it passed.

        Running the setups here instead means a mid-session add behaves like
        a symbol watched since the open, including in what it does *not*
        re-announce: a leg that already fired before the add stays fired, so
        the second half of a move the trader missed is not served up as a
        fresh entry.
        """
        levels = self._gate(bar)
        if levels is None:
            return
        indicators = self.indicators.setdefault(bar.symbol, SessionIndicators(bar.symbol))
        indicators.update(bar)
        self.bars_backfilled += 1
        curve = self.curves.get(bar.symbol)
        for cls in self.setup_classes:
            key = (bar.symbol, cls.name)
            setup = self._state.get(key)
            if setup is None:
                setup = cls(**self.setup_kwargs.get(cls.name, {}))
                self._state[key] = setup
            # Return value and alert both dropped on the floor: this bar is
            # history, and history does not get announced.
            setup.on_bar(bar, levels, indicators, curve)
            setup.pop_alert()
