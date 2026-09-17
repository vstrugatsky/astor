"""Setup and scanner logic, verified against hand-built bars.

uv run python -m astor.test_setups
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astor.core.scanner import Scanner
from astor.core.setups import ReversalLong, ReversalShort
from astor.core.types import BarEvent, DailyLevels, VolumeCurve
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


def test_scanner_drops_zero_volume() -> None:
    sc = Scanner({"TEST": LEVELS}, [ReversalLong])
    sc.on_bar(bar(0, 101, 101, 99, 99.5))  # real volume, seeds indicators/leg state
    # A padded zero-volume bar carrying price above the session low must not
    # be evaluated at all.
    check("zero-volume dropped", sc.on_bar(bar(1, 100.5, 100.5, 100.5, 100.5, v=0)), [])
    check("counted as skipped", sc.skipped_zero_volume, 1)
    check("not evaluated", sc.bars_evaluated, 1)
    check(
        "zero-volume bar never touched indicators",
        sc.indicators["TEST"].cum_volume,
        1000,
    )
    # A real bar at the same price is evaluated (bars_evaluated increments),
    # proving it was the volume filter that suppressed the phantom bar, not
    # some Setup/level issue.
    sc.on_bar(bar(2, 100.5, 100.6, 100.4, 100.5))
    check("real bar was evaluated", sc.bars_evaluated, 2)


def test_scanner_skips_symbols_without_levels() -> None:
    sc = Scanner({"TEST": LEVELS}, [ReversalLong])
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


def _canonical_long_bars() -> list[BarEvent]:
    """A REV-L leg built to clear every default Reversal filter at once:
    anchor pinned at bar 0's high (98.0, session open), leg deepens to an
    extreme of 93.0 at minute 4 (leg=5.0 >= 1.0x the $4.00 ADR, built over
    exactly the 4-minute floor), then a bar 5 retrace to 94.0 (20% of the
    leg, comfortably in the 10-50% band) with a wide-open default volume
    curve so move_rvol trivially clears the 2.0x floor for this ADV tier.
    Bar 1's close (97.5, a small hop from bar 0's 97.8) is deliberately
    close to bar 0's, not bar 4's own pace -- accel_price only reads
    earlier=[bar0, bar1] and recent_end=extreme_close (bar 4), so a small
    early hop next to the leg's real (much bigger) net move over the last
    3 bars clears the 1.2 accel_price floor; bars 2-3's own closes are
    otherwise unconstrained (accel_price never reads them directly).
    Reused across every generic (non-Reversal-specific) test that just
    needs *a* trigger to fire.
    """
    return [
        bar(0, 98.0, 98.0, 97.5, 97.8, 50_000, symbol="REV"),
        bar(1, 97.8, 97.9, 96.0, 97.5, 50_000, symbol="REV"),
        bar(2, 96.3, 96.4, 95.0, 95.3, 50_000, symbol="REV"),
        bar(3, 95.3, 95.4, 94.0, 94.3, 50_000, symbol="REV"),
        bar(4, 94.3, 94.4, 93.0, 93.3, 50_000, symbol="REV"),  # extreme, minute 4
        bar(5, 93.3, 93.8, 93.1, 94.0, 10_000, symbol="REV"),  # retrace 20%
    ]


def _canonical_short_bars() -> list[BarEvent]:
    """Mirror of _canonical_long_bars for REV-S: anchor pinned at bar 0's
    low (102.0), leg extends to 107.7 at minute 4 (leg=5.7), retrace to
    106.5 at minute 5 (21% of the leg). Bar 1's close (102.5, a small hop
    from bar 0's 102.2) mirrors _canonical_long_bars' own accel_price
    setup -- see its docstring."""
    return [
        bar(0, 102.0, 102.0, 102.0, 102.2, 50_000, symbol="REV"),
        bar(1, 102.2, 104.0, 102.1, 102.5, 50_000, symbol="REV"),
        bar(2, 103.8, 105.5, 103.5, 105.2, 50_000, symbol="REV"),
        bar(3, 105.2, 106.7, 105.0, 106.4, 50_000, symbol="REV"),
        bar(4, 106.4, 107.7, 106.2, 107.4, 50_000, symbol="REV"),  # extreme, minute 4
        bar(5, 107.4, 107.6, 106.4, 106.5, 10_000, symbol="REV"),  # retrace 21%
    ]


def _generous_curve(symbol: str = "REV", n: int = 60) -> VolumeCurve:
    """A curve whose average is tiny at every minute, so any real bar volume
    trivially clears the RVOL floor -- for tests where RVOL isn't the point."""
    return VolumeCurve(
        symbol, sessions=10,
        cum_volume_by_minute={(OPEN + timedelta(minutes=i)).timetz().replace(tzinfo=None): 1.0 for i in range(n)},
    )


def _rev_scanner(
    setup_cls, symbol: str = "REV", curve: VolumeCurve | None = None, setup_kwargs: dict | None = None
) -> Scanner:
    levels = DailyLevels(
        symbol, date(2026, 8, 13), 0.0, 0.0, adr_pct=4.0, prev_close=100.0, adv=100_000_000.0
    )
    kwargs = {setup_cls.name: setup_kwargs} if setup_kwargs else None
    scanner = Scanner({symbol: levels}, [setup_cls], kwargs)
    scanner.curves[symbol] = curve or _generous_curve(symbol)
    return scanner


def _feed(scanner: Scanner, bars: list[BarEvent]) -> list:
    triggers = []
    for b in bars:
        triggers.extend(scanner.on_bar(b))
    return triggers


def test_run_replay_end_to_end() -> None:
    sc = _rev_scanner(ReversalLong)
    triggers, alerts = [], []
    fed = run_replay(
        sc, {"REV": _canonical_long_bars()}, on_trigger=triggers.append,
        on_capitulation_alert=alerts.append,
    )
    check("bars fed", fed, 6)
    check("one trigger", len(triggers), 1)
    check("one capitulation alert (leg still extending at minute 4)", len(alerts), 1)
    check(
        "detection ts is bar completion",
        triggers[0].bar_end,
        triggers[0].bar_start + timedelta(minutes=1),
    )


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


def test_compute_adv() -> None:
    """Average Dollar Volume: mean of (close * volume) over the last N
    sessions, in dollars -- feeds grade_trigger's rvol tiers."""
    from astor.drivers.replay import compute_adv

    class B:
        def __init__(self, close, volume):
            self.close, self.volume = close, volume

    # $10 * 1M and $20 * 2M -> mean $25M.
    adv, used = compute_adv([B(10, 1_000_000), B(20, 2_000_000)], 2)
    check("adv dollars", adv, 25_000_000.0)
    check("sessions used", used, 2)

    # Only the last N sessions count.
    adv2, used2 = compute_adv([B(1000, 1_000_000), B(10, 1_000_000)], 1)
    check("window respected", adv2, 10_000_000.0)
    check("sessions used", used2, 1)

    # A zero-volume bar is skipped, not a division error; no usable bars -> 0.0.
    adv3, used3 = compute_adv([B(10, 0), B(20, 1_000_000)], 2)
    check("zero-volume skipped", adv3, 20_000_000.0)
    check("count excludes bad bar", used3, 1)
    check("no usable bars", compute_adv([B(10, 0)], 2), (0.0, 0))


def test_next_trading_session() -> None:
    """Before today's close -> today; at/after close -> the next weekday,
    skipping Sat/Sun. Shared by LiveDriver's own session anchor and
    warm-cache's default --date."""
    from astor.drivers.replay import next_trading_session

    # Wednesday mid-session -> targets today.
    check(
        "mid-session -> today",
        next_trading_session(datetime(2026, 8, 19, 10, 0, tzinfo=ET)),
        date(2026, 8, 19),
    )
    # Wednesday after the close -> Thursday.
    check(
        "after close -> next weekday",
        next_trading_session(datetime(2026, 8, 19, 17, 0, tzinfo=ET)),
        date(2026, 8, 20),
    )
    # Friday after the close -> Monday, not Saturday.
    check(
        "Friday after close -> Monday",
        next_trading_session(datetime(2026, 8, 21, 17, 0, tzinfo=ET)),
        date(2026, 8, 24),
    )
    # Sunday (e.g. a night-before-prep run) -> Monday.
    check(
        "Sunday -> Monday",
        next_trading_session(datetime(2026, 8, 23, 20, 0, tzinfo=ET)),
        date(2026, 8, 24),
    )


def test_bar_cache_prune() -> None:
    """Entries older than retention_days are deleted, except a date that
    was ever an actual replay/live session (protected forever regardless
    of age); anything within the retention window survives either way."""
    import tempfile

    from astor.drivers.replay import BarCache

    cache = BarCache(Path(tempfile.mkdtemp()))
    today = date(2026, 8, 27)

    old_unprotected = date(2026, 7, 1)  # 57 days old, never a real session
    old_protected = date(2026, 7, 2)  # equally old, but was a real session
    recent = date(2026, 8, 20)  # 7 days old, within the retention window

    for kind in ("levels", "curve", "intraday"):
        cache.save(kind, "AAA", old_unprotected, "1 min", [1])
        cache.save(kind, "BBB", old_protected, "1 min", [1])
        cache.save(kind, "CCC", recent, "1 min", [1])

    removed = cache.prune(
        protected_sessions={old_protected.isoformat()}, retention_days=30, today=today
    )
    check("one entry removed per kind", removed, {"levels": 1, "curve": 1, "intraday": 1})

    check(
        "old + unprotected is gone",
        cache.load("levels", "AAA", old_unprotected, "1 min"),
        None,
    )
    check(
        "old + protected survives",
        cache.load("levels", "BBB", old_protected, "1 min"),
        [1],
    )
    check(
        "recent survives regardless of protection",
        cache.load("levels", "CCC", recent, "1 min"),
        [1],
    )


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


# adr_pct=4.0 on a $100 prev_close -> $4.00 of ADR -- matches the canonical
# REV-L/REV-S fixtures below (_canonical_long_bars/_canonical_short_bars),
# which are sized against a $4.00 ADR, not ADR_LEVELS's $10.00.
REV_LEVELS = DailyLevels(
    "REV", date(2026, 8, 13), 0.0, 0.0, adr_pct=4.0, prev_close=100.0, adv=100_000_000.0
)


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


def test_session_indicators_ema9() -> None:
    """ema9 is seeded from the first bar's close (no SMA warmup) and decays
    with alpha=2/(9+1)=0.2 thereafter. ema9_at_high/low freeze the reading
    at the same bar session_high/session_low themselves were set -- the
    same relationship cum_volume_at_high/low already has."""
    from astor.core.indicators import SessionIndicators

    ind = SessionIndicators("TEST")
    ind.update(bar(0, 100, 101, 99, 100))  # close=100 -> seeds ema9
    check("ema9 seeded from the first close", ind.ema9, 100.0)
    check("ema9_at_low frozen at the seeding bar (also the first low)", ind.ema9_at_low, 100.0)
    check("ema9_at_high frozen at the seeding bar (also the first high)", ind.ema9_at_high, 100.0)

    ind.update(bar(1, 100, 100.5, 98.5, 95))  # new low (98.5 < 99), close=95
    # ema9 = 100 + 0.2*(95-100) = 99.0
    check("ema9 decays toward the new close", round(ind.ema9, 4), 99.0)
    check("ema9_at_low updates -- a new session low was just set", ind.ema9_at_low, 99.0)
    check("ema9_at_high stays at the earlier freeze -- no new session high", ind.ema9_at_high, 100.0)

    ind.update(bar(2, 95, 96, 99.0, 96))  # low=99.0 is not below 98.5 -- no new low
    # ema9 = 99.0 + 0.2*(96-99.0) = 98.4
    check("ema9 keeps updating on every bar", round(ind.ema9, 4), 98.4)
    check("ema9_at_low unaffected -- no new session low this bar", ind.ema9_at_low, 99.0)


