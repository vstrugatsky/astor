"""Setup and scanner logic, verified against hand-built bars.

uv run python -m astor.test_setups
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astor.core.scanner import Scanner
from astor.core.setups import OvershootFail, UndercutRally
from astor.core.types import BarEvent, DailyLevels
from astor.drivers.replay import ReplayDriver, run_replay

ET = ZoneInfo("America/New_York")
SESSION = date(2026, 8, 14)
OPEN = datetime(2026, 8, 14, 9, 30, tzinfo=ET)
LEVELS = DailyLevels("TEST", date(2026, 8, 13), pdh=105.0, pdl=100.0)


def bar(i: int, o, h, low, c, v=1000, symbol="TEST") -> BarEvent:
    start = OPEN + timedelta(minutes=i)
    return BarEvent(symbol, start, start + timedelta(minutes=1), o, h, low, c, v)


def check(label: str, got, want) -> None:
    assert got == want, f"{label}: expected {want}, got {got}"


def test_undercut_rally_basic() -> None:
    s = UndercutRally()
    # Above PDL -- nothing.
    check("no arm above level", s.on_bar(bar(0, 101, 102, 100.5, 101.5), LEVELS), None)
    # Dips below PDL, closes below -- armed, no trigger yet.
    check("arm on undercut", s.on_bar(bar(1, 101, 101, 99.0, 99.5), LEVELS), None)
    # Still below.
    check("stay armed", s.on_bar(bar(2, 99.5, 99.8, 98.5, 99.0), LEVELS), None)
    # Closes back above PDL -- trigger.
    t = s.on_bar(bar(3, 99, 101, 99, 100.5), LEVELS)
    assert t is not None, "expected U&R trigger"
    check("trigger price", t.trigger_price, 100.5)
    check("level", t.level_value, 100.0)
    check("direction", t.direction, "long")
    check("bars below", t.extras["bars_below"], 3)
    check("undercut low", t.extras["undercut_low"], 98.5)
    check("depth", t.extras["depth_below_level"], 1.5)
    # Re-armed: a bar above PDL must not fire again without a new undercut.
    check("no repeat fire", s.on_bar(bar(4, 100.5, 102, 100.4, 101.5), LEVELS), None)


def test_undercut_rally_same_bar_flag() -> None:
    # One bar that wicks below PDL and closes above it.
    wick = bar(1, 101, 101.5, 99.0, 100.8)

    off = UndercutRally(allow_same_bar_reclaim=False)
    check("same-bar suppressed by default", off.on_bar(wick, LEVELS), None)
    # It is armed though, so the next close above fires. That second bar never
    # dips below PDL, so it is an armed bar but not a bar below the level.
    t = off.on_bar(bar(2, 100.8, 101.2, 100.7, 101.0), LEVELS)
    assert t is not None, "expected trigger on following bar"
    check("only the wick bar broke the level", t.extras["bars_below"], 1)
    check("but two bars elapsed since the breach", t.extras["bars_armed"], 2)

    on = UndercutRally(allow_same_bar_reclaim=True)
    t2 = on.on_bar(wick, LEVELS)
    assert t2 is not None, "expected same-bar trigger when enabled"
    check("same-bar bars_below", t2.extras["bars_below"], 1)
    check("same-bar price", t2.trigger_price, 100.8)


def test_undercut_rally_fires_twice() -> None:
    s = UndercutRally()
    fires = 0
    for b in [
        bar(0, 101, 101, 99, 99.5),  # arm
        bar(1, 99.5, 101, 99.5, 100.5),  # fire 1
        bar(2, 100.5, 101, 98, 99),  # arm again
        bar(3, 99, 101, 99, 100.7),  # fire 2
    ]:
        if s.on_bar(b, LEVELS):
            fires += 1
    check("two undercut-reclaim cycles fire twice", fires, 2)


def test_overshoot_fail_basic() -> None:
    s = OvershootFail()
    check("no arm below level", s.on_bar(bar(0, 104, 104.5, 103, 104), LEVELS), None)
    check("arm on overshoot", s.on_bar(bar(1, 104.5, 106.5, 104.5, 106), LEVELS), None)
    t = s.on_bar(bar(2, 106, 106, 104, 104.5), LEVELS)
    assert t is not None, "expected O&F trigger"
    check("trigger price", t.trigger_price, 104.5)
    check("level", t.level_value, 105.0)
    check("direction", t.direction, "short")
    check("bars above", t.extras["bars_above"], 2)
    check("overshoot high", t.extras["overshoot_high"], 106.5)
    check("height", t.extras["height_above_level"], 1.5)


def test_boundary_is_strict() -> None:
    # Exactly touching the level is not a breach, and closing exactly at the
    # level is not a reclaim. Strict inequalities both ways.
    s = UndercutRally()
    check(
        "touch PDL is not undercut",
        s.on_bar(bar(0, 101, 101, 100.0, 100.5), LEVELS),
        None,
    )
    s.on_bar(bar(1, 101, 101, 99, 99), LEVELS)  # arm
    check(
        "close AT PDL is not reclaim",
        s.on_bar(bar(2, 99, 100.0, 99, 100.0), LEVELS),
        None,
    )
    assert s.on_bar(bar(3, 100, 100.5, 100, 100.01), LEVELS) is not None


def test_scanner_drops_zero_volume() -> None:
    sc = Scanner({"TEST": LEVELS}, [UndercutRally])
    sc.on_bar(bar(0, 101, 101, 99, 99))  # arm, real volume
    # A padded zero-volume bar carrying price above PDL must not fire.
    check("zero-volume dropped", sc.on_bar(bar(1, 100.5, 100.5, 100.5, 100.5, v=0)), [])
    check("counted as skipped", sc.skipped_zero_volume, 1)
    check("not evaluated", sc.bars_evaluated, 1)
    # A real bar at the same price does fire, proving the level logic was fine
    # and it was the volume filter that suppressed the phantom.
    assert sc.on_bar(bar(2, 100.5, 100.6, 100.4, 100.5)), "real bar should fire"


def test_scanner_skips_symbols_without_levels() -> None:
    sc = Scanner({"TEST": LEVELS}, [UndercutRally])
    check("unknown symbol dropped", sc.on_bar(bar(0, 1, 2, 0.5, 1, symbol="NOLVL")), [])
    check("recorded", sc.skipped_no_levels, {"NOLVL"})


def test_stream_is_chronological_across_symbols() -> None:
    a = [bar(0, 1, 1, 1, 1, symbol="AAA"), bar(2, 1, 1, 1, 1, symbol="AAA")]
    b = [bar(1, 1, 1, 1, 1, symbol="BBB"), bar(2, 1, 1, 1, 1, symbol="BBB")]
    out = list(ReplayDriver.stream({"AAA": a, "BBB": b}))
    got = [(e.start.strftime("%H:%M"), e.symbol) for e in out]
    check(
        "merged in time order, ties by symbol",
        got,
        [("09:30", "AAA"), ("09:31", "BBB"), ("09:32", "AAA"), ("09:32", "BBB")],
    )


def test_stream_window_filter() -> None:
    bars = {"AAA": [bar(i, 1, 1, 1, 1, symbol="AAA") for i in range(10)]}
    out = list(ReplayDriver.stream(bars, time(9, 32), time(9, 35)))
    check(
        "window start inclusive, end exclusive",
        [e.start.strftime("%H:%M") for e in out],
        ["09:32", "09:33", "09:34"],
    )


def test_run_replay_end_to_end() -> None:
    bars = {
        "TEST": [
            bar(0, 101, 101, 100.5, 101),
            bar(1, 101, 101, 99, 99.5),  # arm
            bar(2, 99.5, 101, 99.5, 100.6),  # fire
        ]
    }
    sc = Scanner({"TEST": LEVELS}, [UndercutRally, OvershootFail])
    seen = []
    fed = run_replay(sc, bars, on_trigger=seen.append)
    check("bars fed", fed, 3)
    check("one trigger", len(seen), 1)
    check(
        "detection ts is bar completion",
        seen[0].bar_end,
        seen[0].bar_start + timedelta(minutes=1),
    )


def test_bars_beyond_counts_only_bars_past_the_level() -> None:
    """bars_below is time spent below the level, not time spent armed.

    These diverge whenever a filter suppresses a reclaim: the setup stays armed
    while price sits back above the level. Counting those bars as "below"
    overstated how sustained the breach was -- AAPL reported 52 when only 39
    bars had actually broken the level.
    """
    s = UndercutRally(min_reclaim_adr=0.1)  # needs $1.00 reclaim on $10 ADR
    s.on_bar(bar(0, 101, 101, 97, 97.5), ADR_LEVELS)  # breaks level, armed
    # Three bars back above PDL but reclaiming too weakly to fire. They are
    # armed bars, not bars below the level.
    for i in (1, 2, 3):
        check(
            f"weak reclaim bar {i} suppressed",
            s.on_bar(bar(i, 100.2, 100.4, 100.1, 100.3), ADR_LEVELS),
            None,
        )
    t = s.on_bar(bar(4, 100.3, 101.5, 100.2, 101.2), ADR_LEVELS)
    assert t is not None, "expected fire once the reclaim was convincing"
    check("bars actually below the level", t.extras["bars_below"], 1)
    check("bars since the breach", t.extras["bars_armed"], 5)


def test_bars_above_counts_only_bars_past_the_level() -> None:
    s = OvershootFail(min_reclaim_adr=0.1)
    s.on_bar(bar(0, 105, 108, 105, 107.5), ADR_LEVELS)  # breaks PDH, armed
    for i in (1, 2):
        check(
            f"weak fail bar {i} suppressed",
            s.on_bar(bar(i, 104.9, 104.95, 104.8, 104.9), ADR_LEVELS),
            None,
        )
    t = s.on_bar(bar(3, 104.9, 104.9, 103, 103.5), ADR_LEVELS)
    assert t is not None, "expected fire on a decisive fail"
    check("bars actually above the level", t.extras["bars_above"], 1)
    check("bars since the breach", t.extras["bars_armed"], 4)


def test_reclaim_may_span_many_bars() -> None:
    """A reclaim is not required to complete in one bar.

    It is a point-in-time distance from the level, re-tested on every bar while
    the setup stays armed, so price may grind up over many bars and fire when it
    finally gets far enough past.
    """
    s = UndercutRally(min_reclaim_adr=0.1)  # needs close >= 101.00
    s.on_bar(bar(0, 101, 101, 97, 97.5), ADR_LEVELS)
    closes = [100.2, 100.5, 100.8, 100.95]  # creeping up, all short of 101.00
    for i, c in enumerate(closes, start=1):
        check(
            f"still short at {c}", s.on_bar(bar(i, c, c, c - 0.1, c), ADR_LEVELS), None
        )
    t = s.on_bar(bar(5, 100.95, 101.3, 100.9, 101.05), ADR_LEVELS)
    assert t is not None, "a gradual reclaim must still fire"
    check("fired on the 6th bar of the sequence", t.extras["bars_armed"], 6)


def test_compute_adr_pct() -> None:
    """Deepvue definition: mean of (High/Low - 1), as a percent."""
    from astor.drivers.replay import compute_adr_pct

    class B:
        def __init__(self, h, low):
            self.high, self.low = h, low

    # Three sessions with ranges of exactly 10%, 20%, 30% -> mean 20%.
    adr, used = compute_adr_pct([B(110, 100), B(120, 100), B(130, 100)], 3)
    check("adr percent", adr, 20.0)
    check("sessions used", used, 3)

    # Only the last N sessions count.
    adr2, used2 = compute_adr_pct([B(200, 100), B(110, 100), B(110, 100)], 2)
    check("window respected", adr2, 10.0)
    check("sessions used", used2, 2)

    # A bad bar is skipped, not a division error; no usable bars -> 0.0.
    adr3, used3 = compute_adr_pct([B(110, 0), B(120, 100)], 2)
    check("bad low skipped", adr3, 20.0)
    check("count excludes bad bar", used3, 1)
    check("no usable bars", compute_adr_pct([B(110, 0)], 2), (0.0, 0))


# ADR of 10% on a $100 prior close -> $10.00 of ADR.
ADR_LEVELS = DailyLevels(
    "TEST",
    date(2026, 8, 13),
    pdh=105.0,
    pdl=100.0,
    adr_pct=10.0,
    prev_close=100.0,
    adr_sessions=14,
)


def test_adr_property() -> None:
    check("adr in dollars", ADR_LEVELS.adr, 10.0)
    check("no adr data -> zero", LEVELS.adr, 0.0)


def test_excursion_filter_rejects_shallow_breach() -> None:
    """A 1-cent undercut is chop, not a setup, at a 0.1 ADR threshold."""
    s = UndercutRally(min_excursion_adr=0.1)  # needs >= $1.00 of undercut
    s.on_bar(bar(0, 100.5, 100.5, 99.99, 100.2), ADR_LEVELS)  # 0.01 deep -> armed
    check(
        "shallow breach suppressed",
        s.on_bar(bar(1, 100.2, 101, 100.1, 100.9), ADR_LEVELS),
        None,
    )
    # Still armed. A deeper push then a reclaim does fire.
    s.on_bar(bar(2, 100.2, 100.2, 98.5, 98.8), ADR_LEVELS)  # now 1.50 deep
    t = s.on_bar(bar(3, 98.8, 101, 98.8, 100.9), ADR_LEVELS)
    assert t is not None, "expected fire once the undercut was deep enough"
    check("excursion in adr units", t.extras["excursion_adr"], 0.15)


def test_reclaim_filter_rejects_limp_reclaim() -> None:
    """Closing a cent past the level is not a reclaim worth an alert."""
    s = UndercutRally(min_reclaim_adr=0.1)  # needs close >= $1.00 above PDL
    s.on_bar(bar(0, 101, 101, 97, 97.5), ADR_LEVELS)  # deep undercut, armed
    check(
        "limp reclaim suppressed",
        s.on_bar(bar(1, 97.5, 100.2, 97.5, 100.01), ADR_LEVELS),
        None,
    )
    # Stays armed, so a convincing reclaim later still fires.
    t = s.on_bar(bar(2, 100.01, 101.5, 100.0, 101.2), ADR_LEVELS)
    assert t is not None, "expected fire on a convincing reclaim"
    check("reclaim in adr units", t.extras["reclaim_adr"], 0.12)


def test_filters_apply_to_short_side() -> None:
    s = OvershootFail(min_excursion_adr=0.1, min_reclaim_adr=0.1)
    s.on_bar(bar(0, 105, 105.05, 105, 105.02), ADR_LEVELS)  # 0.05 over -> armed
    check(
        "shallow overshoot suppressed",
        s.on_bar(bar(1, 105.02, 105.02, 104.9, 104.99), ADR_LEVELS),
        None,
    )
    s.on_bar(bar(2, 105, 107, 105, 106.5), ADR_LEVELS)  # now 2.00 over
    check(
        "limp fail suppressed",
        s.on_bar(bar(3, 106.5, 106.5, 104.9, 104.99), ADR_LEVELS),
        None,
    )
    t = s.on_bar(bar(4, 104.99, 105, 103, 103.5), ADR_LEVELS)
    assert t is not None, "expected fire on a decisive fail"
    check("excursion adr", t.extras["excursion_adr"], 0.2)
    check("reclaim adr", t.extras["reclaim_adr"], 0.15)


def test_filters_skipped_without_adr() -> None:
    """No ADR means no scale; filter rather than reject everything silently."""
    s = UndercutRally(min_excursion_adr=99.0, min_reclaim_adr=99.0)
    s.on_bar(bar(0, 101, 101, 99, 99.5), LEVELS)  # LEVELS has adr_pct 0
    t = s.on_bar(bar(1, 99.5, 101, 99.5, 100.5), LEVELS)
    assert t is not None, "unscalable filters must not silently suppress"
    check("adr units unavailable", t.extras["excursion_adr"], None)


def test_store_view_keeps_eastern_time() -> None:
    """The view must report the session's own clock, not UTC.

    SQLite's date()/time() parse the ISO offset and normalize to UTC, which
    shifted every trigger 4 hours forward and made RTH triggers appear after
    the close. This is the guard against that returning.
    """
    import tempfile

    from astor.core.store import TriggerStore
    from astor.core.types import Trigger

    path = Path(tempfile.mkdtemp()) / "t.db"
    store = TriggerStore(path)
    store.start_run("r1", "replay", {})
    trig = Trigger(
        symbol="TEST",
        setup="U&R",
        direction="long",
        trigger_price=100.5,
        bar_start=OPEN,
        bar_end=OPEN + timedelta(minutes=1),
        level_name="PDL",
        level_value=100.0,
        extras={"bars_below": 3, "depth_below_level": 1.5, "undercut_low": 98.5},
    )
    store.record("r1", trig, trig.bar_end)
    store.commit()

    row = store.conn.execute(
        "SELECT session, trigger_time, bars_beyond, excursion, extreme"
        " FROM latest_triggers"
    ).fetchone()
    check("session date is ET", row[0], "2026-08-14")
    check("trigger time is ET, not UTC", row[1], "09:30:00")
    check("bars_beyond coalesced", row[2], 3)
    check("excursion coalesced", row[3], 1.5)
    check("extreme coalesced", row[4], 98.5)
    check("WAL enabled", store.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
    store.close()


def test_store_view_coalesces_short_setup() -> None:
    """O&F records the mirrored extras keys; the view must map them too."""
    import tempfile

    from astor.core.store import TriggerStore
    from astor.core.types import Trigger

    store = TriggerStore(Path(tempfile.mkdtemp()) / "t.db")
    store.start_run("r1", "replay", {})
    store.record(
        "r1",
        Trigger(
            symbol="TEST",
            setup="O&F",
            direction="short",
            trigger_price=104.5,
            bar_start=OPEN,
            bar_end=OPEN + timedelta(minutes=1),
            level_name="PDH",
            level_value=105.0,
            extras={
                "bars_above": 2,
                "height_above_level": 1.5,
                "overshoot_high": 106.5,
            },
        ),
        OPEN + timedelta(minutes=1),
    )
    store.commit()
    row = store.conn.execute(
        "SELECT bars_beyond, excursion, extreme FROM latest_triggers"
    ).fetchone()
    check("short-side coalesce", row, (2, 1.5, 106.5))
    store.close()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
