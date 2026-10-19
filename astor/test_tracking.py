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
    check("peak is the best close", tr.trades["NVDA", None].peak, 105.0)


def test_track_announces_exactly_what_trail_does() -> None:
    """Since 2026-10-19 track runs trail's rules: every action trail takes
    is a notice from track on the same close, long and short."""
    from datetime import time as dtime

    kinds = {"breakeven": "breakeven", "exit": "sell", "eod": "eod"}
    paths = [
        (100.0, 95.0, [101, 103, 105, 110, 108, 104.9, 90]),   # +1R, +2R, back 1R
        (100.0, 105.0, [99, 97, 95, 90, 92, 95.1, 110]),       # the short's mirror
        (100.0, 95.0, [102, 97.1, 96, 94]),                    # never +1R: 1R off its peak
    ]
    fired = 0
    for entry, stop, closes in paths:
        trail, track = TrailTracker(eod_exit=dtime(15, 59)), SellRuleTracker(eod_exit=dtime(15, 59))
        trail.track("NVDA", entry, stop)
        track.track("NVDA", entry, stop)
        for i, c in enumerate(closes):
            a, n = trail.on_close("NVDA", c, bar_time(i)), track.on_close("NVDA", c, bar_time(i))
            check(f"{entry}/{stop} close {c}", [kinds[x.kind] for x in a], [x.kind for x in n])
            fired += len(n)
    check("the paths set rules off", fired, 5)
    late = OPEN.replace(hour=15, minute=58)
    tr = SellRuleTracker(eod_exit=dtime(15, 59))
    tr.track("NVDA", 100.0, 95.0)
    check("under 2R at the close: a notice", close1(tr, "NVDA", 104.0, late, late + timedelta(minutes=1)).kind, "eod")


def test_track_sells_on_the_tiered_giveback() -> None:
    tr = SellRuleTracker()
    tr.track("NVDA", entry=100.0, stop=95.0)  # R=5
    check("+1R: breakeven", close1(tr, "NVDA", 105.0, bar_time(0)).kind, "breakeven")
    check("+2R: nothing", close1(tr, "NVDA", 110.0, bar_time(1)), None)
    check("0.5R back from +2R: nothing now (limit 1R)", close1(tr, "NVDA", 107.5, bar_time(2)), None)
    a = close1(tr, "NVDA", 105.0, bar_time(3))
    check("1R back: sell, all of it", (a.kind, a.rule, a.whole), ("sell", "gb", True))
    check("done after sell", tr.trades["NVDA", None].state, "done")
    check("no repeat after done", close1(tr, "NVDA", 90.0, bar_time(4)), None)


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
    check("peak back to entry after retrack", tr.trades["NVDA", None].peak, 100.0)


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


def _driver_holding(*held: tuple[str, str, float]):
    """A LiveDriver with only its books and a fake IB holding `held`
    (symbol, account, shares) -- enough for `_on_position`."""
    from types import SimpleNamespace as NS

    from astor.drivers.live import LiveDriver

    d = LiveDriver.__new__(LiveDriver)
    d.tracker, d.trailer = SellRuleTracker(), TrailTracker()
    positions = [NS(contract=NS(symbol=s), account=a, position=n) for s, a, n in held]
    d.ib = NS(positions=lambda: positions)
    return d


def _flat(symbol: str, account: str):
    from types import SimpleNamespace as NS

    return NS(contract=NS(symbol=symbol), account=account, position=0)


def test_flat_position_ends_its_trade() -> None:
    d = _driver_holding(("CCXI", "U7654329", -50))
    d.tracker.track("CCXI", 10.77, 10.50, account="U1234566")
    d.trailer.track("CCXI", 10.77, 11.00, account="U7654329")
    d._on_position(_flat("CCXI", "U1234566"))
    check("stopped-out trade dropped", ("CCXI", "U1234566") in d.tracker.trades, False)
    check("other account's trade kept", ("CCXI", "U7654329") in d.trailer.trades, True)


def test_flat_position_ends_an_unnamed_trade_only_when_nothing_is_held() -> None:
    d = _driver_holding(("CCXI", "U7654329", -50))
    d.tracker.track("CCXI", 10.77, 10.50)
    d._on_position(_flat("CCXI", "U1234566"))
    check("still held elsewhere -> kept", ("CCXI", None) in d.tracker.trades, True)
    d = _driver_holding()
    d.tracker.track("CCXI", 10.77, 10.50)
    d._on_position(_flat("CCXI", "U1234566"))
    check("held nowhere -> dropped", d.tracker.trades, {})


# -- exit plans: rules, shares, R coefficient, close ------------------------

def _plan(text: str):
    from astor.core.exit_plan import parse_trade_command

    return parse_trade_command(f"XYZ {text}").plan


