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
    # Absolute dollar figures are deliberately not stored -- only ADR units.
    for gone in ("depth_below_level", "reclaim_close", "reclaim_distance", "adr"):
        assert gone not in t.extras, f"{gone} should no longer be recorded"
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
    for gone in ("height_above_level", "fail_close", "reclaim_distance", "adr"):
        assert gone not in t.extras, f"{gone} should no longer be recorded"


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
    s = UndercutRally(min_reclaim_pct=1.0)  # needs a 1% reclaim on PDL=100
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
    s = OvershootFail(min_reclaim_pct=1.0)
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
    s = UndercutRally(min_reclaim_pct=1.0)  # needs close >= 101.00 (PDL=100)
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
    """A 1-cent undercut is chop, not a setup, at a 1%-of-level threshold."""
    s = UndercutRally(min_excursion_pct=1.0)  # needs >= $1.00 of undercut on PDL=100
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
    s = UndercutRally(min_reclaim_pct=1.0)  # needs close >= $1.00 above PDL (=100)
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
    s = OvershootFail(min_excursion_pct=1.0, min_reclaim_pct=1.0)
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
    # excursion_adr/reclaim_adr are recorded as diagnostics regardless of the
    # (now percent-only) filter -- levels.adr is still available here.
    check("excursion adr", t.extras["excursion_adr"], 0.2)
    check("reclaim adr", t.extras["reclaim_adr"], 0.15)


def test_filters_work_without_adr() -> None:
    """Filtering is percent-only now, so it never depends on ADR being
    available -- only the excursion_adr/reclaim_adr diagnostics do."""
    s = UndercutRally(min_excursion_pct=0.5, min_reclaim_pct=0.5)
    s.on_bar(bar(0, 101, 101, 99, 99.5), LEVELS)  # LEVELS has adr_pct 0, PDL=100
    t = s.on_bar(bar(1, 99.5, 101, 99.5, 100.5), LEVELS)
    assert t is not None, "percent filter must not depend on ADR availability"
    check("adr diagnostic unavailable", t.extras["excursion_adr"], None)


def test_excursion_pct_catches_aeva() -> None:
    """AEVA's real U&R on 2026-08-14: PDL=22.25, undercut bottomed at 22.05, a
    $0.20 breach -- 0.8989% of level, clears a 0.5% floor comfortably. This is
    the case that originally motivated a flat-percent excursion measure
    (ADR alone missed it by five cents) before ADR was dropped from filtering
    entirely.
    """
    aeva = DailyLevels(
        "AEVA",
        date(2026, 8, 13),
        pdh=25.86,
        pdl=22.25,
        adr_pct=11.00,
        prev_close=22.42,
        adr_sessions=14,
    )
    s = UndercutRally(min_excursion_pct=0.5)
    s.on_bar(bar(0, 22.35, 22.40, 22.05, 22.27), aeva)  # arm, low=22.05
    t = s.on_bar(bar(1, 22.20, 22.35, 22.10, 22.30), aeva)  # close 22.30
    assert t is not None, "excursion clears the flat-percent floor"
    check("excursion_pct", t.extras["excursion_pct"], 0.8989)


def test_min_pct_passes_semantics() -> None:
    """Direct coverage of the percent-floor helper's degenerate cases."""
    from astor.core.setups import _min_pct_passes

    ok, pct = _min_pct_passes(amount=2.0, level_value=100.0, min_pct=1.0)
    check("clears the floor", ok, True)
    check("pct computed", pct, 2.0)

    ok2, _ = _min_pct_passes(amount=0.5, level_value=100.0, min_pct=1.0)
    check("short of the floor rejects", ok2, False)

    # min_pct <= 0 disables the check -- always passes, pct still reported.
    ok3, pct3 = _min_pct_passes(amount=0.0001, level_value=100.0, min_pct=0.0)
    check("no threshold configured means no filtering at all", ok3, True)
    check("pct is still reported even when unused as a filter", pct3, 0.0001)

    # level_value <= 0 means pct can't be computed; a configured floor fails
    # closed rather than silently passing.
    ok4, pct4 = _min_pct_passes(amount=1.0, level_value=0.0, min_pct=1.0)
    check("unresolvable pct must not auto-pass a configured floor", ok4, False)
    check("pct is None when the level is unusable", pct4, None)