def test_compute_rvol() -> None:
    from astor.core.indicators import compute_rvol
    from astor.core.types import VolumeCurve

    curve = VolumeCurve(
        "TEST", sessions=5, cum_volume_by_minute={time(9, 30): 1000.0, time(9, 31): 2500.0}
    )
    b0 = bar(0, 100, 101, 99, 100)  # start 09:30
    check("double the average rounds to 2.00", compute_rvol(2000.0, b0.start, curve), 2.00)

    b1 = bar(1, 100, 101, 99, 100)  # start 09:31
    check("half the average rounds to 0.50", compute_rvol(1250.0, b1.start, curve), 0.50)

    b2 = bar(2, 100, 101, 99, 100)  # start 09:32, not in the curve
    check("no curve coverage for this minute -> None", compute_rvol(500.0, b2.start, curve), None)

    zero_curve = VolumeCurve("TEST", sessions=0, cum_volume_by_minute={time(9, 30): 0.0})
    check(
        "zero average -> None, not a division error",
        compute_rvol(500.0, b0.start, zero_curve),
        None,
    )


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
    sc = Scanner({"TEST": LEVELS}, [ReversalLong])

    b0 = bar(0, 101, 101, 99, 99.5)
    sc.backfill_bar(b0)
    check("indicators updated by backfill", sc.indicators["TEST"].cum_volume, b0.volume)
    check("no setup state created by backfill", sc._state, {})
    check("no triggers recorded by backfill", sc.triggers, [])

    # Setup state starts clean: on_bar sees this as the FIRST bar it has ever
    # evaluated for TEST, so both extreme and anchor "move" (they were None)
    # -- it can only arm a fresh leg, never fire off a push it never saw form.
    b1 = bar(1, 99.5, 100, 99.4, 99.8)
    fired = sc.on_bar(b1)
    check("setup starts clean -- this bar only arms it, can't fire yet", fired, [])
    check("but indicators keep accumulating across backfill and live bars",
          sc.indicators["TEST"].cum_volume, b0.volume + b1.volume)


def test_trigger_extras_include_avwap_and_rvol() -> None:
    """Scanner wires its running indicators/curve into Setup.on_bar, so every
    trigger carries the current AVWAP and RVOL as diagnostics."""
    sc = _rev_scanner(ReversalLong)
    fired = _feed(sc, _canonical_long_bars())
    assert fired, "expected a trigger"
    t = fired[0]
    assert t.extras["avwap"] is not None, "avwap should be populated from indicators"
    assert t.extras["rvol"] is not None, "rvol should be populated from the curve"


def test_position_size() -> None:
    from astor.core.setups import _position_size

    shares, dollars = _position_size(entry=100.0, stop=98.0, max_loss=100.0)
    check("shares = floor(100 / 2)", shares, 50)
    check("dollars = 50 * entry", dollars, 5000.0)

    check("zero risk per share -> no size", _position_size(100.0, 100.0, 100.0), (None, None))
    check(
        "risk too wide for even one share -> no size",
        _position_size(100.0, 50.0, 10.0),
        (None, None),
    )
    check("non-positive max_loss -> no size", _position_size(100.0, 98.0, 0.0), (None, None))


def test_trigger_extras_include_notify_fields() -> None:
    """avwap_pct and the level stop/position-size pair are computed the same
    way avwap/rvol are -- from Scanner-supplied indicators, recorded but
    never filtered on. Unlike the retired U&R/O&F, Reversal has no second,
    distinct "technical vs. actual" stop to show -- the level *is* the
    session extreme already -- so session_extreme/session_stop_* are left
    unset entirely rather than duplicating level_stop_*.
    """
    from astor.core.indicators import SessionIndicators

    ind = SessionIndicators("REV")
    s = ReversalLong(max_loss_dollars=100.0)
    curve = _generous_curve()

    t = None
    for b in _canonical_long_bars():
        ind.update(b)
        r = s.on_bar(b, REV_LEVELS, ind, curve)
        if r is not None:
            t = r
    assert t is not None, "expected a fire"

    check("avwap_pct is populated", t.extras["avwap_pct"] is not None, True)
    # entry=94.0 vs the leg extreme=93.0 -> abs((94-93)/93*100)
    check("level_stop_pct", t.extras["level_stop_pct"], 1.0753)
    check("max_loss_dollars recorded", t.extras["max_loss_dollars"], 100.0)
    check("session_open recorded", t.extras["session_open"], 98.0)
    check("session_high recorded", t.extras["session_high"], 98.0)
    check("session_low recorded", t.extras["session_low"], 93.0)
    check("session_extreme is not recorded for Reversal", "session_extreme" in t.extras, False)
    check(
        "session_stop_shares is not recorded for Reversal",
        "session_stop_shares" in t.extras,
        False,
    )


def test_notify_pct_fields_are_unsigned_for_short_setups() -> None:
    """level_stop_pct is a stop's distance -- a trader thinks of that as a
    positive number regardless of direction, unlike avwap_pct, which stays
    signed since above/below AVWAP is meaningfully directional."""
    from astor.core.indicators import SessionIndicators

    ind = SessionIndicators("REV")
    s = ReversalShort(max_loss_dollars=100.0)
    curve = _generous_curve()

    t = None
    for b in _canonical_short_bars():
        ind.update(b)
        r = s.on_bar(b, REV_LEVELS, ind, curve)
        if r is not None:
            t = r
    assert t is not None, "expected a fire"

    # entry=106.5 vs the leg extreme=107.7 -> abs((106.5-107.7)/107.7*100)
    check("level_stop_pct is a positive magnitude", t.extras["level_stop_pct"], 1.1142)
    check("session_open recorded", t.extras["session_open"], 102.0)
    check("session_high recorded", t.extras["session_high"], 107.7)
    check("session_low recorded", t.extras["session_low"], 102.0)


def _grading_fixture(**overrides):
    """A long (REV-L) trigger with every grade_trigger factor set to *miss*:
    price below session_open, rvol below every tier's threshold, no AVWAP
    extension (avwap unset), fired at minute 200 (outside both the early
    window and the last hour). Grade should come out to the base of 1.
    Override kwargs to flip individual factors on. adr_pct/prev_close
    default to a clean $10.00 ADR (10% of a $100 prev_close) so AVWAP-
    extension tests can work in round ADR units."""
    from astor.core.indicators import SessionIndicators
    from astor.core.types import Trigger

    minute = overrides.pop("minute", 200)
    levels = DailyLevels(
        "TEST",
        date(2026, 8, 13),
        pdh=105.0,
        pdl=100.0,
        prev_close=overrides.pop("prev_close", 100.0),
        adr_pct=overrides.pop("adr_pct", 10.0),
        adv=overrides.pop("adv", 1_000_000.0),  # smallest tier: rvol > 1.5
    )
    ind = SessionIndicators("TEST")
    ind.session_open = overrides.pop("session_open", 102.0)
    extras = {"rvol": overrides.pop("rvol", 1.0)}
    avwap = overrides.pop("avwap", None)
    if avwap is not None:
        extras["avwap"] = avwap
    direction = overrides.pop("direction", "long")
    trigger = Trigger(
        symbol="TEST",
        setup="REV-L" if direction == "long" else "REV-S",
        direction=direction,
        trigger_price=overrides.pop("price", 101.0),
        bar_start=OPEN + timedelta(minutes=minute),
        bar_end=OPEN + timedelta(minutes=minute + 1),
        level_name="EXT",
        level_value=100.0,
        extras=extras,
    )
    assert not overrides, f"unused overrides: {overrides}"
    return trigger, levels, ind


def test_grade_trigger_base_case_is_one() -> None:
    """Every factor missing -> the base grade, never lower."""
    from astor.core.grading import grade_trigger

    t, levels, ind = _grading_fixture()
    check("all factors miss -> base grade", grade_trigger(t, levels, ind), 1)


def test_grade_trigger_adds() -> None:
    """Each add-1 factor, applied alone against the all-miss baseline,
    bumps the grade from 1 to 2."""
    from astor.core.grading import grade_trigger

    t, levels, ind = _grading_fixture(price=103.0)  # > open (102)
    check("price beyond open", grade_trigger(t, levels, ind), 2)

    t, levels, ind = _grading_fixture(rvol=1.6)  # > smallest-tier threshold (1.5)
    check("rvol beyond threshold", grade_trigger(t, levels, ind), 2)

    t, levels, ind = _grading_fixture(minute=15)  # start of the early window
    check("early window, lower bound", grade_trigger(t, levels, ind), 2)

    t, levels, ind = _grading_fixture(minute=90)  # end of the early window
    check("early window, upper bound", grade_trigger(t, levels, ind), 2)

    t, levels, ind = _grading_fixture(minute=91)  # just past the window
    check("just past the early window -> no add", grade_trigger(t, levels, ind), 1)

    t, levels, ind = _grading_fixture(minute=14)  # just before the window
    check("just before the early window -> no add", grade_trigger(t, levels, ind), 1)


def test_grade_trigger_subtracts() -> None:
    from astor.core.grading import grade_trigger

    # Push the base up first (price beyond open) so the last-hour penalty is
    # visible instead of clamped away.
    t, levels, ind = _grading_fixture(price=103.0, minute=330)  # 15:00 ET, last hour starts
    check("last hour, lower bound", grade_trigger(t, levels, ind), 1)

    t, levels, ind = _grading_fixture(price=103.0, minute=329)
    check("just before the last hour -> no subtract", grade_trigger(t, levels, ind), 2)


def test_grade_trigger_rvol_tiers_scale_with_adv() -> None:
    """The same rvol clears a higher-ADV name's lower threshold but misses a
    lower-ADV name's higher one."""
    from astor.core.grading import grade_trigger

    t, levels, ind = _grading_fixture(rvol=1.2, adv=6_000_000_000.0)  # > 5B tier: 1.15
    check("rvol clears the > 5B ADV tier", grade_trigger(t, levels, ind), 2)

    t, levels, ind = _grading_fixture(rvol=1.2, adv=600_000_000.0)  # > 500M tier: 1.3
    check("same rvol misses the > 500M ADV tier", grade_trigger(t, levels, ind), 1)

    t, levels, ind = _grading_fixture(rvol=1.2, adv=100_000_000.0)  # default tier: 1.5
    check("same rvol misses the default tier", grade_trigger(t, levels, ind), 1)


def test_grade_trigger_avwap_extension_penalty() -> None:
    """Price already extended too far from AVWAP in the setup's own
    direction, in ADR units: >0.5 ADR subtracts 2, >0.3 (but <=0.5) ADR
    subtracts 1, and extension in the opposite (safe) direction never
    penalizes -- ADR here is $10.00 (10% of a $100 prev_close, see
    _grading_fixture)."""
    from astor.core.grading import grade_trigger

    # price=101 (default, open-add misses), avwap=95 -> extension = 6 = 0.6 ADR -> > 0.5.
    t, levels, ind = _grading_fixture(avwap=95.0)
    check("long, >0.5 ADR extended -> -2", grade_trigger(t, levels, ind), 1)  # 1 - 2, clamped

    # price=103 (open-add fires, +1), avwap=99 -> extension = 4 = 0.4 ADR -> between 0.3 and 0.5.
    t, levels, ind = _grading_fixture(price=103.0, avwap=99.0)
    check("long, 0.3-0.5 ADR extended -> -1", grade_trigger(t, levels, ind), 1)  # 1 + 1 - 1

    # Exactly 0.5 ADR (avwap=98 -> extension=5) falls in the -1 tier, not -2:
    # the rule is "more than" 0.5, not "at least".
    t, levels, ind = _grading_fixture(price=103.0, avwap=98.0)
    check("long, exactly 0.5 ADR -> -1, not -2", grade_trigger(t, levels, ind), 1)  # 1 + 1 - 1

    # Exactly 0.3 ADR (avwap=100 -> extension=3) is not "more than" 0.3 either.
    t, levels, ind = _grading_fixture(price=103.0, avwap=100.0)
    check("long, exactly 0.3 ADR -> no penalty", grade_trigger(t, levels, ind), 2)  # 1 + 1

    # avwap above price -> extension negative (price hasn't run up yet) -> no penalty.
    t, levels, ind = _grading_fixture(price=103.0, avwap=110.0)
    check("long, avwap above price -> no penalty", grade_trigger(t, levels, ind), 2)  # 1 + 1

    # Mirrored for a short: extension is avwap - price. session_open=90 keeps
    # the open-add isolated out (101 is not below 90), same as price=101's
    # default open-miss on the long side above.
    t, levels, ind = _grading_fixture(
        direction="short", price=101.0, session_open=90.0, avwap=107.0
    )
    check("short, >0.5 ADR extended -> -2", grade_trigger(t, levels, ind), 1)  # 1 - 2, clamped

    t, levels, ind = _grading_fixture(
        direction="short", price=101.0, session_open=90.0, avwap=95.0
    )
    check("short, avwap below price -> no penalty", grade_trigger(t, levels, ind), 1)


