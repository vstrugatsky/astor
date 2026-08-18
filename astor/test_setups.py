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


def test_excursion_pct_recovers_aeva() -> None:
    """The exact case that motivated the OR: AEVA's real U&R on 2026-08-14.

    PDL=22.25, ADR%=11.00 -> ADR$=2.4662. The undercut bottomed at 22.05, a
    $0.20 breach -- 0.081x ADR, under a 0.1x threshold by five cents. As a
    fraction of the level (0.20/22.25 = 0.899%) it clears a 0.5% floor easily.
    ADR-only misses it; ADR-or-0.5%-of-level catches it.
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
    adr_only = UndercutRally(min_excursion_adr=0.1, min_reclaim_adr=0.1)
    check(
        "ADR-only misses the real undercut",
        adr_only.on_bar(bar(0, 22.35, 22.40, 22.05, 22.27), aeva),
        None,
    )
    t = adr_only.on_bar(bar(1, 22.20, 22.35, 22.10, 22.30), aeva)
    check("ADR-only: still misses on the follow-through bar", t, None)

    both = UndercutRally(
        min_excursion_adr=0.1, min_excursion_pct=0.5, min_reclaim_adr=0.1
    )
    both.on_bar(bar(0, 22.35, 22.40, 22.05, 22.27), aeva)  # arm, low=22.05
    both.on_bar(bar(1, 22.20, 22.35, 22.10, 22.30), aeva)  # reclaim too weak yet
    both.on_bar(bar(2, 22.39, 22.47, 22.21, 22.46), aeva)  # reclaim still short
    t2 = both.on_bar(bar(3, 22.45, 22.69, 22.45, 22.57), aeva)  # reclaim clears 0.1x
    assert t2 is not None, (
        "the OR filter should catch the same undercut ADR-only missed"
    )
    check(
        "excursion_adr still fails on its own", t2.extras["excursion_adr"] < 0.1, True
    )
    check("excursion_pct is what let it through", t2.extras["excursion_pct"], 0.8989)


def test_excursion_or_semantics() -> None:
    """Direct coverage of the OR helper's degenerate cases."""
    from astor.core.setups import _excursion_passes

    # Both configured, ADR passes, pct fails -> OR passes.
    ok, pct = _excursion_passes(
        excursion=2.0,
        level_value=100.0,
        adr=10.0,
        min_excursion_adr=0.1,
        min_excursion_pct=5.0,
    )
    check("adr side alone can carry the OR", ok, True)
    check("pct computed regardless of which side passed", pct, 2.0)

    # Both configured, ADR fails, pct passes -> OR passes (the AEVA shape).
    ok2, _ = _excursion_passes(
        excursion=1.0,
        level_value=100.0,
        adr=100.0,
        min_excursion_adr=0.5,
        min_excursion_pct=0.5,
    )
    check("pct side alone can carry the OR", ok2, True)

    # Both configured, both fail -> rejected.
    ok3, _ = _excursion_passes(
        excursion=0.1,
        level_value=100.0,
        adr=100.0,
        min_excursion_adr=0.5,
        min_excursion_pct=5.0,
    )
    check("both failing rejects", ok3, False)

    # Both configured, ADR unscalable (adr=0) -> degrades to pct-only, not
    # auto-pass. This is the case that distinguishes "drop from the OR" from
    # "count as a pass": if unscalable auto-passed, a bad pct would slip through.
    ok4, _ = _excursion_passes(
        excursion=0.1,
        level_value=100.0,
        adr=0.0,
        min_excursion_adr=0.5,
        min_excursion_pct=5.0,
    )
    check("unscalable ADR must not auto-pass when pct also fails", ok4, False)

    ok5, _ = _excursion_passes(
        excursion=1.0,
        level_value=100.0,
        adr=0.0,
        min_excursion_adr=0.5,
        min_excursion_pct=0.5,
    )
    check("unscalable ADR degrades cleanly to a real pct check", ok5, True)

    # Neither configured -> filter fully off, matches pre-existing behaviour.
    ok6, pct6 = _excursion_passes(
        excursion=0.0001,
        level_value=100.0,
        adr=100.0,
        min_excursion_adr=0.0,
        min_excursion_pct=0.0,
    )
    check("no thresholds configured means no filtering at all", ok6, True)
    check("excursion_pct is still reported even when unused as a filter", pct6, 0.0001)


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