def test_combined_pct_filter_rejects_two_weak_halves() -> None:
    """Excursion and reclaim can each clear their own floor while the whole
    move is still small -- the combined floor catches that pair."""
    s = UndercutRally(min_excursion_pct=0.6, min_reclaim_pct=0.6, min_combined_pct=1.5)
    s.on_bar(bar(0, 100.6, 100.6, 99.35, 99.5), LEVELS)  # undercut_low=99.35
    # excursion 0.65%, reclaim 0.65% -- each clears its own floor, sum 1.3%
    # doesn't clear 1.5%.
    t = s.on_bar(bar(1, 99.5, 100.65, 99.4, 100.65), LEVELS)
    check("both halves pass individually but combined is too small", t, None)
    # A deeper reclaim pushes the sum over 1.5% without changing excursion.
    t2 = s.on_bar(bar(2, 100.65, 101.0, 100.6, 100.9), LEVELS)
    assert t2 is not None, "combined floor should now clear"
    check("combined_pct", t2.extras["combined_pct"], 1.55)


def test_reclaim_pct_recorded() -> None:
    """reclaim_pct is a diagnostic, always recorded -- not gated by any filter,
    same as excursion_pct."""
    s = UndercutRally()
    s.on_bar(bar(0, 101, 101, 99, 99.5), LEVELS)  # arm, PDL=100
    t = s.on_bar(bar(1, 99.5, 101, 99.5, 100.5), LEVELS)  # close 0.5 above PDL
    assert t is not None
    check("reclaim_pct = 0.5/100*100", t.extras["reclaim_pct"], 0.5)

    o = OvershootFail()
    o.on_bar(bar(0, 104, 106, 104, 105.5), LEVELS)  # arm, PDH=105
    t2 = o.on_bar(bar(1, 105.5, 105.5, 104.475, 104.475), LEVELS)  # 0.525 below PDH
    assert t2 is not None
    check("reclaim_pct = 0.525/105*100", t2.extras["reclaim_pct"], 0.5)


def test_session_indicators_avwap() -> None:
    from astor.core.indicators import SessionIndicators

    ind = SessionIndicators("TEST")
    check("no avwap before any bar", ind.avwap, None)

    ind.update(bar(0, 100, 102, 98, 100))  # typical=(102+98+100)/3=100, vol=1000
    check("first bar's avwap is just its typical price", ind.avwap, 100.0)
    check("first_bar_start is the first bar's start", ind.first_bar_start, OPEN)

    ind.update(bar(1, 100, 104, 100, 102, v=2000))  # typical=102, vol=2000
    # (100*1000 + 102*2000) / 3000 = 304000 / 3000
    assert abs(ind.avwap - 101.3333) < 0.0001, f"avwap={ind.avwap}"
    check("first_bar_start doesn't move on later bars", ind.first_bar_start, OPEN)


def test_compute_rvol() -> None:
    from astor.core.indicators import compute_rvol
    from astor.core.types import VolumeCurve

    curve = VolumeCurve(
        "TEST", sessions=5, cum_volume_by_minute={time(9, 30): 1000.0, time(9, 31): 2500.0}
    )
    b0 = bar(0, 100, 101, 99, 100)  # start 09:30
    check("double the average rounds to 2.00", compute_rvol(2000.0, b0, curve), 2.00)

    b1 = bar(1, 100, 101, 99, 100)  # start 09:31
    check("half the average rounds to 0.50", compute_rvol(1250.0, b1, curve), 0.50)

    b2 = bar(2, 100, 101, 99, 100)  # start 09:32, not in the curve
    check("no curve coverage for this minute -> None", compute_rvol(500.0, b2, curve), None)

    zero_curve = VolumeCurve("TEST", sessions=0, cum_volume_by_minute={time(9, 30): 0.0})
    check("zero average -> None, not a division error", compute_rvol(500.0, b0, zero_curve), None)