def test_grade_trigger_max_grade_is_four() -> None:
    from astor.core.grading import grade_trigger

    # All three add factors at once -> exactly the ceiling.
    t, levels, ind = _grading_fixture(price=103.0, rvol=1.6, minute=20)
    check("all adds -> grade 4", grade_trigger(t, levels, ind), 4)


def test_grade_trigger_floor_clamped_to_one() -> None:
    from astor.core.grading import grade_trigger

    # Severe AVWAP extension (-2) plus last hour (-1) with nothing added
    # would be -2 unclamped; must floor at 1.
    t, levels, ind = _grading_fixture(minute=330, avwap=95.0)
    check("clamped to 1", grade_trigger(t, levels, ind), 1)


def test_grade_trigger_mirrors_for_short_setups() -> None:
    """A short setup needs price *below* open to add, the opposite of a long."""
    from astor.core.grading import grade_trigger

    t, levels, ind = _grading_fixture(direction="short", price=101.0, session_open=102.0)
    check("short: price below open -> adds", grade_trigger(t, levels, ind), 2)

    t, levels, ind = _grading_fixture(direction="short", price=103.0, session_open=102.0)
    check("short: price above open -> no add", grade_trigger(t, levels, ind), 1)


def test_scanner_attaches_auto_grade() -> None:
    """Scanner, not Setup, computes auto_grade. A raw Setup.on_bar call
    (as in test_trigger_extras_include_notify_fields) never sees it at all."""
    sc = _rev_scanner(ReversalLong)
    fired = _feed(sc, _canonical_long_bars())
    assert len(fired) == 1
    check("auto_grade present on a Scanner-fired trigger", "auto_grade" in fired[0].extras, True)


def test_format_trigger_line() -> None:
    """Two lines: the leg quality (identical in shape to what the earlier
    CAPITULATION heads-up showed, so the two compare line to line), then
    the stop and the size it allows. Nothing else."""
    from astor.core.types import Trigger
    from astor.drivers.live import _format_trigger_line

    t = Trigger(
        symbol="ABAT",
        setup="REV-S",
        direction="short",
        trigger_price=2.40,
        bar_start=OPEN,
        bar_end=OPEN + timedelta(minutes=1),
        level_name="EXT",
        level_value=2.50,
        extras={
            "auto_grade": 3,
            "avwap_pct": -3.1,
            "adr_pct": 8.2,
            "rvol": 1.5,
            "reclaim_pct": 0.85,
            "level_stop_pct": 0.85,
            "level_stop_shares": 417,
            "level_stop_dollars": 1000.80,
            "session_extreme": 2.62,
            "session_extreme_pct": 9.1,
            "session_stop_shares": 120,
            "session_stop_dollars": 998.0,
            "leg_size_adr": 1.25,
            "move_rvol": 2.5,
            "ema9_extension_adr": 0.6723,
            "accel_price": 4.6667,
            "accel_vol": 1.0,
        },
    )
    line = _format_trigger_line(t)
    check("headline present", "TRIGGER ABAT" in line, True)
    check("price shown", "@ 2.40" in line, True)
    check("leg size shown", "leg 1.25x ADR" in line, True)
    check("move_rvol shown", "move_rvol 2.5" in line, True)
    check("ema9 extension shown", "ema9_ext 0.67 ADR" in line, True)
    check("accel_price shown", "accel_price 4.67" in line, True)
    check("accel_vol shown", "accel_vol 1.00" in line, True)
    check("level line reads as a magnitude + word, short -> below", "EXT 0.85% below" in line, True)
    check("level position size shown", "417 sh ($1,001)" in line, True)
    # Two lines and no more -- the rest is after-hours review material, still
    # on the Trigger and in the store but not in the in-session view.
    check("exactly two lines", len(line.splitlines()), 2)
    for label, dropped in [
        ("grade", "grade 3/4"), ("avwap", "avwap"), ("adr", "adr 8.20%"),
        ("rvol", "rvol 1.5"), ("session-extreme stop line", "CDH"),
    ]:
        check(f"{label} dropped from the in-session view", dropped in line, False)


def test_format_trigger_line_renders_unmeasurable_leg_values() -> None:
    """Individual leg metrics can come through None; the line still renders
    (Reversal is the only setup left, so there is no all-None case)."""
    from astor.core.types import Trigger
    from astor.drivers.live import _format_trigger_line

    t = Trigger(
        symbol="ABAT", setup="REV-L", direction="long", trigger_price=2.40,
        bar_start=OPEN, bar_end=OPEN + timedelta(minutes=1),
        level_name="EXT", level_value=2.30,
        extras={
            "reclaim_pct": 0.85, "level_stop_shares": 417, "level_stop_dollars": 1000.80,
            "leg_size_adr": 1.25, "move_rvol": None, "ema9_extension_adr": None,
            "accel_price": 4.6667, "accel_vol": None,
        },
    )
    line = _format_trigger_line(t)
    check("measured values still shown", "leg 1.25x ADR" in line, True)
    check("unmeasurable ones read n/a", "move_rvol n/a   ema9_ext n/a ADR" in line, True)
    check("still two lines", len(line.splitlines()), 2)


def test_format_capitulation_alert() -> None:
    from astor.core.types import CapitulationAlert
    from astor.drivers.live import _format_capitulation_alert

    a = CapitulationAlert(
        symbol="ABAT", direction="short", bar_start=OPEN,
        bar_end=OPEN + timedelta(minutes=1), price=2.40,
        leg_extreme=2.62, anchor=2.95, leg_size_adr=1.25, move_rvol=2.5,
        ema9_extension_adr=0.6723, accel_price=4.6667, accel_vol=1.0,
    )
    line = _format_capitulation_alert(a)
    check("headline present", "CAPITULATION" in line and "ABAT" in line, True)
    check("leg metrics shown", "leg 1.25x ADR   move_rvol 2.5" in line, True)
    # Dropped from the display -- bookends of the leg, not actionable mid-session.
    check("extreme not shown", "extreme" in line, False)
    check("anchor not shown", "anchor" in line, False)


def test_should_notify() -> None:
    from astor.core.types import Trigger
    from astor.drivers.live import _should_notify

    def trigger_at(setup: str, hh: int, mm: int, grade: int | None) -> Trigger:
        start = datetime(2026, 8, 14, hh, mm, tzinfo=ET)
        return Trigger(
            symbol="TEST", setup=setup, direction="long", trigger_price=1.0,
            bar_start=start, bar_end=start + timedelta(minutes=1),
            level_name="EXT", level_value=1.0,
            extras={} if grade is None else {"auto_grade": grade},
        )

    # The 15:00 ET cutoff is the whole rule: grade is never consulted.
    check(
        "REV-L notifies with no grade at all, before the cutoff",
        _should_notify(trigger_at("REV-L", 11, 30, None)),
        True,
    )
    check(
        "REV-S notifies with grade 1, before the cutoff",
        _should_notify(trigger_at("REV-S", 14, 59, 1)),
        True,
    )
    check(
        "REV-L does not notify at/after the 15:00 cutoff, even grade 4",
        _should_notify(trigger_at("REV-L", 15, 0, 4)),
        False,
    )


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
                setup="REV-L",
                direction="long",
                trigger_price=price,
                bar_start=session_open,
                bar_end=session_open + timedelta(minutes=1),
                level_name="EXT",
                level_value=100.0,
                extras={"leg_size_adr": 1.2},
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


def test_grade_defaults_to_null_and_notes_is_editable() -> None:
    """grade/notes are the trader's own post-hoc review, entered by editing
    the row directly (e.g. in DataGrip) -- record() never sets them, so
    every new trigger must default to NULL (ungraded) until reviewed. grade
    is a 1-4 scale (D..A); a value outside that range must be rejected."""
    import sqlite3
    import tempfile

    from astor.core.store import TriggerStore
    from astor.core.types import Trigger

    store = TriggerStore(Path(tempfile.mkdtemp()) / "t.db")
    store.start_run("r1", "replay", {"session": "2026-08-14"})
    store.record(
        "r1",
        Trigger(
            symbol="TEST",
            setup="REV-L",
            direction="long",
            trigger_price=100.1,
            bar_start=OPEN,
            bar_end=OPEN + timedelta(minutes=1),
            level_name="EXT",
            level_value=100.0,
            extras={},
        ),
        OPEN + timedelta(minutes=1),
    )
    store.commit()

    row = store.conn.execute("SELECT grade, notes FROM latest_triggers").fetchone()
    check("grade defaults to NULL", row[0], None)
    check("notes defaults to NULL", row[1], None)

    store.conn.execute(
        "UPDATE triggers SET grade = 4, notes = 'clean break' WHERE run_id = 'r1'"
    )
    store.commit()
    row = store.conn.execute("SELECT grade, notes FROM latest_triggers").fetchone()
    check("grade is editable", row[0], 4)
    check("notes is editable", row[1], "clean break")

    try:
        store.conn.execute("UPDATE triggers SET grade = 5 WHERE run_id = 'r1'")
        raise AssertionError("expected a CHECK constraint failure for grade=5")
    except sqlite3.IntegrityError:
        pass
    store.close()


def test_latest_triggers_review_columns_editable_through_the_view() -> None:
    """latest_triggers is a plain view (joins, a WHERE-IN subquery, an ORDER
    BY) -- SQLite treats that as read-only unless an INSTEAD OF trigger says
    otherwise. DataGrip users edit grid cells straight on latest_triggers, so
    an UPDATE issued against the view (not the underlying triggers table)
    must actually take, redirected by id to the one row it names."""
    import tempfile

    from astor.core.store import TriggerStore
    from astor.core.types import Trigger

    store = TriggerStore(Path(tempfile.mkdtemp()) / "t.db")
    store.start_run("r1", "replay", {"session": "2026-08-14"})
    store.record(
        "r1",
        Trigger(
            symbol="TEST",
            setup="REV-L",
            direction="long",
            trigger_price=100.1,
            bar_start=OPEN,
            bar_end=OPEN + timedelta(minutes=1),
            level_name="EXT",
            level_value=100.0,
            extras={},
        ),
        OPEN + timedelta(minutes=1),
    )
    store.commit()

    row_id = store.conn.execute("SELECT id FROM triggers").fetchone()[0]
    store.conn.execute(
        "UPDATE latest_triggers SET grade = 2, notes = 'via view' WHERE id = ?",
        (row_id,),
    )
    store.commit()

    row = store.conn.execute(
        "SELECT grade, notes FROM triggers WHERE id = ?", (row_id,)
    ).fetchone()
    check("grade written through the view", row[0], 2)
    check("notes written through the view", row[1], "via view")
    store.close()


def test_migrate_adds_review_columns_to_a_pre_existing_db() -> None:
    """A database created before grade/notes existed must gain them on the
    next open -- CREATE TABLE IF NOT EXISTS alone would leave an
    already-existing triggers table without the new columns."""
    import sqlite3
    import tempfile

    from astor.core.store import SCHEMA, TriggerStore

    path = Path(tempfile.mkdtemp()) / "old.db"
    old_schema = SCHEMA.replace(
        "    extras_json   TEXT NOT NULL,\n"
        "    -- The trader's own setup-quality grade, D=1 (non-tradeable) .. A=4\n"
        "    -- (best). NULL until reviewed -- not a default like 0/1, since ungraded\n"
        "    -- is a real, distinct state from \"graded D\".\n"
        "    grade         INTEGER CHECK (grade IS NULL OR grade BETWEEN 1 AND 4),\n"
        "    notes         TEXT                       -- the trader's own notes\n",
        "    extras_json   TEXT NOT NULL\n",
    )
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.commit()
    conn.close()

    store = TriggerStore(path)
    columns = {row[1] for row in store.conn.execute("PRAGMA table_info(triggers)")}
    check("grade added by migration", "grade" in columns, True)
    check("notes added by migration", "notes" in columns, True)
    store.close()


