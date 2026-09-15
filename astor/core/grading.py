"""Real-time setup-quality grading, 1 (weakest) - 4 (strongest).

Applied by Scanner right after a Setup fires (astor.core.scanner), using
data a Setup already computed (rvol, avwap, in trigger.extras) alongside
data Scanner already holds (levels, indicators).

This never filters anything: every trigger is stored regardless of grade
(astor.core.store), and Live's terminal alert stopped gating on it when
U&R/O&F were retired (see astor.drivers.live._should_notify) -- the grade
is recorded for review, nothing more. The result lives in
extras["auto_grade"] -- the program's own real-time call, distinct from the
trader's hand-entered `grade` column, so a later hand-edit never erases what
the rules actually said in the moment.
"""

from __future__ import annotations

from datetime import time

from astor.core.indicators import SessionIndicators
from astor.core.types import DailyLevels, Trigger

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# Minutes since RTH_OPEN a trigger must fire within to count as "early" --
# before minute 15 (9:45 ET) the action is often still too unstable to read;
# past minute 90 (11:00 ET) a breakout is no longer a fresh open-driven move.
EARLY_WINDOW_MINUTES = (15, 90)

# A trigger inside the last hour before RTH_CLOSE (15:00-16:00 ET) is treated
# as more likely a late-day chase than a fresh setup.
LAST_HOUR_MINUTES = 60

# The more liquid a name, the less relative volume it takes to look unusual --
# a $5B/day name moving on 15% extra volume is as meaningful as an illiquid
# one needing 50% extra. Ordered highest ADV floor first; the first matching
# tier wins.
_RVOL_TIERS = (
    (5_000_000_000, 1.15),
    (500_000_000, 1.3),
)
_DEFAULT_RVOL_THRESHOLD = 1.5

# How far price has already run from AVWAP, in ADR units, in the setup's own
# direction (above AVWAP for a long U&R, below it for a short O&F) -- a
# trigger that far from AVWAP is chasing a move that's already extended,
# with less room left to run than the setup's edge assumes. Ordered widest
# threshold first; the first one price clears wins.
_AVWAP_EXTENSION_TIERS = (
    (0.5, 2),
    (0.3, 1),
)


def rvol_threshold(adv: float) -> float:
    """The RVOL floor for a name of this ADV, highest-liquidity tier first.
    Shared with astor.core.setups (Reversal), not just this module's own
    grading -- a $5B/day name needing less relative volume to look unusual
    is a property of the name, not of any one setup.
    """
    for floor, threshold in _RVOL_TIERS:
        if adv > floor:
            return threshold
    return _DEFAULT_RVOL_THRESHOLD


def _avwap_extension_penalty(extension_adr: float) -> int:
    for floor, penalty in _AVWAP_EXTENSION_TIERS:
        if extension_adr > floor:
            return penalty
    return 0


def _minutes_since_open(t: time) -> int:
    return (t.hour * 60 + t.minute) - (RTH_OPEN.hour * 60 + RTH_OPEN.minute)


def grade_trigger(
    trigger: Trigger,
    levels: DailyLevels,
    indicators: SessionIndicators,
) -> int:
    """1-4, clamped. Starts at 1 (base); each factor below is independent,
    so nothing here disqualifies a trigger the way a Setup filter does --
    it only says how strong a trigger that already fired looks."""
    grade = 1
    price = trigger.trigger_price
    long = trigger.direction == "long"

    session_open = indicators.session_open
    if session_open is not None:
        beyond_open = price > session_open if long else price < session_open
        if beyond_open:
            grade += 1

    rvol = trigger.extras.get("rvol")
    if rvol is not None and rvol > rvol_threshold(levels.adv):
        grade += 1

    minutes = _minutes_since_open(trigger.bar_start.time())
    if EARLY_WINDOW_MINUTES[0] <= minutes <= EARLY_WINDOW_MINUTES[1]:
        grade += 1

    minutes_to_close = _minutes_since_open(RTH_CLOSE) - minutes
    if minutes_to_close <= LAST_HOUR_MINUTES:
        grade -= 1

    avwap = trigger.extras.get("avwap")
    adr = levels.adr
    if avwap is not None and adr > 0:
        extension = (price - avwap) if long else (avwap - price)
        grade -= _avwap_extension_penalty(extension / adr)

    return max(1, min(4, grade))