def test_build_cum_volume_curve() -> None:
    from astor.drivers.replay import build_cum_volume_curve

    d1, d2 = date(2026, 8, 17), date(2026, 8, 18)

    def row(d: date, hh: int, mm: int, v: float) -> dict:
        return {"t": f"{d:%Y-%m-%d}T{hh:02d}:{mm:02d}:00-04:00", "v": v}

    curve, used = build_cum_volume_curve(
        {
            d1: [row(d1, 9, 30, 100), row(d1, 9, 31, 50)],
            d2: [row(d2, 9, 30, 200), row(d2, 9, 31, 100)],
        }
    )
    check("both sessions contributed", used, 2)
    check("09:30 average = (100+200)/2", curve[time(9, 30)], 150.0)
    check("09:31 cumulative average = (150+300)/2", curve[time(9, 31)], 225.0)

    # A session with no usable rows doesn't count, and a minute only some
    # sessions have bars for is averaged over just those.
    curve2, used2 = build_cum_volume_curve(
        {
            d1: [row(d1, 9, 30, 100), row(d1, 9, 31, 50)],
            d2: [row(d2, 9, 30, 200)],  # missing 09:31
            date(2026, 8, 19): [],  # no usable bars at all
        }
    )
    check("only sessions with usable rows count", used2, 2)
    check("09:30 averaged over both", curve2[time(9, 30)], 150.0)
    check("09:31 averaged only over the session that has it", curve2[time(9, 31)], 150.0)


def test_scanner_backfill_bar_updates_indicators_not_setups() -> None:
    sc = Scanner({"TEST": LEVELS}, [UndercutRally])

    b0 = bar(0, 101, 101, 99, 99.5)
    sc.backfill_bar(b0)
    check("indicators updated by backfill", sc.indicators["TEST"].cum_volume, b0.volume)
    check("no setup state created by backfill", sc._state, {})
    check("no triggers recorded by backfill", sc.triggers, [])

    # Setup state starts clean: on_bar sees this as the FIRST bar it has ever
    # evaluated for TEST, so it only arms (this bar's low is itself below
    # PDL) -- it can't fire, because it never saw the backfilled undercut.
    b1 = bar(1, 99.5, 101, 99.5, 100.5)
    fired = sc.on_bar(b1)
    check("setup starts clean -- this bar only arms it, can't fire yet", fired, [])
    check("but indicators keep accumulating across backfill and live bars",
          sc.indicators["TEST"].cum_volume, b0.volume + b1.volume)


def test_trigger_extras_include_avwap_and_rvol() -> None:
    """Scanner wires its running indicators/curve into Setup.on_bar, so every
    trigger carries the current AVWAP and RVOL as diagnostics -- no setup
    filters on them yet, but they're useful context for all of them."""
    from astor.core.types import VolumeCurve

    curve = VolumeCurve("TEST", sessions=5, cum_volume_by_minute={time(9, 31): 500.0})
    sc = Scanner({"TEST": LEVELS}, [UndercutRally])
    sc.curves["TEST"] = curve

    sc.on_bar(bar(0, 101, 101, 99, 99.5))  # arm, also seeds indicators
    fired = sc.on_bar(bar(1, 99.5, 101, 99.5, 100.5))  # fire
    assert fired, "expected a trigger"
    t = fired[0]
    assert t.extras["avwap"] is not None, "avwap should be populated from indicators"
    check("rvol = cum_volume(2000) / curve avg at 09:31(500)", t.extras["rvol"], 4.0)


def test_setup_on_bar_defaults_indicators_and_curve_to_none() -> None:
    """Direct on_bar calls (as every other test in this file makes) must keep
    working without indicators/curve -- Scanner is the only caller expected
    to supply them."""
    s = UndercutRally()
    s.on_bar(bar(0, 101, 101, 99, 99.5), LEVELS)
    t = s.on_bar(bar(1, 99.5, 101, 99.5, 100.5), LEVELS)
    assert t is not None
    check("avwap is None without indicators", t.extras["avwap"], None)
    check("rvol is None without indicators/curve", t.extras["rvol"], None)


