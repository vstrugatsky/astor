"""Sell-rule tracking logic, verified against hand-built closes.

uv run python -m astor.test_tracking
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astor.core.tracking import SellRuleTracker, TrackedTrade

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 8, 14, 9, 30, tzinfo=ET)


def bar_time(i: int) -> datetime:
    return OPEN + timedelta(minutes=i)


def check(label: str, got, want) -> None:
    assert got == want, f"{label}: expected {want}, got {got}"


def test_trade_direction_and_1r_target() -> None:
    long = TrackedTrade("NVDA", entry=100.0, stop=95.0)
    check("long direction", long.direction, "long")
    check("long r", long.r, 5.0)
    check("long 1r target", long.target_1r, 105.0)

    short = TrackedTrade("NVDA", entry=100.0, stop=105.0)
    check("short direction", short.direction, "short")
    check("short r", short.r, 5.0)
    check("short 1r target", short.target_1r, 95.0)


def test_stop_equal_entry_rejected() -> None:
    try:
        TrackedTrade("NVDA", entry=100.0, stop=100.0)
        raise AssertionError("expected ValueError for stop == entry")
    except ValueError:
        pass


def test_long_arms_on_1r_close_not_before() -> None:
    tr = SellRuleTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)  # R=5, 1R target=105

    check("no alert below 1R", tr.on_close("NVDA", 104.9, bar_time(0)), None)
    alert = tr.on_close("NVDA", 105.0, bar_time(1))
    assert alert is not None and alert.kind == "breakeven"
    check("armed", tr.trades["NVDA"].state, "armed")
    check("peak set to arming close", tr.trades["NVDA"].peak, 105.0)


def test_long_sell_fires_on_trailing_giveback_from_peak() -> None:
    tr = SellRuleTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)  # R=5
    tr.on_close("NVDA", 105.0, bar_time(0))  # arms, peak=105

    # Runs further favorable -- peak should extend, no giveback alert yet.
    check("peak extends, no alert", tr.on_close("NVDA", 110.0, bar_time(1)), None)
    check("peak updated", tr.trades["NVDA"].peak, 110.0)

    # Gives back less than 0.5R (2.5) from the new peak (110) -- no alert.
    check("partial giveback, no alert", tr.on_close("NVDA", 108.0, bar_time(2)), None)

    # Gives back exactly 0.5R (2.5) from peak 110 -> 107.5 -- sell fires.
    alert = tr.on_close("NVDA", 107.5, bar_time(3))
    assert alert is not None and alert.kind == "sell"
    check("done after sell", tr.trades["NVDA"].state, "done")

    # No further alerts once done.
    check("no repeat after done", tr.on_close("NVDA", 90.0, bar_time(4)), None)


def test_short_mirrors_long() -> None:
    tr = SellRuleTracker()
    tr.track("TSLA", entry=100.0, stop=105.0)  # R=5, short, 1R target=95

    check("no alert above 1R", tr.on_close("TSLA", 95.1, bar_time(0)), None)
    alert = tr.on_close("TSLA", 95.0, bar_time(1))
    assert alert is not None and alert.kind == "breakeven"
    check("peak set", tr.trades["TSLA"].peak, 95.0)

    check("peak extends lower, no alert", tr.on_close("TSLA", 90.0, bar_time(2)), None)
    check("peak updated", tr.trades["TSLA"].peak, 90.0)

    # 0.5R = 2.5; giveback from peak 90 to 92.5 fires sell.
    alert = tr.on_close("TSLA", 92.5, bar_time(3))
    assert alert is not None and alert.kind == "sell"
    check("done after sell", tr.trades["TSLA"].state, "done")


def test_untracked_symbol_is_a_no_op() -> None:
    tr = SellRuleTracker()
    check("no trade tracked", tr.on_close("AAPL", 200.0, bar_time(0)), None)


def test_untrack_stops_further_evaluation() -> None:
    tr = SellRuleTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    check("untrack returns True when tracked", tr.untrack("NVDA"), True)
    check("untrack returns False when not tracked", tr.untrack("NVDA"), False)
    check("no alert after untrack", tr.on_close("NVDA", 200.0, bar_time(0)), None)


def test_retrack_replaces_prior_state() -> None:
    tr = SellRuleTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    tr.on_close("NVDA", 105.0, bar_time(0))  # arms
    check("armed before retrack", tr.trades["NVDA"].state, "armed")

    tr.track("NVDA", entry=100.0, stop=95.0)  # re-track resets state
    check("watching after retrack", tr.trades["NVDA"].state, "watching")
    check("peak cleared after retrack", tr.trades["NVDA"].peak, None)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
