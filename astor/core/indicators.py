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
    from a Setup's own overshoot_high/undercut_low: those reset every time a
    setup fires and re-arms, so a second O&F later in the day only reflects
    its own local overshoot, not necessarily the day's real high if an
    earlier, bigger spike happened before this arming.
    """

    symbol: str
    cum_volume: float = 0.0
    cum_vol_price: float = 0.0  # sum(volume * typical_price)
    first_bar_start: datetime | None = None
    session_high: float | None = None
    session_low: float | None = None

    def update(self, bar: BarEvent) -> None:
        typical = (bar.high + bar.low + bar.close) / 3.0
        self.cum_volume += bar.volume
        self.cum_vol_price += bar.volume * typical
        if self.first_bar_start is None:
            self.first_bar_start = bar.start
        self.session_high = bar.high if self.session_high is None else max(
            self.session_high, bar.high
        )
        self.session_low = bar.low if self.session_low is None else min(
            self.session_low, bar.low
        )

    @property
    def avwap(self) -> float | None:
        return self.cum_vol_price / self.cum_volume if self.cum_volume > 0 else None


def compute_rvol(cum_volume: float, bar: BarEvent, curve: VolumeCurve) -> float | None:
    """Today's cumulative volume through `bar`, against the historical average
    cumulative volume at that same point in the session (curve.sessions RTH
    sessions back). 2.00 means today is running at double the average pace at
    this time of day; 0.50 means half. None if the curve has no coverage for
    this minute (e.g. a newly-added symbol whose curve is still building, or
    a bar outside the RTH minutes the curve was built from).
    """
    avg = curve.avg_cum_volume_at(bar.start.timetz().replace(tzinfo=None))
    if not avg:
        return None
    return round(cum_volume / avg, 2)
