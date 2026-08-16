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
    """Prior-session reference levels. RTH only, per the setup definitions."""

    symbol: str
    session: date  # the session these levels were taken from
    pdh: float
    pdl: float


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