def test_plan_parses_the_example() -> None:
    from astor.core.exit_plan import CommandError, allocate, parse_trade_command

    c = parse_trade_command("xyz 156.80 157.89 reg 1.5R 35% gb 35% avwap 30% be close exit")
    check("prices, account", (c.symbol, c.entry, c.stop, c.account), ("XYZ", 156.80, 157.89, "reg"))
    check("plan", c.plan.describe(), "1.5R; 35% gb, 35% avwap, 30% be; close exit")
    check("close exit", _plan("close exit").close_hold_r, None)
    check("close 2.5R", _plan("close 2.5R").close_hold_r, 2.5)
    check("close's R isn't the coefficient", (_plan("close 2.5R 1.5R").close_hold_r, _plan("close 2.5R 1.5R").r_mult), (2.5, 1.5))
    check("default: 2R", (_plan("").close_hold_r, _plan("").is_default), (2.0, True))
    check("nothing given: today's rule", parse_trade_command("XYZ").plan.is_default, True)
    check("the rest rides the stop", _plan("50% avwap0.1").describe(), "50% avwap0.1, 50% be")
    check("a bare rule takes it all", _plan("avwap").describe(), "100% avwap")
    check("shares: largest remainder", allocate(_plan("35% gb 35% avwap 30% be").legs, 7),
          {"gb": 3, "avwap": 2, "be": 2})
    for bad in ("35%", "60% gb 60% avwap", "35% gb 10% gb", "156.8", "35% xyz", "close", "close 35% gb"):
        try:
            parse_trade_command(f"XYZ {bad}")
        except CommandError:
            continue
        raise AssertionError(f"{bad!r} should be refused")


def test_r_coefficient_scales_every_r_rule() -> None:
    tr = TrailTracker(eod_exit=None)
    t = tr.track("XYZ", 100.0, 98.0, plan=_plan("1.5R"))
    check("R is 1.5x the stop distance", t.r, 3.0)
    check("+1R is 3 away", t.target_1r, 103.0)
    check("102.9 is short of it", close1(tr, "XYZ", 102.9, bar_time(1)), None)
    check("103 moves the stop", close1(tr, "XYZ", 103.0, bar_time(2)).kind, "breakeven")


def test_avwap_rule_arms_on_the_trade_side_then_fires_past_it() -> None:
    tr = TrailTracker(eod_exit=None)
    tr.track("XYZ", 100.0, 98.0, plan=_plan("avwap"))
    # yard 2.00 -> 0.05 x 2 = 0.10 past AVWAP 100.50
    check("below AVWAP at the start: waits", close1(tr, "XYZ", 100.0, bar_time(1), None, 100.5, 2.0), None)
    check("still below: still waits", close1(tr, "XYZ", 99.0, bar_time(2), None, 100.5, 2.0), None)
    check("above: arms", close1(tr, "XYZ", 101.0, bar_time(3), None, 100.5, 2.0), None)
    check("within 0.10 under: holds", close1(tr, "XYZ", 100.41, bar_time(4), None, 100.5, 2.0), None)
    a = close1(tr, "XYZ", 100.39, bar_time(5), None, 100.5, 2.0)
    check("past 0.10 under: sells", (a.kind, a.rule, a.whole), ("exit", "avwap", True))
    check("and that was all of it", tr.trades["XYZ", None].state, "done")


def test_avwap_rule_short_mirrors_long() -> None:
    tr = TrailTracker(eod_exit=None)
    tr.track("XYZ", 100.0, 102.0, plan=_plan("avwap"))
    close1(tr, "XYZ", 99.0, bar_time(1), None, 99.5, 2.0)  # under AVWAP: armed
    check("0.10 over: holds", close1(tr, "XYZ", 99.6, bar_time(2), None, 99.5, 2.0), None)
    check("past it: sells", close1(tr, "XYZ", 99.61, bar_time(3), None, 99.5, 2.0).rule, "avwap")


def test_partial_legs_fire_on_their_own() -> None:
    tr = TrailTracker(eod_exit=None)
    t = tr.track("XYZ", 100.0, 98.0, plan=_plan("35% gb 35% avwap 30% be"),
                 leg_shares={"gb": 35, "avwap": 35, "be": 30})
    close1(tr, "XYZ", 101.0, bar_time(1), None, 100.5, 2.0)  # armed above AVWAP
    a = close1(tr, "XYZ", 100.3, bar_time(2), None, 100.5, 2.0)
    check("avwap first: its share only", (a.rule, a.whole), ("avwap", False))
    check("still trailing", t.state, "watching")
    check("+1R: breakeven for the rest", close1(tr, "XYZ", 102.0, bar_time(3), None, 100.5, 2.0).kind, "breakeven")
    # peak 102 = +1R: limit is 50% of it, 1.00
    a = close1(tr, "XYZ", 101.0, bar_time(4), None, 100.5, 2.0)
    check("gb: its share, the be shares stay", (a.rule, a.whole, t.state), ("gb", False, "armed"))
    check("be shares ride the stop: nothing more", close1(tr, "XYZ", 100.2, bar_time(5), None, 100.5, 2.0), None)


