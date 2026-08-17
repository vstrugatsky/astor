"""The vocabulary shared by every driver and every setup.

BarEvent is deliberately the *only* thing a setup ever sees. Nothing here holds
a reference to a dataset, a broker connection, or a clock -- that is what makes
a Replay run and a Live run indistinguishable from inside a setup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class BarEvent:
    """One completed bar.

    `start` is the bar's timestamp as IBKR reports it (the bar's *start*).
    `end` is when the bar completed, and therefore the earliest instant a live
    system could have evaluated it. Setups are handed only completed bars.
    """

    symbol: str
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class DailyLevels:
    """Prior-session reference levels and volatility. RTH only, per the setups.

    `adr_pct` is the Deepvue-compatible definition -- the mean of (High/Low - 1)
    over the prior N sessions, as a percent. It is stored as a percent rather
    than dollars so it reconciles with the research platform and stays
    comparable across tickers; the dollar figure the filters actually use is
    derived from it against the prior close.
    """

    symbol: str
    session: date  # the session these levels were taken from
    pdh: float
    pdl: float
    adr_pct: float = 0.0
    prev_close: float = 0.0
    adr_sessions: int = 0

    @property
    def adr(self) -> float:
        """ADR in dollars, anchored to the prior close.

        Anchored to the prior close rather than the live price so the filter
        threshold is a constant for the whole session -- otherwise the bar that
        triggers a setup would move the yardstick used to judge it.
        """
        return self.adr_pct / 100.0 * self.prev_close


@dataclass(frozen=True, slots=True)
class Trigger:
    """A setup firing on a specific bar.

    `extras` carries the context the trader needs to tighten loose rules later
    -- how deep the undercut went, how long it lasted. Recording it now is what
    makes "develop the rules organically" possible without re-running history.
    """

    symbol: str
    setup: str
    direction: str
    trigger_price: float
    bar_start: datetime
    bar_end: datetime
    level_name: str
    level_value: float
    extras: dict[str, Any] = field(default_factory=dict)