def test_latest_triggers_spans_sessions_and_supersedes_reruns() -> None:
    """latest_triggers is keyed on session, not global recency.

    Two different sessions (Friday, then today) must both appear -- that is
    the whole point of covering "more than one run". But a second replay of
    the SAME session (a threshold rerun) must supersede the first, not add to
    it, so the view never shows two conflicting versions of one session's
    triggers side by side. trigger_detail keeps the full, undeduped history
    for anyone who wants to audit an old run explicitly by run_id.
    """
    import tempfile

    from astor.core.store import TriggerStore
    from astor.core.types import Trigger

    store = TriggerStore(Path(tempfile.mkdtemp()) / "t.db")

    def record_run(run_id: str, session: str, price: float) -> None:
        # The real replay driver always keeps bar dates matching the run's own
        # --date (fetch_bars skips any bar whose date differs), so bar_start
        # and the run's params session never disagree in practice. Deriving
        # bar_start from `session` here keeps the fixture honest to that.
        session_open = datetime.fromisoformat(f"{session}T09:30:00").replace(tzinfo=ET)
        store.start_run(run_id, "replay", {"session": session})
        store.record(
            run_id,
            Trigger(
                symbol="TEST",
                setup="U&R",
                direction="long",
                trigger_price=price,
                bar_start=session_open,
                bar_end=session_open + timedelta(minutes=1),
                level_name="PDL",
                level_value=100.0,
                extras={"bars_below": 1, "bars_armed": 1, "excursion_adr": 0.2},
            ),
            session_open + timedelta(minutes=1),
        )
        store.commit()

    record_run("s1-a", "2026-08-14", 100.1)  # first attempt at Friday
    record_run("s2-a", "2026-08-17", 200.2)  # an unrelated later session
    record_run("s1-b", "2026-08-14", 100.9)  # rerun of Friday: supersedes s1-a

    rows = store.conn.execute(
        "SELECT run_id, session, trigger_price FROM latest_triggers ORDER BY session"
    ).fetchall()
    check("two distinct sessions, reruns deduped to one row each", len(rows), 2)
    by_session = {r[1]: (r[0], r[2]) for r in rows}
    check(
        "Friday shows the rerun, not the original",
        by_session["2026-08-14"],
        ("s1-b", 100.9),
    )
    check("today's single run shows too", by_session["2026-08-17"], ("s2-a", 200.2))

    total = store.conn.execute("SELECT count(*) FROM trigger_detail").fetchone()[0]
    check("trigger_detail retains the full history, undeduped", total, 3)
    store.close()


def test_latest_triggers_includes_live_runs() -> None:
    """latest_run_per_session must not filter by mode -- a 'live' run is a
    run of its session exactly like a 'replay' is. This is the regression
    test for a real bug: the view's WHERE mode='replay' silently excluded
    every live trigger from latest_triggers (visible only in trigger_detail)
    until the first live session found it in practice.
    """
    import tempfile

    from astor.core.store import TriggerStore
    from astor.core.types import Trigger

    store = TriggerStore(Path(tempfile.mkdtemp()) / "t.db")
    session_open = datetime.fromisoformat("2026-08-20T09:30:00").replace(tzinfo=ET)
    store.start_run("live-1", "live", {"session": "2026-08-20"})
    store.record(
        "live-1",
        Trigger(
            symbol="ABAT",
            setup="O&F",
            direction="short",
            trigger_price=2.40,
            bar_start=session_open,
            bar_end=session_open + timedelta(minutes=1),
            level_name="PDH",
            level_value=2.50,
            extras={"bars_above": 1, "bars_armed": 1},
        ),
        session_open + timedelta(minutes=21),
    )
    store.commit()

    rows = store.conn.execute(
        "SELECT run_id, symbol FROM latest_triggers WHERE session = '2026-08-20'"
    ).fetchall()
    check("the live run's trigger shows up in latest_triggers", rows, [("live-1", "ABAT")])
    store.close()


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
        extras={"bars_below": 3, "bars_armed": 4, "excursion_adr": 0.15},
    )
    store.record("r1", trig, trig.bar_end)
    store.commit()

    row = store.conn.execute(
        "SELECT session, trigger_time, bars_beyond, latency_sec FROM latest_triggers"
    ).fetchone()
    check("session date is ET", row[0], "2026-08-14")
    check("trigger time is ET, not UTC", row[1], "09:30:00")
    check("bars_beyond coalesced", row[2], 3)
    # Replay detects at bar completion, so latency is zero by construction.
    check("replay latency is zero", row[3], 0.0)
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
            extras={"bars_above": 2, "bars_armed": 3, "excursion_adr": 0.15},
        ),
        OPEN + timedelta(minutes=1),
    )
    store.commit()
    row = store.conn.execute(
        "SELECT bars_beyond, bars_armed, excursion_adr FROM latest_triggers"
    ).fetchone()
    check("short-side coalesce", row, (2, 3, 0.15))
    store.close()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
