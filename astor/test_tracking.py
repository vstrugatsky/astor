"""Sell-rule tracking logic, verified against hand-built closes.

uv run python -m astor.test_tracking
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astor.core.tracking import SellRuleTracker, TrackedTrade, TrailTracker
from astor.drivers.orders import breakeven_price

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 8, 14, 9, 30, tzinfo=ET)


def bar_time(i: int) -> datetime:
    return OPEN + timedelta(minutes=i)


def close1(tr, *args):
    """The one alert/action a close sets off on a single-account book, or None."""
    out = tr.on_close(*args)
    assert len(out) <= 1, out
    return out[0] if out else None


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

    check("no alert below 1R", close1(tr, "NVDA", 104.9, bar_time(0)), None)
    alert = close1(tr, "NVDA", 105.0, bar_time(1))
    assert alert is not None and alert.kind == "breakeven"
    check("armed", tr.trades["NVDA", None].state, "armed")
    check("peak set to arming close", tr.trades["NVDA", None].peak, 105.0)


def test_long_sell_fires_on_trailing_giveback_from_peak() -> None:
    tr = SellRuleTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)  # R=5
    close1(tr, "NVDA", 105.0, bar_time(0))  # arms, peak=105

    # Runs further favorable -- peak should extend, no giveback alert yet.
    check("peak extends, no alert", close1(tr, "NVDA", 110.0, bar_time(1)), None)
    check("peak updated", tr.trades["NVDA", None].peak, 110.0)

    # Gives back less than 0.5R (2.5) from the new peak (110) -- no alert.
    check("partial giveback, no alert", close1(tr, "NVDA", 108.0, bar_time(2)), None)

    # Gives back exactly 0.5R (2.5) from peak 110 -> 107.5 -- sell fires.
    alert = close1(tr, "NVDA", 107.5, bar_time(3))
    assert alert is not None and alert.kind == "sell"
    check("done after sell", tr.trades["NVDA", None].state, "done")

    # No further alerts once done.
    check("no repeat after done", close1(tr, "NVDA", 90.0, bar_time(4)), None)


def test_short_mirrors_long() -> None:
    tr = SellRuleTracker()
    tr.track("TSLA", entry=100.0, stop=105.0)  # R=5, short, 1R target=95

    check("no alert above 1R", close1(tr, "TSLA", 95.1, bar_time(0)), None)
    alert = close1(tr, "TSLA", 95.0, bar_time(1))
    assert alert is not None and alert.kind == "breakeven"
    check("peak set", tr.trades["TSLA", None].peak, 95.0)

    check("peak extends lower, no alert", close1(tr, "TSLA", 90.0, bar_time(2)), None)
    check("peak updated", tr.trades["TSLA", None].peak, 90.0)

    # 0.5R = 2.5; giveback from peak 90 to 92.5 fires sell.
    alert = close1(tr, "TSLA", 92.5, bar_time(3))
    assert alert is not None and alert.kind == "sell"
    check("done after sell", tr.trades["TSLA", None].state, "done")


def test_untracked_symbol_is_a_no_op() -> None:
    tr = SellRuleTracker()
    check("no trade tracked", close1(tr, "AAPL", 200.0, bar_time(0)), None)


def test_untrack_stops_further_evaluation() -> None:
    tr = SellRuleTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    check("untrack drops the trade", len(tr.untrack("NVDA")), 1)
    check("untrack drops nothing when not tracked", tr.untrack("NVDA"), [])
    check("no alert after untrack", close1(tr, "NVDA", 200.0, bar_time(0)), None)


def test_retrack_replaces_prior_state() -> None:
    tr = SellRuleTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    close1(tr, "NVDA", 105.0, bar_time(0))  # arms
    check("armed before retrack", tr.trades["NVDA", None].state, "armed")

    tr.track("NVDA", entry=100.0, stop=95.0)  # re-track resets state
    check("watching after retrack", tr.trades["NVDA", None].state, "watching")
    check("peak cleared after retrack", tr.trades["NVDA", None].peak, None)


# -- trail ----------------------------------------------------------------
# R=5 throughout: entry 100, stop 95 (long) or 105 (short).


def test_trail_exits_on_1r_giveback_before_reaching_1r() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0, account="U1")
    check("+0.5R, no action", close1(tr, "NVDA", 102.5, bar_time(0)), None)
    check("back to entry, 0.5R given back", close1(tr, "NVDA", 100.0, bar_time(1)), None)
    action = close1(tr, "NVDA", 97.5, bar_time(2))  # -0.5R: 1R from 102.5
    assert action is not None and action.kind == "exit", action
    check("giveback", action.giveback, 5.0)
    check("limit is 1R", action.limit, 5.0)
    check("done", tr.trades["NVDA", "U1"].state, "done")
    check("no repeat after exit", close1(tr, "NVDA", 90.0, bar_time(3)), None)


def test_trail_never_favorable_exits_at_the_stop_level() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    check("-0.9R, no action", close1(tr, "NVDA", 95.5, bar_time(0)), None)
    action = close1(tr, "NVDA", 95.0, bar_time(1))
    assert action is not None and action.kind == "exit"


def test_trail_breakeven_at_1r_then_half_the_peak() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    action = close1(tr, "NVDA", 105.0, bar_time(0))
    assert action is not None and action.kind == "breakeven"
    check("peak +1.4R, no action", close1(tr, "NVDA", 107.0, bar_time(1)), None)
    # 50% of 7.00 = 3.50 -> exits at 103.50 (+0.7R), not before
    check("3.49 back, no action", close1(tr, "NVDA", 103.51, bar_time(2)), None)
    action = close1(tr, "NVDA", 103.5, bar_time(3))
    assert action is not None and action.kind == "exit"
    check("limit is half the peak", action.limit, 3.5)


def test_trail_caps_giveback_at_1r_from_2r_on() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    close1(tr, "NVDA", 105.0, bar_time(0))  # breakeven
    check("peak +3R", close1(tr, "NVDA", 115.0, bar_time(1)), None)
    # 1R (5.00), not 50% of 15.00
    check("4.99 back, no action", close1(tr, "NVDA", 110.01, bar_time(2)), None)
    action = close1(tr, "NVDA", 110.0, bar_time(3))
    assert action is not None and action.kind == "exit"
    check("limit is 1R", action.limit, 5.0)


def test_trail_limit_meets_at_2r() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    close1(tr, "NVDA", 105.0, bar_time(0))
    close1(tr, "NVDA", 110.0, bar_time(1))  # exactly +2R
    action = close1(tr, "NVDA", 105.0, bar_time(2))
    assert action is not None and action.kind == "exit"
    check("limit at 2R is 1R", action.limit, 5.0)


def test_trail_breakeven_fires_once() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    assert close1(tr, "NVDA", 105.0, bar_time(0)).kind == "breakeven"
    check("second 1R close, no action", close1(tr, "NVDA", 105.5, bar_time(1)), None)


def test_trail_short_mirrors_long() -> None:
    tr = TrailTracker()
    tr.track("TSLA", entry=100.0, stop=105.0)
    assert close1(tr, "TSLA", 95.0, bar_time(0)).kind == "breakeven"
    check("peak -1.6R", close1(tr, "TSLA", 92.0, bar_time(1)), None)
    # 50% of 8.00 = 4.00 -> exits at 96.00
    check("3.99 back", close1(tr, "TSLA", 95.99, bar_time(2)), None)
    action = close1(tr, "TSLA", 96.0, bar_time(3))
    assert action is not None and action.kind == "exit"


def test_breakeven_price_rounds_away_from_a_loss() -> None:
    check("long rounds up", breakeven_price(TrackedTrade("A", 100.4512, 95.0)), 100.46)
    check("short rounds down", breakeven_price(TrackedTrade("A", 100.4588, 105.0)), 100.45)
    check("exact cent kept", breakeven_price(TrackedTrade("A", 100.45, 95.0)), 100.45)
    check("sub-dollar tick", breakeven_price(TrackedTrade("A", 0.51234, 0.45)), 0.5124)


# -- trail: end of day ----------------------------------------------------
# OPEN + 388 min is the 15:58 bar, the one ending at 15:59.


def eod_bar(i: int = 388) -> tuple[datetime, datetime]:
    return bar_time(i), bar_time(i + 1)


def test_trail_eod_exits_under_2r() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    close1(tr, "NVDA", 105.0, bar_time(0))  # breakeven
    check("15:57 bar, no action", close1(tr, "NVDA", 109.0, *eod_bar(387)), None)
    action = close1(tr, "NVDA", 109.0, *eod_bar())  # +1.8R at 15:59
    assert action is not None and action.kind == "eod", action
    check("done", tr.trades["NVDA", None].state, "done")


def test_trail_eod_exits_a_loser() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    action = close1(tr, "NVDA", 98.0, *eod_bar())
    assert action is not None and action.kind == "eod"


def test_trail_eod_holds_at_2r_or_more() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    close1(tr, "NVDA", 105.0, bar_time(0))
    check("+2R at 15:59, held", close1(tr, "NVDA", 110.0, *eod_bar()), None)
    check("still open", tr.trades["NVDA", None].state, "armed")


def test_trail_eod_beats_breakeven_on_the_same_bar() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    action = close1(tr, "NVDA", 106.0, *eod_bar())  # first +1R close, at 15:59
    assert action is not None and action.kind == "eod"


def test_trail_eod_short_measures_gain_downward() -> None:
    tr = TrailTracker()
    tr.track("TSLA", entry=100.0, stop=105.0)
    close1(tr, "TSLA", 95.0, bar_time(0))
    check("short +2.2R held", close1(tr, "TSLA", 89.0, *eod_bar()), None)
    tr2 = TrailTracker()
    tr2.track("TSLA", entry=100.0, stop=105.0)
    close1(tr2, "TSLA", 95.0, bar_time(0))
    action = close1(tr2, "TSLA", 92.0, *eod_bar())  # +1.6R
    assert action is not None and action.kind == "eod"


def test_trail_eod_off_when_disabled() -> None:
    tr = TrailTracker(eod_exit=None)
    tr.track("NVDA", entry=100.0, stop=95.0)
    check("no eod rule", close1(tr, "NVDA", 101.0, *eod_bar()), None)


def test_trail_eod_follows_an_early_close() -> None:
    from datetime import time

    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0, eod_exit=time(12, 59))  # 13:00 close
    check("12:57 bar, no action", close1(tr, "NVDA", 101.0, *eod_bar(207)), None)
    action = close1(tr, "NVDA", 101.0, *eod_bar(208))  # 12:58 bar, ends 12:59
    assert action is not None and action.kind == "eod", action


def test_trail_eod_default_is_1559() -> None:
    tr = TrailTracker()
    check("default cutoff", tr.track("NVDA", 100.0, 95.0).eod_exit.strftime("%H:%M"), "15:59")




# -- one trade per symbol per account -------------------------------------


def test_two_accounts_track_the_same_symbol_apart() -> None:
    tr = SellRuleTracker()
    tr.track("CBRS", entry=156.80, stop=157.89, account="REG")  # short
    tr.track("CBRS", entry=151.35, stop=149.00, account="IRA")  # long
    check("both kept", sorted(a for _, a in tr.trades), ["IRA", "REG"])
    alerts = tr.on_close("CBRS", 155.70, bar_time(0))  # short +1R, long +1.85R
    check("each alerts for its own account", sorted(a.account for a in alerts), ["IRA", "REG"])


def test_untrack_one_account_keeps_the_other() -> None:
    tr = TrailTracker()
    tr.track("CBRS", entry=156.80, stop=157.89, account="REG")
    tr.track("CBRS", entry=151.35, stop=149.00, account="IRA")
    check("one dropped", [t.account for t in tr.untrack("CBRS", "IRA")], ["IRA"])
    check("other kept", list(tr.trades), [("CBRS", "REG")])
    check("no account drops all", len(tr.untrack("CBRS")), 1)


def test_unknown_account_and_named_account_replace_each_other() -> None:
    tr = SellRuleTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)
    tr.track("NVDA", entry=100.0, stop=95.0, account="REG")
    check("named replaces unnamed", list(tr.trades), [("NVDA", "REG")])
    tr.track("NVDA", entry=100.0, stop=95.0)
    check("unnamed replaces named", list(tr.trades), [("NVDA", None)])


# -- trail started on a trade already under way ---------------------------


def test_trail_exits_on_a_giveback_from_before_it_started() -> None:
    # 2026-10-12: CBRS short, best close 149.50 before `trail`, then 150.78
    # -- 1.17R back from +6.7R, over the 1R allowed.
    tr = TrailTracker()
    tr.track("CBRS", entry=156.80, stop=157.89, account="REG", peak=149.50)
    action = close1(tr, "CBRS", 150.78, bar_time(0))
    assert action is not None and action.kind == "exit", action
    check("account carried", action.account, "REG")


def test_trail_breakeven_off_a_peak_from_before_it_started() -> None:
    tr = TrailTracker()
    tr.track("NVDA", entry=100.0, stop=95.0, peak=106.0)  # was +1.2R
    action = close1(tr, "NVDA", 104.0, bar_time(0))  # +0.8R: 2.00 back of 3.00
    assert action is not None and action.kind == "breakeven", action


def test_trail_peak_never_worse_than_entry() -> None:
    tr = TrailTracker()
    trade = tr.track("NVDA", entry=100.0, stop=95.0, peak=98.0)
    check("peak at entry", trade.peak, 100.0)


def test_opening_fill_time_walks_back_to_zero() -> None:
    from types import SimpleNamespace

    from astor.drivers.live import opening_fill_time

    def fill(minute: int, side: str, shares: float):
        return SimpleNamespace(time=bar_time(minute), execution=SimpleNamespace(side=side, shares=shares))

    # in 10 at :00, out at :05, short 4 at :10 then 2 more at :12
    fills = [fill(0, "BOT", 10), fill(5, "SLD", 10), fill(10, "SLD", 4), fill(12, "SLD", 2)]
    check("opened by the :10 fill", opening_fill_time(-6, fills), bar_time(10))
    check("flip in one fill", opening_fill_time(-4, [fill(0, "BOT", 6), fill(3, "SLD", 10)]), bar_time(3))
    check("held from before today", opening_fill_time(-8, fills[2:]), None)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