def test_migrate_replaces_is_tradeable_with_grade() -> None:
    """A database from the intermediate is_tradeable/notes generation (the
    real data/astor.db's shape as of 2026-08-22) must have is_tradeable
    dropped and grade added -- a Y/N flag doesn't map onto a 1-4 scale, so
    the old values are discarded rather than translated. The views/trigger
    referencing is_tradeable must not block the DROP COLUMN, and must still
    work afterward."""
    import sqlite3
    import tempfile

    from astor.core.store import SCHEMA, TriggerStore

    path = Path(tempfile.mkdtemp()) / "old.db"
    old_schema = SCHEMA.replace(
        "    -- The trader's own setup-quality grade, D=1 (non-tradeable) .. A=4\n"
        "    -- (best). NULL until reviewed -- not a default like 0/1, since ungraded\n"
        "    -- is a real, distinct state from \"graded D\".\n"
        "    grade         INTEGER CHECK (grade IS NULL OR grade BETWEEN 1 AND 4),\n",
        "    is_tradeable  TEXT NOT NULL DEFAULT 'N',\n",
    )
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.commit()
    conn.close()

    store = TriggerStore(path)
    columns = {row[1] for row in store.conn.execute("PRAGMA table_info(triggers)")}
    check("is_tradeable dropped by migration", "is_tradeable" in columns, False)
    check("grade added by migration", "grade" in columns, True)
    check(
        "latest_triggers still queryable after the migration",
        store.conn.execute("SELECT count(*) FROM latest_triggers").fetchone()[0],
        0,
    )
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
            setup="REV-S",
            direction="short",
            trigger_price=2.40,
            bar_start=session_open,
            bar_end=session_open + timedelta(minutes=1),
            level_name="EXT",
            level_value=2.50,
            extras={},
        ),
        session_open + timedelta(minutes=21),
    )
    store.commit()

    rows = store.conn.execute(
        "SELECT run_id, symbol FROM latest_triggers WHERE session = '2026-08-20'"
    ).fetchall()
    check("the live run's trigger shows up in latest_triggers", rows, [("live-1", "ABAT")])
    store.close()


