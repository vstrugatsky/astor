"""Running per-symbol session indicators, driven off the same bar stream as
Setup -- so, like Setup, this holds no reference to a dataset, a broker
connection, or a clock. That is what lets Replay compute the same AVWAP/RVOL
a Live run would have seen, ahead of any setup actually consuming them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from astor.core.types import BarEvent, VolumeCurve


@dataclass
class SessionIndicators:
    """AVWAP, the running volume this symbol's RVOL is computed from, and the
    session's true high/low -- all anchored to the first bar this instance
    ever sees. The caller (Scanner) is responsible for starting a fresh
    instance at the first bar of the day.

    session_high/session_low are the day's actual intraday extremes, distinct
    from a Reversal setup's own _extreme/_anchor (astor.core.setups): those
    reset whenever a leg fires and re-arms, so a second REV-S later in the
    day is scored against its own local leg, not necessarily the day's real
    high if an earlier, bigger spike happened before this leg started.

    session_open is just the first bar's open -- fixed for the rest of the
    session, unlike session_high/session_low which keep moving.

    session_high_at/session_low_at and cum_volume_at_high/cum_volume_at_low
    record *when* the current session_high/session_low were set, and the
    cumulative volume at that moment -- tracked here rather than by whatever
    consumes them (astor.core.setups' Reversal) specifically because this
    object, unlike a Setup, stays continuous through Scanner.backfill_bar.
    A symbol added to Live mid-session gets a Setup that starts clean (by
    design -- it must not re-detect a trigger from before it was tracked),
    but this keeps updating from bar 1 regardless of when a Setup starts
    consuming it -- so a Setup bootstrapping mid-session from these reads
    the *true* historical moment an extreme was set, not "whenever I
    started looking."
    """

    symbol: str
    cum_volume: float = 0.0
    cum_vol_price: float = 0.0  # sum(volume * typical_price)
    first_bar_start: datetime | None = None
    session_open: float | None = None
    session_high: float | None = None
    session_low: float | None = None
    session_high_at: datetime | None = None
    session_low_at: datetime | None = None
    cum_volume_at_high: float | None = None
    cum_volume_at_low: float | None = None
    # 9-period EMA of 1-min close, seeded from the first bar's close (no SMA
    # warmup -- same "just start accumulating from bar 1" treatment avwap
    # already gets). ema9_at_high/low freeze the reading at the moment the
    # current session_high/session_low were set, same relationship
    # cum_volume_at_high/low already has to session_high_at/low_at -- Reversal
    # reads these to score how extended a leg finished, without the retrace
    # that follows dragging price back toward the EMA first.
    ema9: float | None = None
    ema9_at_high: float | None = None
    ema9_at_low: float | None = None

    _EMA9_ALPHA = 2.0 / (9 + 1)

    def update(self, bar: BarEvent) -> None:
        typical = (bar.high + bar.low + bar.close) / 3.0
        self.cum_volume += bar.volume
        self.cum_vol_price += bar.volume * typical
        if self.first_bar_start is None:
            self.first_bar_start = bar.start
            self.session_open = bar.open
        self.ema9 = (
            bar.close
            if self.ema9 is None
            else self.ema9 + self._EMA9_ALPHA * (bar.close - self.ema9)
        )
        if self.session_high is None or bar.high > self.session_high:
            self.session_high = bar.high
            self.session_high_at = bar.start
            self.cum_volume_at_high = self.cum_volume
            self.ema9_at_high = self.ema9
        if self.session_low is None or bar.low < self.session_low:
            self.session_low = bar.low
            self.session_low_at = bar.start
            self.cum_volume_at_low = self.cum_volume
            self.ema9_at_low = self.ema9

    @property
    def avwap(self) -> float | None:
        return self.cum_vol_price / self.cum_volume if self.cum_volume > 0 else None


def compute_rvol(cum_volume: float, at: datetime, curve: VolumeCurve) -> float | None:
    """Today's cumulative volume through `at`, against the historical average
    cumulative volume at that same point in the session (curve.sessions RTH
    sessions back). 2.00 means today is running at double the average pace at
    this time of day; 0.50 means half. None if the curve has no coverage for
    this minute (e.g. a newly-added symbol whose curve is still building, or
    a bar outside the RTH minutes the curve was built from).

    `at` is a timestamp, not a BarEvent -- callers pass a bar's `.start`
    typically, but Reversal's move_rvol needs to pass session_high_at/
    session_low_at (astor.core.indicators), which aren't attached to any
    particular bar the current Setup instance necessarily saw itself.
    """
    avg = curve.avg_cum_volume_at(at.timetz().replace(tzinfo=None))
    if not avg:
        return None
    return round(cum_volume / avg, 2)