def test_two_rules_on_one_close_end_with_the_whole() -> None:
    tr = TrailTracker(eod_exit=None)
    tr.track("XYZ", 100.0, 98.0, plan=_plan("50% gb 50% avwap"))
    tr.on_close("XYZ", 101.0, bar_time(1), None, 100.5, 2.0)
    out = tr.on_close("XYZ", 98.9, bar_time(2), None, 100.5, 2.0)  # gave back 2.1 and lost AVWAP
    check("both, last takes what's left", [(a.rule, a.whole) for a in out], [("gb", False), ("avwap", True)])


def test_close_sells_even_past_2r() -> None:
    from datetime import time as dtime

    for plan, want in (("", "breakeven"), ("close exit", "eod"), ("close 3R", "eod"), ("close 2.5R", "breakeven")):
        tr = TrailTracker(eod_exit=dtime(15, 59))
        tr.track("XYZ", 100.0, 98.0, plan=_plan(plan))
        late = OPEN.replace(hour=15, minute=58)
        a = close1(tr, "XYZ", 105.0, late, late + timedelta(minutes=1))  # +2.5R
        check(f"{plan or 'default'} at +2.5R", a.kind, want)


def test_track_notifies_per_rule() -> None:
    from datetime import time as dtime

    tr = SellRuleTracker()
    t = tr.track("XYZ", 100.0, 98.0, plan=_plan("50% gb 50% avwap close exit"), eod_exit=dtime(15, 59))
    tr.on_close("XYZ", 101.0, bar_time(1), None, 100.5, 2.0)
    a = close1(tr, "XYZ", 100.3, bar_time(2), None, 100.5, 2.0)
    check("avwap: a sell notice", (a.kind, a.rule), ("sell", "avwap"))
    check("+1R: breakeven", close1(tr, "XYZ", 102.0, bar_time(3), None, 100.5, 2.0).kind, "breakeven")
    a = close1(tr, "XYZ", 101.0, bar_time(4), None, 100.5, 2.0)
    check("0.5R back: gb's sell", (a.kind, a.rule, t.state), ("sell", "gb", "done"))
    tr2 = SellRuleTracker()
    tr2.track("XYZ", 100.0, 98.0, plan=_plan("close exit"), eod_exit=dtime(15, 59))
    late = OPEN.replace(hour=15, minute=58)
    check("close: an eod notice", close1(tr2, "XYZ", 101.0, late, late + timedelta(minutes=1)).kind, "eod")


def _executor_holding(held: float, stops: list[tuple[float, float]]):
    """A dry-run TrailExecutor over a fake IBKR: a long of `held` shares
    protected by SELL stops of (qty, price)."""
    import asyncio
    from types import SimpleNamespace as NS

    from astor.drivers.orders import TrailExecutor

    trades = [
        NS(contract=NS(symbol="XYZ"), orderStatus=NS(status="Submitted"),
           order=NS(account="U1", action="SELL", orderType="STP", totalQuantity=q,
                    auxPrice=p, parentId=0, orderId=-(i + 1)))
        for i, (q, p) in enumerate(stops)
    ]

    async def all_open():
        return trades

    ib = NS(
        positions=lambda account="": [NS(contract=NS(symbol="XYZ"), position=held)],
        reqAllOpenOrdersAsync=all_open,
    )
    ex = TrailExecutor(ib, True, lambda *a: None, lambda *a: None)
    return ex, asyncio.run


def test_partial_sale_cuts_the_furthest_stop_first() -> None:
    ex, run = _executor_holding(100, [(50, 97.0), (50, 98.0)])
    t = TrackedTrade("XYZ", 100.0, 98.0, account="U1")
    out = run(ex.exit_shares(t, 35))
    check("cut the 97 tranche by 35", "cut stop 50@97.00 -> 15" in out.text, True)
    check("then sells 35", "SELL 35 Adaptive" in out.text, True)
    out = run(ex.exit_shares(t, 70))
    check("70: cancels 97, cuts 98", "50@97.00 -> cancel, 50@98.00 -> 30" in out.text, True)
    out = run(ex.exit_shares(t, 100))
    check("everything: the whole exit", "cancel stop(s)" in out.text, True)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