def test_latest_triggers_keeps_live_alongside_the_latest_replay() -> None:
    """latest_run_per_session partitions by (session, mode), not just
    session -- a live run for a session is never superseded by a later
    replay of the same date, or vice versa. Each mode keeps its own
    latest-wins slot: a live run and a replay of the same session both
    surface together for side-by-side comparison, while two replays of that
    same session still collapse to just the newest one.
    """
    import tempfile

    from astor.core.store import TriggerStore
    from astor.core.types import Trigger

    store = TriggerStore(Path(tempfile.mkdtemp()) / "t.db")

    def record_run(run_id: str, mode: str, price: float) -> None:
        session_open = datetime.fromisoformat("2026-08-20T09:30:00").replace(tzinfo=ET)
        store.start_run(run_id, mode, {"session": "2026-08-20"})
        store.record(
            run_id,
            Trigger(
                symbol="TEST",
                setup="REV-L",
                direction="long",
                trigger_price=price,
                bar_start=session_open,
                bar_end=session_open + timedelta(minutes=1),
                level_name="EXT",
                level_value=100.0,
                extras={},
            ),
            session_open + timedelta(minutes=1),
        )
        store.commit()

    record_run("live-1", "live", 100.1)  # the morning's live run
    record_run("replay-1", "replay", 100.2)  # first evening replay attempt
    record_run("replay-2", "replay", 100.3)  # rerun: supersedes replay-1, not live-1

    rows = store.conn.execute(
        "SELECT run_id, trigger_price FROM latest_triggers"
        " WHERE session = '2026-08-20' ORDER BY run_id"
    ).fetchall()
    check(
        "live stays, plus only the newest replay -- not the superseded one",
        rows,
        [("live-1", 100.1), ("replay-2", 100.3)],
    )
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
        setup="REV-L",
        direction="long",
        trigger_price=100.5,
        bar_start=OPEN,
        bar_end=OPEN + timedelta(minutes=1),
        level_name="EXT",
        level_value=100.0,
        extras={"leg_size_adr": 1.5},
    )
    store.record("r1", trig, trig.bar_end)
    store.commit()

    row = store.conn.execute(
        "SELECT session, trigger_time, leg_size_adr, latency_sec FROM latest_triggers"
    ).fetchone()
    check("session date is ET", row[0], "2026-08-14")
    check("trigger time is ET, not UTC", row[1], "09:30:00")
    check("leg_size_adr passed through", row[2], 1.5)
    # Replay detects at bar completion, so latency is zero by construction.
    check("replay latency is zero", row[3], 0.0)
    check("WAL enabled", store.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
    store.close()


def test_record_alert_and_alert_detail_view() -> None:
    """Capitulation alerts and sell-rule track alerts (breakeven/sell) share
    one table and one view -- kind-specific columns simply read NULL for
    the other kind's rows, same COALESCE-free "just extract what's there"
    pattern trigger_detail already uses for U&R/O&F's mirrored key names.
    Track alerts have no bar_end concept (they're not a completed bar
    firing a setup) -- that column stays NULL for them, unlike capitulation
    alerts which always have one.
    """
    import tempfile

    from astor.core.store import TriggerStore

    store = TriggerStore(Path(tempfile.mkdtemp()) / "t.db")
    store.start_run("r1", "live", {"session": "2026-08-14"})
    store.record_alert(
        "r1", "capitulation", "REV", "long", OPEN, 93.3,
        OPEN + timedelta(minutes=4, seconds=30),
        {
            "leg_extreme": 93.0, "anchor": 98.0, "leg_size_adr": 1.25,
            "move_rvol": 250_000.0, "ema9_extension_adr": 0.7166,
        },
        bar_end=OPEN + timedelta(minutes=5),
    )
    store.record_alert(
        "r1", "breakeven", "REV", "long", OPEN + timedelta(minutes=10), 96.0,
        OPEN + timedelta(minutes=10, seconds=5),
        {"entry": 94.0, "stop": 93.0, "peak": 96.0, "r": 1.0, "target_1r": 95.0, "account": "U123"},
    )
    store.commit()

    rows = store.conn.execute(
        "SELECT kind, leg_extreme, entry, bar_end FROM alert_detail ORDER BY kind"
    ).fetchall()
    check("two alerts recorded", len(rows), 2)
    by_kind = {r[0]: r for r in rows}
    check(
        "capitulation row has leg_extreme, no entry",
        by_kind["capitulation"][1:3],
        (93.0, None),
    )
    check(
        "breakeven row has entry, no leg_extreme",
        by_kind["breakeven"][1:3],
        (None, 94.0),
    )
    check("track alert has no bar_end", by_kind["breakeven"][3], None)
    check("capitulation alert has a real bar_end", by_kind["capitulation"][3] is not None, True)
    store.close()


# ---------------------------------------------------------------------------
# Reversal (REV-L/REV-S): leg tracking, capitulation-quality filters, and the
# capitulation-in-progress alert.
# ---------------------------------------------------------------------------


def test_reversal_long_basic() -> None:
    sc = _rev_scanner(ReversalLong)
    triggers = _feed(sc, _canonical_long_bars())
    check("exactly one trigger", len(triggers), 1)
    t = triggers[0]
    check("setup", t.setup, "REV-L")
    check("direction", t.direction, "long")
    check("level_name", t.level_name, "EXT")
    check("level_value is the leg extreme", t.level_value, 93.0)
    check("trigger_price", t.trigger_price, 94.0)
    check("anchor is session_open (prev_close/gap out of scope)", t.extras["anchor"], 98.0)
    check("leg_size_adr", t.extras["leg_size_adr"], 1.25)
    check("retrace_pct", t.extras["retrace_pct"], 20.0)
    check("retrace_via pct, not an EMA9 reclaim", t.extras["retrace_via"], "pct")
    check(
        "leg_duration_minutes -- bar 0's open through the extreme bar's close, 5 bars",
        t.extras["leg_duration_minutes"],
        5.0,
    )
    check("move_rvol", t.extras["move_rvol"], 250_000.0)
    check("ema9_extension_adr", t.extras["ema9_extension_adr"], 0.6723)
    check("accel_price now filtered on too (>= min_accel_price)", t.extras["accel_price"], 4.6667)
    check("accel_vol now filtered on too (>= min_accel_vol)", t.extras["accel_vol"], 1.0)
    check("reclaim_pct aliases level_stop_pct", t.extras["reclaim_pct"], t.extras["level_stop_pct"])


def test_reversal_short_mirrors_long() -> None:
    sc = _rev_scanner(ReversalShort)
    triggers = _feed(sc, _canonical_short_bars())
    check("exactly one trigger", len(triggers), 1)
    t = triggers[0]
    check("setup", t.setup, "REV-S")
    check("direction", t.direction, "short")
    check("level_value is the leg extreme", t.level_value, 107.7)
    check("trigger_price", t.trigger_price, 106.5)
    check("anchor is session_open (prev_close/gap out of scope)", t.extras["anchor"], 102.0)
    check("retrace_pct", round(t.extras["retrace_pct"], 4), 21.0526)


def test_reversal_anchor_ignores_prev_close() -> None:
    # prev_close set far from session_open, on both sides -- anchor (98.0,
    # the running session high, which happens to stay pinned at the open in
    # this bar shape) must be unaffected regardless, since prev_close/gap
    # play no part in this setup's anchor at all.
    for prev_close in (90.0, 110.0):
        levels = DailyLevels(
            "REV", date(2026, 8, 13), 0.0, 0.0, adr_pct=4.0, prev_close=prev_close, adv=100_000_000.0
        )
        sc = Scanner({"REV": levels}, [ReversalLong])
        sc.curves["REV"] = _generous_curve()
        triggers = _feed(sc, _canonical_long_bars())
        check(f"exactly one trigger (prev_close={prev_close})", len(triggers), 1)
        check(
            f"anchor unaffected by prev_close={prev_close}",
            triggers[0].extras["anchor"],
            98.0,
        )


def test_reversal_requires_min_leg_adr() -> None:
    # adr_pct=20 -> ADR=$20.00, so the default 1.0x-ADR floor (=20.0) exceeds
    # the 5.0 leg this scenario forms -- same bars as the otherwise-passing
    # basic case, isolating the ADR floor specifically.
    levels = DailyLevels(
        "REV", date(2026, 8, 13), 0.0, 0.0, adr_pct=20.0, prev_close=100.0, adv=100_000_000.0
    )
    sc = Scanner({"REV": levels}, [ReversalLong])
    sc.curves["REV"] = _generous_curve()
    check("leg too small relative to ADR never fires", _feed(sc, _canonical_long_bars()), [])


def test_reversal_requires_rvol_floor() -> None:
    # Average volume at the extreme minute set absurdly high, so the frozen
    # move_rvol comes in far below the 2.0x default floor for this ADV tier
    # -- same bars as the otherwise-passing basic case, isolating the RVOL
    # floor.
    demanding_curve = VolumeCurve(
        "REV", sessions=10,
        cum_volume_by_minute={(OPEN + timedelta(minutes=4)).timetz().replace(tzinfo=None): 10_000_000.0},
    )
    sc = _rev_scanner(ReversalLong, curve=demanding_curve)
    check("thin RVOL relative to average never fires", _feed(sc, _canonical_long_bars()), [])


def test_candidate_requires_a_bar_closing_against_the_leg() -> None:
    """The setup is the reversal, so only a bar that closed against the leg
    is a candidate at all -- up for REV-L, down for REV-S. A bar closing the
    leg's own way is the push still running, whatever its retrace reads."""
    through_extreme = _canonical_long_bars()[:5]  # anchor 98.0, extreme 93.0 at bar 4

    # Same close (94.0 -> retrace 20%, in band) as the canonical fixture's
    # own firing bar, but this one closed down: the leg's own direction.
    sc = _rev_scanner(ReversalLong)
    with_the_leg = bar(5, 94.2, 94.3, 93.1, 94.0, 10_000, symbol="REV")
    check(
        "REV-L: a down-closing bar is not a candidate, even at a firing retrace",
        _feed(sc, through_extreme + [with_the_leg]),
        [],
    )
    doji = bar(6, 94.0, 94.4, 93.9, 94.0, 10_000, symbol="REV")
    check("REV-L: a doji closes against nothing either", sc.on_bar(doji), [])
    against_the_leg = bar(7, 94.0, 94.4, 93.9, 94.3, 10_000, symbol="REV")
    check("REV-L: the up-closing bar right after does fire", len(sc.on_bar(against_the_leg)), 1)

    # Mirror: REV-S's leg is the move up, so it needs a down-closing bar.
    sc_s = _rev_scanner(ReversalShort)
    short_through_extreme = _canonical_short_bars()[:5]  # anchor 102.0, extreme 107.7
    check(
        "REV-S: an up-closing bar is not a candidate",
        _feed(sc_s, short_through_extreme + [bar(5, 106.4, 107.6, 106.3, 106.5, 10_000, symbol="REV")]),
        [],
    )
    check(
        "REV-S: the down-closing bar right after does fire",
        len(sc_s.on_bar(bar(6, 106.8, 106.9, 106.3, 106.5, 10_000, symbol="REV"))),
        1,
    )


def test_reversal_requires_min_leg_minutes() -> None:
    """A leg that built in only 3 bars never fires under the default
    4-minute floor, even though it clears every other filter -- but does
    fire once that floor is explicitly loosened."""
    fast_bars = [
        bar(0, 98.0, 98.0, 97.5, 97.8, 50_000, symbol="REV"),
        bar(1, 97.8, 97.9, 95.0, 95.3, 50_000, symbol="REV"),
        bar(2, 95.3, 95.4, 93.0, 93.3, 50_000, symbol="REV"),  # extreme at minute 2 -- leg=5.0
        bar(3, 93.3, 93.8, 93.1, 94.0, 10_000, symbol="REV"),  # retrace 20%, elapsed 1
    ]
    default_sc = _rev_scanner(ReversalLong)
    check(
        "3-bar leg never fires under the default 4-minute floor",
        _feed(default_sc, fast_bars),
        [],
    )

    levels = DailyLevels(
        "REV", date(2026, 8, 13), 0.0, 0.0, adr_pct=4.0, prev_close=100.0, adv=100_000_000.0
    )
    # min_accel_price/min_accel_vol loosened too -- this 3-bar leg has too
    # few bars to form an "earlier" accel window (falls back to the neutral
    # 1.0), unrelated to the min_leg_minutes floor this test isolates.
    loose_sc = Scanner(
        {"REV": levels}, [ReversalLong],
        {"REV-L": {"min_leg_minutes": 0.0, "min_accel_price": 0.0, "min_accel_vol": 0.0}},
    )
    loose_sc.curves["REV"] = _generous_curve()
    triggers = _feed(loose_sc, fast_bars)
    check("same leg fires once the floor is loosened", len(triggers), 1)
    check("leg_duration_minutes -- 3 bars", triggers[0].extras["leg_duration_minutes"], 3.0)


def test_leg_duration_counts_bars_not_gaps_between_them() -> None:
    """A leg of N one-minute bars took N minutes to build: measured from the
    first push bar's open to the last pushing bar's close, not start-to-start
    (which counts the gaps between bars and lands one short)."""
    bars = [
        bar(0, 98.0, 98.0, 97.5, 97.8, 50_000, symbol="REV"),  # anchor 98.0, push bar 1
        bar(1, 97.8, 97.9, 96.0, 96.2, 50_000, symbol="REV"),  # push bar 2
        bar(2, 96.2, 96.4, 95.0, 95.3, 50_000, symbol="REV"),  # push bar 3
        bar(3, 95.3, 95.4, 93.0, 93.3, 50_000, symbol="REV"),  # push bar 4 -- extreme 93.0
        bar(4, 93.3, 93.8, 93.1, 94.0, 10_000, symbol="REV"),  # closes back up -- leg over
    ]
    sc = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_price": 0.0, "min_accel_vol": 0.0})
    triggers = _feed(sc, bars)
    check("fires", len(triggers), 1)
    # Bar 0's open (9:30) through bar 3's close (9:34) -- 4 bars, 4 minutes.
    check("4 push bars read 4 minutes", triggers[0].extras["leg_duration_minutes"], 4.0)


def test_leg_duration_includes_bars_still_closing_the_push_way() -> None:
    """A bar that closes the push's way without taking out the extreme is
    still the leg working (2026-09-16 ADPT 09:34: o 28.62 c 28.65 under a
    28.72 high), so the run of them right after the extreme counts toward
    how long the leg took. The extreme itself does not move.
    """
    bars = [
        bar(0, 102.0, 102.0, 102.0, 102.2, 50_000, symbol="REV"),  # anchor 102.0, push bar 1
        bar(1, 102.2, 104.0, 102.1, 103.9, 50_000, symbol="REV"),  # push bar 2
        bar(2, 103.9, 105.5, 103.5, 105.2, 50_000, symbol="REV"),  # push bar 3
        bar(3, 105.2, 107.7, 105.0, 107.4, 50_000, symbol="REV"),  # push bar 4 -- extreme 107.7
        bar(4, 107.4, 107.6, 107.3, 107.5, 8_000, symbol="REV"),   # up close under the high
        bar(5, 107.5, 107.6, 106.4, 106.5, 10_000, symbol="REV"),  # closes down -- leg over
    ]
    sc = _rev_scanner(ReversalShort, setup_kwargs={"min_accel_price": 0.0, "min_accel_vol": 0.0})
    triggers = _feed(sc, bars)
    check("fires", len(triggers), 1)
    t = triggers[0]
    # Bar 0's open (9:30) through bar 4's close (9:35) -- the extreme bar plus
    # the one continuation bar after it.
    check("continuation bar counts toward duration", t.extras["leg_duration_minutes"], 5.0)
    check("but the extreme is still the high the leg reached", t.extras["leg_extreme"], 107.7)
    check("and the stop still sits on it", t.level_value, 107.7)

    # One bar later the run is broken, so a further up-close cannot re-open it.
    more = bars + [
        bar(6, 106.5, 106.8, 106.3, 106.7, 9_000, symbol="REV"),  # up close, but after the break
        bar(7, 106.7, 106.9, 106.0, 106.2, 9_000, symbol="REV"),
    ]
    sc2 = _rev_scanner(ReversalShort, setup_kwargs={"min_accel_price": 0.0, "min_accel_vol": 0.0})
    durations = {
        t.extras["leg_duration_minutes"] for t in _feed(sc2, more)
    }
    check("a later up bar never re-opens a closed leg", durations, {5.0})


def test_reversal_min_confirm_minutes_still_works_when_set() -> None:
    """min_confirm_minutes defaults to 0 (the very next bar can confirm),
    but the knob still works when explicitly set -- a same-bar-ish retrace
    is rejected until enough time has passed since the extreme."""
    bars = _canonical_long_bars() + [
        bar(6, 94.0, 94.2, 93.9, 94.1, 5_000, symbol="REV"),  # elapsed 2 minutes
    ]
    levels = DailyLevels(
        "REV", date(2026, 8, 13), 0.0, 0.0, adr_pct=4.0, prev_close=100.0, adv=100_000_000.0
    )

    sc = Scanner({"REV": levels}, [ReversalLong], {"REV-L": {"min_confirm_minutes": 2.0}})
    sc.curves["REV"] = _generous_curve()
    check(
        "bar 5 (elapsed 1 minute) too early for a 2-minute confirm wait",
        _feed(sc, bars[:6]),
        [],
    )

    sc2 = Scanner({"REV": levels}, [ReversalLong], {"REV-L": {"min_confirm_minutes": 2.0}})
    sc2.curves["REV"] = _generous_curve()
    triggers = _feed(sc2, bars)
    check("bar 6 (elapsed 2 minutes) confirms", len(triggers), 1)
    check("retrace_minutes", triggers[0].extras["retrace_minutes"], 2.0)


def test_reversal_retrace_past_50pct_kills_leg_until_new_extreme() -> None:
    # Every bar's high is capped at 98.0 throughout (never exceeding bar 0's
    # high) so the anchor never itself moves during this test -- isolating
    # the extreme/retrace/dead-leg behavior from anchor movement.
    sc = _rev_scanner(ReversalLong)
    bars = [
        bar(0, 98.0, 98.0, 97.5, 97.8, 1000, symbol="REV"),
        bar(1, 97.8, 98.0, 96.0, 96.2, 1000, symbol="REV"),
        bar(2, 96.2, 96.5, 95.0, 95.3, 1000, symbol="REV"),
        bar(3, 95.3, 96.0, 94.0, 94.3, 1000, symbol="REV"),
        bar(4, 94.3, 95.0, 93.0, 93.3, 1000, symbol="REV"),  # leg1 extreme 93.0 (anchor 98, size 5.0)
        bar(5, 93.3, 98.0, 93.5, 98.0, 1000, symbol="REV"),  # retrace 100% -- kills leg1
        bar(6, 98.0, 98.0, 96.0, 96.5, 1000, symbol="REV"),  # back in-band, but dead
        bar(7, 96.5, 96.8, 96.0, 96.6, 1000, symbol="REV"),
        bar(8, 96.6, 96.9, 96.0, 96.7, 1000, symbol="REV"),
        bar(9, 96.7, 97.0, 96.0, 96.8, 1000, symbol="REV"),
        bar(10, 96.8, 97.1, 96.0, 96.9, 1000, symbol="REV"),
    ]
    check("dead leg never fires even back in-band", _feed(sc, bars), [])

    # A new, deeper low re-arms a fresh leg (own anchor reused, own extreme).
    # Duration is now measured from the dead retracement's own turnaround
    # (bar 11, the first bar that stops climbing further -- see _dead_bars),
    # not from the original anchor at bar 0 -- so bars 11-12 keep declining
    # (still dead, not yet past session_low) before bar 13 finally breaks
    # through, giving leg2 a real (>= min_leg_minutes) span of its own.
    more_bars = [
        bar(11, 96.9, 97.0, 94.0, 94.3, 1000, symbol="REV"),  # dead, but turns down (push start)
        bar(12, 94.3, 94.5, 93.5, 93.8, 1000, symbol="REV"),  # still dead (low 93.5 > 93.0), declining
        bar(13, 93.8, 94.0, 92.0, 92.3, 1000, symbol="REV"),  # breaks session_low -- leg2 revives
        bar(14, 92.3, 92.7, 90.0, 90.3, 1000, symbol="REV"),
        bar(15, 90.3, 90.6, 89.0, 89.3, 1000, symbol="REV"),  # new leg2 extreme 89.0 (anchor 98, size 9.0)
        bar(16, 89.3, 90.5, 89.1, 90.0, 1000, symbol="REV"),  # retrace 11%, elapsed 1
    ]
    triggers = _feed(sc, more_bars)
    check("exactly one trigger for leg2", len(triggers), 1)
    t = triggers[0]
    check("fires on the new, deeper extreme", t.level_value, 89.0)
    check("anchor unchanged (not recomputed, still session_open)", t.extras["anchor"], 98.0)
    # bar 11 (the turn) to bar 15 (leg2's extreme) -- not bar 0 (the original,
    # now-irrelevant anchor timestamp).
    check(
        "leg_duration_minutes measured from the dead leg's own turnaround",
        t.extras["leg_duration_minutes"],
        5.0,
    )


def test_reversal_leg_bars_reset_on_rearm_after_dead_leg() -> None:
    """Regression test for a real bug found reviewing SEDG's 2026-09-10
    10:59 REV-S trigger: a leg that died (retrace > 50%) and later re-armed
    with a deeper extreme, anchor unchanged throughout, must compute
    accel_price/accel_vol only from the *new* push's own bars -- not mixed
    with a stale leftover bar from the earlier, unrelated, dead push. Before
    the fix, _leg_bars only reset on a new anchor, so the dead leg's last
    _leg_bars entry (here, a single huge-volume bar) survived into the
    revived leg's "earlier" bucket and produced a misleadingly low ratio.

    min_accel_price is loosened here -- this fixture is about leg_bars/
    accel_vol content specifically (the small, steadily-rising volumes on
    leg2 are what accel_vol's own assertion below exercises), not about
    whether leg2 also happens to clear the unrelated accel_price floor.
    """
    sc = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_price": 0.0})
    bars = [
        # leg1: huge, distinctive volume so any leakage into leg2 is obvious.
        bar(0, 98.0, 98.0, 97.5, 97.8, 500_000, symbol="REV"),
        bar(1, 97.8, 98.0, 96.0, 96.2, 500_000, symbol="REV"),
        bar(2, 96.2, 96.5, 95.0, 95.3, 500_000, symbol="REV"),
        bar(3, 95.3, 96.0, 94.0, 94.3, 500_000, symbol="REV"),
        bar(4, 94.3, 95.0, 93.0, 93.3, 500_000, symbol="REV"),  # leg1 extreme 93.0 (anchor 98, size 5.0)
        bar(5, 93.3, 98.0, 93.5, 98.0, 500_000, symbol="REV"),  # retrace 100% -- kills leg1
        bar(6, 98.0, 98.0, 96.0, 96.5, 500_000, symbol="REV"),  # dead, back in-band
        bar(7, 96.5, 96.8, 96.0, 96.6, 500_000, symbol="REV"),
        bar(8, 96.6, 96.9, 96.0, 96.7, 500_000, symbol="REV"),
        bar(9, 96.7, 97.0, 96.0, 96.8, 500_000, symbol="REV"),
        bar(10, 96.8, 97.1, 96.0, 96.9, 500_000, symbol="REV"),
        # leg2: same anchor (98), a fresh deeper extreme with small, distinct,
        # steadily rising volume -- this is the only data accel should see.
        bar(11, 96.9, 97.0, 92.0, 92.3, 5_000, symbol="REV"),
        bar(12, 92.3, 92.7, 90.5, 90.8, 10_000, symbol="REV"),
        bar(13, 90.8, 91.0, 89.0, 89.3, 20_000, symbol="REV"),
        bar(14, 89.3, 89.6, 87.5, 87.8, 30_000, symbol="REV"),
        bar(15, 87.8, 88.0, 86.0, 86.3, 40_000, symbol="REV"),  # leg2 extreme 86.0 (anchor 98, size 12.0)
        bar(16, 86.3, 87.5, 86.1, 87.4, 5_000, symbol="REV"),  # retrace 11.7%, fires
    ]
    triggers = _feed(sc, bars)
    check("exactly one trigger, off leg2", len(triggers), 1)
    t = triggers[0]
    check("fires on leg2's extreme", t.level_value, 86.0)
    setup = sc._state[("REV", "REV-L")]
    # leg2's 5 push bars, plus the retrace bar that fired (16) -- _leg_bars
    # keeps accumulating past the extreme since a pause and the start of a
    # retrace look identical in real time; accel filters back down to just
    # the bars at-or-before extreme_at (see push_bars in on_bar), which is
    # what the accel_vol assertion below actually exercises. Either way,
    # none of leg1's stale 500k-volume bars are present.
    check(
        "leg_bars holds only leg2's bars -- none of leg1's",
        [b.volume for b in setup._leg_bars],
        [5_000, 10_000, 20_000, 30_000, 40_000, 5_000],
    )
    # recent = mean(20k,30k,40k)=30k, earlier = mean(5k,10k)=7.5k -> 4.0.
    # Before the fix, "earlier" picked up a single 500k-volume leftover bar
    # from leg1, producing a ratio near-zero instead.
    check("accel_vol computed only from leg2's own bars", t.extras["accel_vol"], 4.0)


def _cdna_style_bars() -> list[BarEvent]:
    """REV-S leg: pre-leg low at bar 2 (9:32), peak at bar 6 (9:36). Under
    the new looser defaults this now fires at bar 7 (a 15% retrace already
    clears the 10% floor), not bar 12 as it did under the old 25% floor.
    Not built to clear accel_price/accel_vol/ema9_extension_adr -- tests
    using this fixture are about anchor-shift/timing correctness and pass
    setup_kwargs zeroing those, same pattern as _rev_scanner's other
    isolation tests."""
    return [
        bar(0, 100.0, 100.0, 99.0, 99.5, 1000, symbol="REV"),  # session open 100.0
        bar(1, 99.5, 99.5, 97.0, 97.5, 1000, symbol="REV"),  # anchor moves to 97.0
        bar(2, 97.5, 97.5, 96.0, 96.3, 1000, symbol="REV"),  # anchor moves to 96.0 -- pre-leg low
        bar(3, 96.3, 100.5, 96.1, 100.2, 1000, symbol="REV"),  # reversal candle -- new extreme 100.5
        bar(4, 100.2, 102.0, 100.0, 101.8, 1000, symbol="REV"),  # new extreme 102.0
        bar(5, 101.8, 104.0, 101.5, 103.8, 1000, symbol="REV"),  # new extreme 104.0
        bar(6, 103.8, 106.0, 103.5, 105.7, 1000, symbol="REV"),  # new extreme 106.0 -- the leg's peak
        bar(7, 105.7, 105.5, 104.0, 104.5, 1000, symbol="REV"),  # retrace 15%, elapsed 1 -- fires
        bar(8, 104.5, 105.0, 103.5, 104.0, 1000, symbol="REV"),
        bar(9, 104.0, 104.5, 103.0, 103.6, 1000, symbol="REV"),
        bar(10, 103.6, 104.0, 102.8, 103.3, 1000, symbol="REV"),
        bar(11, 103.3, 103.8, 102.5, 103.0, 1000, symbol="REV"),
        bar(12, 103.0, 103.5, 102.2, 102.8, 1000, symbol="REV"),
    ]


def test_reversal_short_anchor_shifts_to_pre_leg_low() -> None:
    # The CDNA case that motivated this: price dips right after the open,
    # *before* the real up-leg (the one REV-S actually tracks) begins. The
    # anchor must be that pre-leg low (96.0), not session_open (100.0).
    sc = _rev_scanner(
        ReversalShort,
        setup_kwargs={"min_accel_price": 0.0, "min_accel_vol": 0.0, "min_ema9_extension_adr": 0.0},
    )
    triggers = _feed(sc, _cdna_style_bars())
    check("exactly one trigger", len(triggers), 1)
    t = triggers[0]
    check("fires at bar 7, the first bar clearing the 10% floor", t.bar_start, OPEN + timedelta(minutes=7))
    check("level_value is the leg extreme (session high)", t.level_value, 106.0)
    check("anchor is the pre-leg low, not session_open", t.extras["anchor"], 96.0)
    check("retrace_pct off the pre-leg-low leg (10.0 wide)", t.extras["retrace_pct"], 15.0)
    check("leg_duration_minutes (bar 2's open to bar 6's close)", t.extras["leg_duration_minutes"], 5.0)


def test_reversal_leg_timing_correct_for_symbol_added_mid_session() -> None:
    # The real bug this fixes: a symbol added to Live mid-session gets a
    # Setup that starts clean (Scanner.backfill_bar never calls setup.on_bar
    # -- by design, so it doesn't re-detect a trigger from before it was
    # tracked). Before this fix, the Setup would stamp whichever boundary
    # had already stabilized *before* the symbol joined with the join time
    # instead of its true historical moment, corrupting leg_duration_minutes
    # (and, in the mirror case, retrace_minutes/move_rvol too). Splitting
    # the exact same bars as the test above into "backfilled" (0-2, before
    # the symbol supposedly joined) and "live" (3-12, after) must produce
    # an identical trigger to the fully-live version.
    bars = _cdna_style_bars()
    loose_kwargs = {"min_accel_price": 0.0, "min_accel_vol": 0.0, "min_ema9_extension_adr": 0.0}

    baseline_sc = _rev_scanner(ReversalShort, setup_kwargs=loose_kwargs)
    baseline = _feed(baseline_sc, bars)[0]

    sc = _rev_scanner(ReversalShort, setup_kwargs=loose_kwargs)
    for b in bars[:3]:  # backfilled -- establishes the pre-leg low, bars 0-2
        sc.backfill_bar(b)
    triggers = []
    for b in bars[3:]:  # "joined" here -- setup.on_bar only sees these
        triggers.extend(sc.on_bar(b))

    check("exactly one trigger", len(triggers), 1)
    t = triggers[0]
    check("anchor matches the fully-live run", t.extras["anchor"], baseline.extras["anchor"])
    check(
        "leg_duration_minutes matches the fully-live run (not truncated to the join time)",
        t.extras["leg_duration_minutes"],
        baseline.extras["leg_duration_minutes"],
    )
    check(
        "anchor_start matches the fully-live run",
        t.extras["anchor_start"],
        baseline.extras["anchor_start"],
    )
    check("retrace_pct matches the fully-live run", t.extras["retrace_pct"], baseline.extras["retrace_pct"])
    check("move_rvol matches the fully-live run", t.extras["move_rvol"], baseline.extras["move_rvol"])
    # anchor last set at bar 2 (9:32), extreme (106.0) last set at bar 6 -- so
    # bar 2's open (9:32) through bar 6's close (9:37) = 5 bars.
    check("leg_duration_minutes", t.extras["leg_duration_minutes"], 5.0)


def test_reversal_long_anchor_shifts_to_pre_leg_high() -> None:
    # Mirror of the above: price ticks up right after the open, before the
    # real down-leg begins. The anchor must be that pre-leg high (104.0),
    # not session_open (100.0). Fires at bar 7 (12% retrace) under the new
    # 10% floor, not bar 12 as it did under the old 25% floor. Not built to
    # clear accel_price/accel_vol/ema9_extension_adr -- this test is about
    # anchor-shift correctness, so those are zeroed.
    sc = _rev_scanner(
        ReversalLong,
        setup_kwargs={"min_accel_price": 0.0, "min_accel_vol": 0.0, "min_ema9_extension_adr": 0.0},
    )
    bars = [
        bar(0, 100.0, 101.0, 100.0, 100.5, 1000, symbol="REV"),  # session open 100.0
        bar(1, 100.5, 103.0, 100.2, 102.8, 1000, symbol="REV"),  # anchor moves to 103.0
        bar(2, 102.8, 104.0, 102.5, 103.8, 1000, symbol="REV"),  # anchor moves to 104.0 -- pre-leg high
        bar(3, 103.8, 104.0, 99.5, 100.0, 1000, symbol="REV"),  # reversal candle -- new extreme 99.5
        bar(4, 100.0, 100.2, 98.0, 98.3, 1000, symbol="REV"),  # new extreme 98.0
        bar(5, 98.3, 98.5, 96.0, 96.3, 1000, symbol="REV"),  # new extreme 96.0
        bar(6, 96.3, 96.5, 94.0, 94.3, 1000, symbol="REV"),  # new extreme 94.0 -- the leg's trough
        bar(7, 94.3, 95.5, 94.2, 95.2, 1000, symbol="REV"),  # retrace 12%, elapsed 1 -- fires
        bar(8, 95.2, 96.0, 94.5, 95.8, 1000, symbol="REV"),
        bar(9, 95.8, 96.3, 94.8, 96.0, 1000, symbol="REV"),
        bar(10, 96.0, 96.6, 95.0, 96.3, 1000, symbol="REV"),
        bar(11, 96.3, 97.0, 95.2, 96.8, 1000, symbol="REV"),
        bar(12, 96.8, 97.3, 95.5, 97.0, 1000, symbol="REV"),
    ]
    triggers = _feed(sc, bars)
    check("exactly one trigger", len(triggers), 1)
    t = triggers[0]
    check("fires at bar 7, the first bar clearing the 10% floor", t.bar_start, OPEN + timedelta(minutes=7))
    check("level_value is the leg extreme (session low)", t.level_value, 94.0)
    check("anchor is the pre-leg high, not session_open", t.extras["anchor"], 104.0)
    check("retrace_pct off the pre-leg-high leg (10.0 wide)", t.extras["retrace_pct"], 12.0)
    # anchor last set at bar 2 (9:32), extreme (94.0) last set at bar 6 -- so
    # bar 2's open (9:32) through bar 6's close (9:37) = 5 bars.
    check("leg_duration_minutes", t.extras["leg_duration_minutes"], 5.0)


def _slow_converge_bars() -> list[BarEvent]:
    """A leg built slowly enough (30 minutes) that the 9EMA has time to
    converge close to price -- unlike a fast capitulation-style crash,
    where the EMA lags far behind (see test_reversal_long_basic's much
    bigger ema9_extension_adr for the same-shaped fast leg). This is what
    lets a small bounce reclaim the EMA well before a 10% retrace, and lets
    the ema9_extension_adr filter have something meaningful to reject.
    """
    bars = [bar(0, 100.0, 100.5, 99.8, 100.0, 50_000, symbol="REV")]
    for i, c in enumerate([98.0, 96.5, 95.2, 94.6], start=1):
        bars.append(bar(i, c + 0.3, c + 0.3, c - 0.05, c, 50_000, symbol="REV"))
    for i in range(5, 30):  # flat, letting ema9 converge toward 94.6
        bars.append(bar(i, 94.6, 94.65, 94.55, 94.6, 50_000, symbol="REV"))
    bars.append(bar(30, 94.6, 94.6, 94.5, 94.55, 50_000, symbol="REV"))  # extreme 94.5
    bars.append(bar(31, 94.55, 95.0, 94.5, 94.9, 10_000, symbol="REV"))  # small bounce
    return bars


def test_reversal_retrace_via_ema9_reclaim_below_pct_floor() -> None:
    """A bounce that reclaims the 9EMA fires even though the retrace itself
    is well under the 10% floor -- the OR side of the retrace-or-reclaim
    condition. Isolated from the separate ema9_extension_adr floor (which
    this same slow-building leg fails -- see the next test) via an explicit
    override, since that filter is a distinct question ("was the move
    extended enough to begin with") from this one ("has price come back").
    """
    levels = DailyLevels(
        "REV", date(2026, 8, 13), 0.0, 0.0, adr_pct=4.0, prev_close=100.0, adv=100_000_000.0
    )
    # accel_price/accel_vol zeroed too -- this leg's whole point is a slow,
    # flat, non-accelerating build (bars 5-29 are identical, letting ema9
    # converge), so it fails those by design, unrelated to the reclaim
    # behavior being isolated here.
    sc = Scanner(
        {"REV": levels}, [ReversalLong],
        {"REV-L": {"min_ema9_extension_adr": 0.0, "min_accel_price": 0.0, "min_accel_vol": 0.0}},
    )
    sc.curves["REV"] = _generous_curve()
    triggers = _feed(sc, _slow_converge_bars())
    check("exactly one trigger", len(triggers), 1)
    t = triggers[0]
    check("retrace_pct is well under the 10% floor", t.extras["retrace_pct"], 6.6667)
    check("fired via the EMA9 reclaim, not the pct floor", t.extras["retrace_via"], "ema9_reclaim")


def test_reversal_requires_min_ema9_extension() -> None:
    """The same slow-building leg (EMA9 has had time to converge close to
    price) fails to fire under the default 0.05-ADR extension floor, even
    though the retrace-or-reclaim condition is already satisfied."""
    sc = _rev_scanner(ReversalLong)
    check(
        "leg not extended enough from its own 9EMA never fires",
        _feed(sc, _slow_converge_bars()),
        [],
    )


def test_trace_reports_every_filter_on_an_early_rejection() -> None:
    """A rejection shows the whole filter panel, not just the filters it got
    as far as measuring -- picking a threshold means seeing what the other
    filters said on that same bar."""
    sc = _rev_scanner(ReversalLong, setup_kwargs={"min_leg_adr": 99.0})
    trace: dict[str, list[dict]] = {}
    for b in _canonical_long_bars():
        sc.on_bar(b, trace=trace)

    candidates = [e["text"] for e in trace["REV-L"] if e["text"].startswith("candidate")]
    check("the leg filter is what rejected it", "FAIL leg" in candidates[-1], True)
    line = candidates[-1]
    # Every filter, including the ones checked *after* the one that failed.
    for label in (
        "retrace", "elapsed", "built", "leg", "move_rvol", "ema9_ext",
        "accel_price", "accel_vol",
    ):
        check(f"{label} present on an early rejection", f"{label} " in line, True)
    # leg carries both framings the trader tunes on: the ADR multiple and
    # the plain percent move.
    check("leg shows its ADR multiple against the floor", "1.25x/99x ADR" in line, True)
    check("leg shows its percent change", "5.10%" in line, True)
    check("accel_vol measured despite the earlier failure", "accel_vol 1.00/1" in line, True)


def test_trace_fire_line_carries_the_same_panel() -> None:
    sc = _rev_scanner(ReversalLong)
    trace: dict[str, list[dict]] = {}
    for b in _canonical_long_bars():
        sc.on_bar(b, trace=trace)
    fire = [e["text"] for e in trace["REV-L"] if e["text"].startswith("FIRE")]
    check("one fire line", len(fire), 1)
    check("same panel as a rejection", "accel_price 4.6667/1.2" in fire[0], True)
    check("says so plainly", fire[0].endswith("all filters pass"), True)


def test_reversal_requires_min_accel_price() -> None:
    # _canonical_long_bars' accel_price is 4.6667 (see test_reversal_long_basic)
    # -- comfortably above the 1.2 default, but below a threshold raised
    # past it, isolating the accel_price floor specifically.
    sc = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_price": 10.0})
    check("leg not accelerating enough never fires", _feed(sc, _canonical_long_bars()), [])


def test_reversal_requires_min_accel_vol() -> None:
    # _canonical_long_bars' accel_vol is exactly 1.0 (flat volume through
    # the whole push) -- clears the 1.0 default (not strictly less than),
    # but fails once the floor is raised past it.
    sc = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_vol": 1.5})
    check("volume not building enough never fires", _feed(sc, _canonical_long_bars()), [])


def test_reversal_ignores_retrace_against_a_stale_extreme() -> None:
    """Regression test for a real bug found reviewing SEDG's 2026-09-10
    REV-S leg: while the anchor is still setting fresh extremes of its own
    (session low dropping bar after bar) and the *extreme* hasn't been
    refreshed since before that started, retrace_pct measured against that
    stale, disconnected extreme is arithmetic noise, not a real
    retracement -- a close sitting near the ever-deepening anchor reads as
    a huge "retrace" purely because the stale extreme is far away, killing
    the leg over and over for no real reason. The fix: don't evaluate
    retrace/death at all until extreme_at is more recent than anchor_at --
    i.e. until a genuine push from the current anchor exists.

    This is deliberately about retrace/death only, not _leg_bars: by the
    trader's own call, the stale extreme here is the *other* setup's
    territory (REV-L's leg down), not a reason to exclude these bars from
    REV-S's own accel sample -- accel_price/accel_vol are meant to cover
    the same anchor-to-extreme span leg_duration_minutes reports, so these
    bars still accumulate in _leg_bars normally; only the
    death-by-stale-arithmetic is what gets suppressed.
    """
    sc = _rev_scanner(ReversalShort)
    bars = [
        bar(0, 100.0, 106.0, 99.0, 100.5, 50_000, symbol="REV"),  # extreme=106 (stale), anchor=99
        bar(1, 100.5, 101.0, 98.0, 99.5, 50_000, symbol="REV"),  # anchor -> 98
        bar(2, 99.5, 99.6, 97.0, 97.5, 50_000, symbol="REV"),  # anchor -> 97
        bar(3, 97.5, 97.6, 95.0, 96.0, 50_000, symbol="REV"),  # anchor -> 95
        # Neither boundary moves -- close (96.5) sits near the anchor (95),
        # far from the stale extreme (106). Old code: retrace ~86%, dead.
        bar(4, 96.0, 97.0, 95.2, 96.5, 50_000, symbol="REV"),
        bar(5, 96.5, 96.6, 93.0, 93.5, 50_000, symbol="REV"),  # anchor -> 93
        # Same shape again -- old code would have killed the leg a second time.
        bar(6, 93.5, 94.0, 93.2, 93.8, 50_000, symbol="REV"),
    ]
    trace: dict[str, list[dict]] = {}
    for b in bars:
        sc.on_bar(b, trace=trace)
    check(
        "no phantom 'leg dead' events while the anchor is still catching up",
        [e["text"] for e in trace["REV-S"] if "dead" in e["text"]],
        [],
    )
    setup = sc._state[("REV", "REV-S")]
    # bar4 (appended after bar3's anchor move) and bar6 (appended after
    # bar5's) both survive -- neither was ever "dead", so neither gets
    # discarded, unlike the pre-existing anchor-move resets that keep
    # wiping bars 0-3 clean as the anchor keeps deepening.
    check(
        "the stale-extreme candidate bars (4, 6) still accumulate normally",
        [b.volume for b in setup._leg_bars],
        [50_000, 50_000],
    )

    # A genuinely fresh high arrives -- since the anchor hasn't moved since
    # bar 5 and nothing here was ever really dead, this extends the same
    # leg rather than resetting it.
    fired7 = sc.on_bar(bar(7, 93.8, 107.0, 93.5, 106.5, 5_000, symbol="REV"), trace=trace)
    check("no trigger yet (just a fresh extreme, no retrace checked)", fired7, [])
    check(
        "leg_bars keeps accumulating across the same anchor's whole span",
        [b.volume for b in setup._leg_bars],
        [50_000, 50_000, 5_000],
    )

    # And a real retrace against this genuine extreme still correctly dies.
    fired8 = sc.on_bar(bar(8, 106.5, 106.6, 95.0, 95.5, 5_000, symbol="REV"), trace=trace)
    check("a real retrace past max still kills the leg", fired8, [])
    check(
        "this time the death is real, not stale-arithmetic noise",
        any("dead" in e["text"] for e in trace["REV-S"] if e["time"].strftime("%H:%M") == "09:38"),
        True,
    )


def test_reversal_meets_ur_condition_flag() -> None:
    """meets_ur_condition (REV-L) is a recorded diagnostic against PDL --
    the retired U&R's own breach+reclaim condition -- not a filter."""
    levels_true = DailyLevels(
        "REV", date(2026, 8, 13), pdh=0.0, pdl=93.5, adr_pct=4.0, prev_close=100.0, adv=100_000_000.0
    )
    sc = Scanner({"REV": levels_true}, [ReversalLong])
    sc.curves["REV"] = _generous_curve()
    t = _feed(sc, _canonical_long_bars())[0]
    check(
        "extreme (93.0) < PDL (93.5) and close (94.0) > PDL -> True",
        t.extras["meets_ur_condition"],
        True,
    )

    levels_false = DailyLevels(
        "REV", date(2026, 8, 13), pdh=0.0, pdl=90.0, adr_pct=4.0, prev_close=100.0, adv=100_000_000.0
    )
    sc2 = Scanner({"REV": levels_false}, [ReversalLong])
    sc2.curves["REV"] = _generous_curve()
    t2 = _feed(sc2, _canonical_long_bars())[0]
    check(
        "extreme (93.0) is not below PDL (90.0) -> False",
        t2.extras["meets_ur_condition"],
        False,
    )


def test_reversal_meets_of_condition_flag() -> None:
    """meets_of_condition (REV-S) mirrors meets_ur_condition against PDH."""
    levels = DailyLevels(
        "REV", date(2026, 8, 13), pdh=107.0, pdl=0.0, adr_pct=4.0, prev_close=100.0, adv=100_000_000.0
    )
    sc = Scanner({"REV": levels}, [ReversalShort])
    sc.curves["REV"] = _generous_curve()
    t = _feed(sc, _canonical_short_bars())[0]
    check(
        "extreme (107.7) > PDH (107.0) and close (106.5) < PDH -> True",
        t.extras["meets_of_condition"],
        True,
    )


def test_capitulation_alert_fires_once_per_leg() -> None:
    """A leg already clearing the trigger's own leg-quality bar (size,
    duration, volume, EMA extension, acceleration) gets a one-time alert
    while it is still extending -- and only once for the whole leg, even
    if the extreme goes on to deepen further afterward, since that's still
    the same push (no death in between). Real case that motivated this:
    FEIM 2026-09-11 alerted three times in three minutes (09:37-09:40) as
    one up-move kept setting marginally deeper highs -- noise, not three
    separate heads-ups.
    """
    sc = _rev_scanner(ReversalLong)
    bars = _canonical_long_bars() + [
        bar(6, 94.0, 94.2, 93.9, 94.1, 5_000, symbol="REV"),  # more retrace, no boundary move
        # deeper extreme, but the SAME push (no death since bar 4) --
        # must NOT alert again, even though this bar alone would clear
        # every leg-quality floor on its own.
        bar(7, 94.1, 94.3, 88.0, 88.5, 200_000, symbol="REV"),
    ]
    seen = []
    for b in bars:
        sc.on_bar(b)
        seen.append(len(sc.pop_capitulation_alerts()))
    check(
        "only one alert, at the first bar clearing every floor (minute 4) -- "
        "none at the deeper extreme (minute 7), same push",
        seen,
        [0, 0, 0, 0, 1, 0, 0, 0],
    )
    check("full-history list has just the one", len(sc.capitulation_alerts), 1)
    check("alert's leg matches the first extreme, not the later deeper one", sc.capitulation_alerts[0].leg_extreme, 93.0)


def test_capitulation_alert_rearms_after_a_genuine_death_and_revival() -> None:
    """Unlike a still-climbing leg (see test_capitulation_alert_fires_once_per_leg),
    a leg that dies (retrace > 50%) and later revives with a deeper
    extreme *is* a new push (push_started_at moves to the revival's own
    turnaround -- see _push_started_at) and gets its own fresh alert.
    Reuses the same fixture test_reversal_leg_bars_reset_on_rearm_after_dead_leg
    verifies leg_bars/accel_vol against.
    """
    sc = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_price": 0.0})
    bars = [
        bar(0, 98.0, 98.0, 97.5, 97.8, 500_000, symbol="REV"),
        bar(1, 97.8, 98.0, 96.0, 96.2, 500_000, symbol="REV"),
        bar(2, 96.2, 96.5, 95.0, 95.3, 500_000, symbol="REV"),
        bar(3, 95.3, 96.0, 94.0, 94.3, 500_000, symbol="REV"),  # leg1 extreme 94.0 -- alerts
        bar(4, 94.3, 95.0, 93.0, 93.3, 500_000, symbol="REV"),  # leg1 deepens to 93.0
        bar(5, 93.3, 98.0, 93.5, 98.0, 500_000, symbol="REV"),  # retrace 100% -- kills leg1
        bar(6, 98.0, 98.0, 96.0, 96.5, 500_000, symbol="REV"),
        bar(7, 96.5, 96.8, 96.0, 96.6, 500_000, symbol="REV"),
        bar(8, 96.6, 96.9, 96.0, 96.7, 500_000, symbol="REV"),
        bar(9, 96.7, 97.0, 96.0, 96.8, 500_000, symbol="REV"),
        bar(10, 96.8, 97.1, 96.0, 96.9, 500_000, symbol="REV"),
        bar(11, 96.9, 97.0, 92.0, 92.3, 5_000, symbol="REV"),
        bar(12, 92.3, 92.7, 90.5, 90.8, 10_000, symbol="REV"),
        bar(13, 90.8, 91.0, 89.0, 89.3, 20_000, symbol="REV"),
        bar(14, 89.3, 89.6, 87.5, 87.8, 30_000, symbol="REV"),  # leg2 extreme 87.5 -- alerts again
        bar(15, 87.8, 88.0, 86.0, 86.3, 40_000, symbol="REV"),  # leg2 deepens to 86.0
    ]
    alerts = []
    for b in bars:
        sc.on_bar(b)
        alerts.extend(sc.pop_capitulation_alerts())
    check("one alert for leg1, one for the revived leg2", len(alerts), 2)
    check("first alert is leg1's own extreme, not leg2's", alerts[0].leg_extreme, 94.0)
    check("second alert is leg2's own extreme, not a re-alert of leg1", alerts[1].leg_extreme, 87.5)


def test_capitulation_alert_gated_by_leg_quality() -> None:
    """The alert requires the same leg-size, RVOL, and duration floors the
    final trigger does -- reusing the fixtures that isolate each of those
    filters above."""
    levels_wide_adr = DailyLevels(
        "REV", date(2026, 8, 13), 0.0, 0.0, adr_pct=20.0, prev_close=100.0, adv=100_000_000.0
    )
    sc_adr = Scanner({"REV": levels_wide_adr}, [ReversalLong])
    sc_adr.curves["REV"] = _generous_curve()
    alerts = []
    for b in _canonical_long_bars():
        sc_adr.on_bar(b)
        alerts.extend(sc_adr.pop_capitulation_alerts())
    check("leg too small relative to ADR never alerts", alerts, [])

    demanding_curve = VolumeCurve(
        "REV", sessions=10,
        cum_volume_by_minute={(OPEN + timedelta(minutes=4)).timetz().replace(tzinfo=None): 10_000_000.0},
    )
    sc_rvol = _rev_scanner(ReversalLong, curve=demanding_curve)
    alerts = []
    for b in _canonical_long_bars():
        sc_rvol.on_bar(b)
        alerts.extend(sc_rvol.pop_capitulation_alerts())
    check("thin RVOL relative to average never alerts", alerts, [])

    sc_accel = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_price": 10.0})
    alerts = []
    for b in _canonical_long_bars():
        sc_accel.on_bar(b)
        alerts.extend(sc_accel.pop_capitulation_alerts())
    check("leg not accelerating enough never alerts", alerts, [])

    fast_bars = [
        bar(0, 98.0, 98.0, 97.5, 97.8, 50_000, symbol="REV"),
        bar(1, 97.8, 97.9, 95.0, 95.3, 50_000, symbol="REV"),
        bar(2, 95.3, 95.4, 93.0, 93.3, 50_000, symbol="REV"),  # extreme at minute 2 -- leg built in 2.0min
    ]
    sc_fast = _rev_scanner(ReversalLong)
    alerts = []
    for b in fast_bars:
        sc_fast.on_bar(b)
        alerts.extend(sc_fast.pop_capitulation_alerts())
    check("leg built too fast (< 4-minute floor) never alerts", alerts, [])

    levels = DailyLevels(
        "REV", date(2026, 8, 13), 0.0, 0.0, adr_pct=4.0, prev_close=100.0, adv=100_000_000.0
    )
    # Isolating min_leg_minutes specifically: fast_bars' 3-bar leg has too
    # few bars to form an "earlier" accel window at all (falls back to the
    # neutral 1.0), so min_accel_price/min_accel_vol are loosened here too
    # -- this test is about the duration floor alone, not accel.
    sc_loose = Scanner(
        {"REV": levels}, [ReversalLong],
        {"REV-L": {"min_leg_minutes": 0.0, "min_accel_price": 0.0, "min_accel_vol": 0.0}},
    )
    sc_loose.curves["REV"] = _generous_curve()
    alerts = []
    for b in fast_bars:
        sc_loose.on_bar(b)
        alerts.extend(sc_loose.pop_capitulation_alerts())
    check("same fast leg alerts once the duration floor is loosened", len(alerts), 1)


def _profile_fixture(minutes: int = 20, avg_per_minute: float = 3500.0):
    """A session that pushes up for 12 minutes on heavy volume, then fades,
    against a flat historical pace of `avg_per_minute` shares."""
    from astor.core.types import VolumeCurve

    bars, curve_map, px, cum = [], {}, 10.00, 0.0
    for i in range(minutes):
        o = px
        px = round(px + (0.06 if i < 12 else -0.03), 2)
        start = OPEN + timedelta(minutes=i)
        bars.append(
            BarEvent("RUM", start, start + timedelta(minutes=1), o,
                     max(o, px) + 0.02, min(o, px) - 0.02, px,
                     4000.0 + (6000.0 if 5 <= i < 12 else 0.0))
        )
        cum += avg_per_minute
        curve_map[start.timetz().replace(tzinfo=None)] = cum
    levels = DailyLevels("RUM", date(2026, 8, 13), pdh=10.40, pdl=9.70,
                         adr_pct=6.0, prev_close=10.00, adr_sessions=14, adv=42_000_000.0)
    return bars, levels, VolumeCurve("RUM", 20, curve_map)


def test_compute_interval_rvol() -> None:
    from astor.core.indicators import compute_interval_rvol
    from astor.core.types import VolumeCurve

    # Flat 1,000-share/minute historical pace.
    curve = VolumeCurve(
        "RUM", 20,
        {(OPEN + timedelta(minutes=i)).timetz().replace(tzinfo=None): 1000.0 * (i + 1)
         for i in range(10)},
    )
    check(
        "a 5-min window trading double its usual 5,000 reads 2.0",
        compute_interval_rvol(10_000, OPEN + timedelta(minutes=5), OPEN + timedelta(minutes=9), curve),
        2.0,
    )
    # The session's first window has nothing before it: the average
    # cumulative volume at its last minute IS its expected window volume.
    check(
        "first window differences against zero, not against a missing key",
        compute_interval_rvol(10_000, OPEN, OPEN + timedelta(minutes=4), curve),
        2.0,
    )
    check(
        "no curve coverage past the end -> None, not a fabricated number",
        compute_interval_rvol(10_000, OPEN + timedelta(minutes=20), OPEN + timedelta(minutes=24), curve),
        None,
    )
    # A hole in the curve right before the window would otherwise charge
    # this window with the missing minutes' volume.
    holed = VolumeCurve("RUM", 20, {k: v for i, (k, v) in enumerate(curve.cum_volume_by_minute.items()) if i != 4})
    check(
        "a gap before the window -> None rather than a silent overcount",
        compute_interval_rvol(10_000, OPEN + timedelta(minutes=5), OPEN + timedelta(minutes=9), holed),
        None,
    )


def test_build_profile_rows() -> None:
    from astor.cli import build_profile_rows

    bars, levels, curve = _profile_fixture()
    rows, empty = build_profile_rows(bars, levels, curve, 5)

    check("20 minutes at 5-min intervals -> 4 rows", len(rows), 4)
    check("buckets anchored on the 09:30 open", [r.at.strftime("%H:%M") for r in rows],
          ["09:30", "09:35", "09:40", "09:45"])
    check("no gaps in a continuous session", empty, 0)
    first = rows[0]
    check("interval volume is the bucket's own", first.volume, 20_000.0)
    check("cumulative volume matches at the first bucket", first.cum_volume, 20_000.0)
    check("interval RVOL: 20k against 5 x 3.5k", first.interval_rvol, 1.14)
    check("session RVOL agrees at the first bucket", first.rvol, 1.14)
    # 09:35-09:39 runs 10k/min against the same 3.5k pace.
    check("interval RVOL tracks the burst", rows[1].interval_rvol, 2.86)
    check("session RVOL lags it, as a cumulative measure should", rows[1].rvol, 2.00)
    check("close is the bucket's last close", first.close, 10.30)
    # Today's own range, high/low - 1: 10.76 high, 10.00 low at this point.
    check("session range is this day's own, not an average",
          round(rows[-1].session_range_pct, 2), 7.62)
    check("interval range is that bucket's own", round(rows[0].interval_range_pct, 2), 3.41)
    check("move from the session open", round(first.from_open_pct, 2), 3.0)


def test_build_profile_rows_window_does_not_rebase_the_session() -> None:
    """--start/--end trim which rows print; RVOL and the session-to-date
    columns still accumulate from the open."""
    from astor.cli import build_profile_rows

    bars, levels, curve = _profile_fixture()
    full, _ = build_profile_rows(bars, levels, curve, 5)
    windowed, _ = build_profile_rows(bars, levels, curve, 5, time(9, 40), time(9, 50))

    check("window trims the rows", [r.at.strftime("%H:%M") for r in windowed], ["09:40", "09:45"])
    by_time = {r.at: r for r in full}
    for r in windowed:
        check(f"{r.at:%H:%M} RVOL unchanged by windowing", r.rvol, by_time[r.at].rvol)
        check(f"{r.at:%H:%M} cumulative volume unchanged", r.cum_volume, by_time[r.at].cum_volume)


def test_build_profile_rows_skips_zero_volume_and_counts_gaps() -> None:
    """Zero-volume padding is dropped exactly as Scanner drops it, so a
    minute nothing traded in neither moves the extremes nor counts as a
    traded interval -- the resulting hole is reported instead."""
    from astor.cli import build_profile_rows

    bars, levels, curve = _profile_fixture()
    # Blank out 09:35-09:39 entirely (zero volume, and a price spike that
    # must not reach session_high).
    padded = [
        BarEvent(b.symbol, b.start, b.end, b.open, 99.0, b.low, b.close, 0.0)
        if time(9, 35) <= b.start.time() < time(9, 40) else b
        for b in bars
    ]
    rows, empty = build_profile_rows(padded, levels, curve, 5)
    check("the dead interval produces no row", [r.at.strftime("%H:%M") for r in rows],
          ["09:30", "09:40", "09:45"])
    check("and is reported rather than silently closed up", empty, 1)
    check("the 99.00 zero-volume high never reached the session extremes",
          rows[-1].session_range_pct < 20.0, True)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
