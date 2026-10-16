"""Setup and scanner logic, verified against hand-built bars.

uv run python -m astor.test_setups
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astor.core.scanner import Scanner
from astor.core.setups import AVWAPTouchLong, AVWAPTouchShort, ReversalLong, ReversalShort
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
        on_setup_alert=alerts.append,
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


def test_scanner_backfill_bar_builds_setup_state_silently() -> None:
    """Backfill brings everything up to date -- indicators and setup state --
    without announcing anything. A symbol added mid-session has to judge its
    leg on the whole leg, not on the few bars since it was added, or filters
    computed from bar history (accel_price/accel_vol) read the wrong thing.
    """
    sc = Scanner({"TEST": LEVELS}, [ReversalLong])

    b0 = bar(0, 101, 101, 99, 99.5)
    sc.backfill_bar(b0)
    check("indicators updated by backfill", sc.indicators["TEST"].cum_volume, b0.volume)
    check("setup state now exists too", list(sc._state), [("TEST", "REV-L")])
    check("but nothing is recorded", sc.triggers, [])
    check("and nothing is announced", sc.pop_alerts(), [])
    check("backfilled bars counted apart from evaluated ones",
          (sc.bars_backfilled, sc.bars_evaluated), (1, 0))

    b1 = bar(1, 99.5, 100, 99.4, 99.8)
    sc.on_bar(b1)
    check("indicators keep accumulating across backfill and live bars",
          sc.indicators["TEST"].cum_volume, b0.volume + b1.volume)


def test_backfill_gives_a_mid_session_add_the_whole_leg() -> None:
    """The RXRX 2026-09-30 case: a leg whose quality can only be measured
    from its own bars. Added late, the setup used to see three bars, fall
    back to a neutral accel_price of 1.0, and be rejected by the 1.2 floor
    -- no alert, no trigger. Backfilled, it sees the leg.
    """
    bars = _canonical_long_bars()

    def run(backfill_through: int):
        sc = _rev_scanner(ReversalLong)
        for i, b in enumerate(bars):
            if i < backfill_through:
                sc.backfill_bar(b)
            else:
                sc.on_bar(b)
        return sc.triggers

    full = run(0)
    check("watched from the open, it fires", len(full), 1)
    # Joining at bar 4 -- one bar before the trigger -- still fires, and on
    # the same bar at the same price, because the leg behind it is intact.
    late = run(4)
    check("joined one bar before the trigger, it still fires", len(late), 1)
    check("same bar", late[0].bar_start, full[0].bar_start)
    check("same accel, measured off the whole leg",
          late[0].extras["accel_price"], full[0].extras["accel_price"])



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
    # 50 shares is $100 of distance alone; the $1 commission each way
    # leaves room for 49 ($98 + $2).
    check("shares = what $100 buys after commissions", shares, 49)
    check("dollars = 49 * entry", dollars, 4900.0)

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
    check("headline present", "TRIGGER" in line and "ABAT" in line, True)
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


def test_alert_line_is_an_order_ticket() -> None:
    """The notification carries what a resting stop order needs and nothing
    else -- the leg/touch diagnostics live in the store, not here."""
    from astor.core.types import CapitulationAlert
    from astor.drivers.live import _format_order_alert

    a = CapitulationAlert(
        symbol="ABAT", direction="short", bar_start=OPEN,
        bar_end=OPEN + timedelta(minutes=1), price=2.40,
        leg_extreme=2.62, anchor=2.95, leg_size_adr=1.25, move_rvol=2.5,
        ema9_extension_adr=0.6723, accel_price=4.6667, accel_vol=1.0,
        target=2.55, target_basis="retrace", stop=2.62, shares=1428,
        dollars=3641.4, max_loss_dollars=100.0,
        # Short from 2.55 with a 2.62 stop, expecting the day's low at 2.20:
        # 0.35 of reward against 0.07 of risk.
        exit_target=2.20, rr=5.0, pace_adr1=0.004, pace_adr=0.011,
    )
    line = _format_order_alert("CAPITULATION", a)
    # R:R is 9.0 by construction here; pace leads instead: 0.011 ADR(14)
    # a minute is 4.3x an average day's 1/390.
    check("pace leads, right after the direction", "short; pace " in line, True)
    check("as a multiple of an average day's", "4.3x" in line, True)
    check("no R:R", "R:R" in line, False)
    for needed in ("CAPITULATION", "ABAT", "short", "target: ", "2.55", "stop: ", "2.62", "1428"):
        check(f"{needed!r} present", needed in line, True)
    # A short's worse fill is a lower one.
    check("overshoot reads the short's way", "below target" in line, True)
    # Everything that explains *why* it fired is deliberately absent.
    for gone in ("move_rvol", "accel", "ema9_ext", "leg "):
        check(f"{gone!r} omitted", gone in line, False)


def test_commissions_fixed_pricing() -> None:
    """IBKR Fixed: $0.005 a share, $1 minimum, 1% of value maximum."""
    from astor.core.commissions import commission, shares_for_risk, trade_risk

    check("30 SKHY: the $1 minimum", commission(30, 156.11), 1.0)
    check("4,000 shares: per share", round(commission(-4000, 2.50), 2), 20.0)
    check("10 shares of a $5 stock: 1% cap", round(commission(10, 5.0), 2), 0.5)
    check("no shares, no commission", commission(0, 10.0), 0.0)
    check("risk = distance + both sides",
          round(trade_risk(4000, 2.50, 2.47), 2), 120.0 + 20.0 + 20.0)

    # A low-priced stock bought by the thousand: a 3c stop and $100 of risk
    # was 3,333 shares on distance alone; 1c a share in commissions makes
    # it 2,500 ($75 + $12.50 + $12.50).
    check("cheap stock sized down by commissions", shares_for_risk(2.50, 2.47, 100.0), 2500)
    for entry, stop, budget in [(2.5, 2.47, 100.0), (156.11, 157.89, 100.0), (7.35, 7.2, 37.0)]:
        n = shares_for_risk(entry, stop, budget)
        check(f"{n} fits, {n + 1} doesn't",
              (trade_risk(n, entry, stop) <= budget, trade_risk(n + 1, entry, stop) > budget),
              (True, True))
    check("too wide for one share", shares_for_risk(100.0, 50.0, 10.0), 0)


def test_slippage_widens_the_risk() -> None:
    """Half the spread at the stop counts like extra stop distance."""
    from astor.core.commissions import shares_for_risk, trade_risk

    # CCXI 2026-10-14: in at 10.83, stop 10.70, 1c half spread -- 14c a
    # share where the distance says 13c.
    check("slip adds per share", round(trade_risk(500, 10.83, 10.70, 0.01), 2), 70.0 + 2.5 + 2.5)
    check("no slip, no change", trade_risk(500, 10.83, 10.70), trade_risk(500, 10.83, 10.70, 0.0))
    check("sized down for the slip", shares_for_risk(10.83, 10.70, 100.0, 0.01) < shares_for_risk(10.83, 10.70, 100.0), True)
    n = shares_for_risk(10.83, 10.70, 100.0, 0.01)
    check(f"{n} fits with slip, {n + 1} doesn't",
          (trade_risk(n, 10.83, 10.70, 0.01) <= 100.0, trade_risk(n + 1, 10.83, 10.70, 0.01) > 100.0),
          (True, True))


def test_alert_line_sizes_for_the_spread() -> None:
    from astor.core.types import CapitulationAlert
    from astor.drivers.live import _format_order_alert

    a = CapitulationAlert(
        symbol="ABAT", direction="short", bar_start=OPEN,
        bar_end=OPEN + timedelta(minutes=1), price=2.40,
        leg_extreme=2.62, anchor=2.95, leg_size_adr=1.25, move_rvol=2.5,
        ema9_extension_adr=0.6723, accel_price=4.6667, accel_vol=1.0,
        target=2.55, target_basis="retrace", stop=2.62, shares=1228,
        dollars=3131.4, max_loss_dollars=100.0,
        exit_target=2.20, rr=5.0, pace_adr1=0.004, pace_adr=0.011,
    )
    # 7c of distance + 1c of half spread + 1c a share in commissions
    sized = _format_order_alert("C", a, slip=0.01)
    check("resized with the quote", ("1111" in sized, "1228" in sized, "no quote" in sized), (True, False, False))
    check("unshortable short says so",
          "not shortable" in _format_order_alert("C", a, slip=0.01, borrow="not shortable -- none to borrow"), True)
    no_quote = _format_order_alert("C", a)
    check("no quote: the setup's own size", "1228" in no_quote, True)
    check("and says so", "(no quote, no slippage)" in no_quote, True)


def test_overshoot_cost_scales_with_the_stock() -> None:
    """The step size is found, not assumed: a cheap stock on a tight stop
    loses shares by the cent, an expensive one by the dime."""
    from astor.drivers.live import _overshoot_cost

    # $100 of risk, 15c stop distance -> ~666 shares, and a cent of
    # overshoot costs dozens of them.
    step, lost = _overshoot_cost(target=7.35, stop=7.20, max_loss=100.0, direction="long")
    check("cheap stock reports in cents", step, 0.01)
    check("and loses many shares per cent", lost > 20, True)

    # Same risk budget, a $6 stop distance -> 16 shares, so a cent is
    # nothing and the step has to widen before it costs a whole share.
    step, lost = _overshoot_cost(target=700.0, stop=694.0, max_loss=100.0, direction="long")
    check("expensive stock needs a wider step", step >= 0.25, True)
    check("and loses at least one share there", lost >= 1, True)

    check("no size, nothing to say",
          _overshoot_cost(target=10.0, stop=10.0, max_loss=100.0, direction="long"), None)


def test_should_notify() -> None:
    """Trigger notifications are suspended (2026-09-24): the experiment is
    to act on the alert by resting a stop order at the price the trigger
    would fire, so a trigger announcement arriving afterwards is noise.
    Recording is unaffected -- this gates printing only."""
    from astor.core.types import Trigger
    from astor.drivers.live import NOTIFY_TRIGGERS, _should_notify

    def trigger_at(setup: str, hh: int, mm: int, grade: int | None) -> Trigger:
        start = datetime(2026, 8, 14, hh, mm, tzinfo=ET)
        return Trigger(
            symbol="TEST", setup=setup, direction="long", trigger_price=1.0,
            bar_start=start, bar_end=start + timedelta(minutes=1),
            level_name="EXT", level_value=1.0,
            extras={} if grade is None else {"auto_grade": grade},
        )

    check("suspended, so nothing notifies", NOTIFY_TRIGGERS, False)
    for hh, mm in [(9, 45), (11, 30), (14, 59)]:
        check(
            f"no trigger notification at {hh}:{mm:02d}",
            _should_notify(trigger_at("REV-L", hh, mm, 4)),
            False,
        )
    # The 15:00 cutoff is still in the expression, so turning notifications
    # back on restores the old behaviour rather than a different one.
    check("cutoff still applies underneath",
          _should_notify(trigger_at("REV-L", 15, 0, 4)), False)


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


def test_views_report_adr1_from_session_high_and_low() -> None:
    """ADR(1) -- the day's own realised range -- is computed in the views
    rather than read from extras, so it covers every row ever recorded
    instead of only the AVT rows written after the extras field existed.

    Not to be confused with adr_pct, which is ADR(14). The trigger side is
    fully retroactive because session_high/session_low have always been
    stored; the alert side cannot be, so alerts without them report NULL.
    """
    import tempfile

    from astor.core.store import TriggerStore
    from astor.core.types import CapitulationAlert, Trigger

    path = Path(tempfile.mkdtemp()) / "t.db"
    store = TriggerStore(path)
    store.start_run("r1", "replay", {})
    store.record(
        "r1",
        Trigger(
            symbol="TEST", setup="REV-L", direction="long", trigger_price=100.5,
            bar_start=OPEN, bar_end=OPEN + timedelta(minutes=1),
            level_name="EXT", level_value=100.0,
            # No day_range_pct in extras at all -- a REV trigger never had one.
            extras={"session_high": 110.0, "session_low": 100.0},
        ),
        OPEN + timedelta(minutes=1),
    )

    def alert(symbol: str, hi: float | None, lo: float | None) -> CapitulationAlert:
        return CapitulationAlert(
            symbol=symbol, direction="long",
            bar_start=OPEN, bar_end=OPEN + timedelta(minutes=1),
            price=100.5, leg_extreme=100.0, anchor=110.0, leg_size_adr=1.5,
            move_rvol=None, ema9_extension_adr=None, accel_price=None,
            accel_vol=None, target=None, target_basis="retrace",
            exit_target=None, rr=None, pace_adr1=None, pace_adr=None,
            stop=100.0, shares=None, dollars=None, max_loss_dollars=500.0,
            session_high=hi, session_low=lo,
        )

    def record(a: CapitulationAlert) -> None:
        store.record_alert(
            "r1", a.kind, a.symbol, a.direction, a.bar_start, a.price,
            a.bar_end, a.store_extras(), bar_end=a.bar_end,
        )

    record(alert("WITH", 110.0, 100.0))
    # The historical shape: recorded before session high/low were carried.
    record(alert("WITHOUT", None, None))
    store.commit()

    row = store.conn.execute(
        "SELECT day_range, day_range_pct FROM latest_triggers"
    ).fetchone()
    check("REV trigger gets day_range despite no extras field", row[0], 10.0)
    check("...and day_range_pct off session_low", round(row[1], 4), 10.0)

    rows = dict(
        store.conn.execute(
            "SELECT symbol, day_range FROM latest_alerts WHERE kind='capitulation'"
        ).fetchall()
    )
    check("alert carrying session high/low gets day_range", rows["WITH"], 10.0)
    check("alert without them reports NULL, not a guess", rows["WITHOUT"], None)
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


def _same_bar_long_bars(close: float) -> list[BarEvent]:
    """_canonical_long_bars, but the extreme bar is also the reversal: it
    opens at 94.2, flushes to the 93.0 extreme and closes at `close`."""
    bars = _canonical_long_bars()[:4]
    return bars + [bar(4, 94.2, 94.6, 93.0, close, 50_000, symbol="REV")]


def test_reversal_fires_on_the_extreme_bar_itself() -> None:
    # 93.0 -> 94.5 is 30% of the 5.0 leg, and the bar closed above its open
    triggers = _feed(_rev_scanner(ReversalLong), _same_bar_long_bars(94.5))
    check("fires on the extreme bar", len(triggers), 1)
    t = triggers[0]
    check("trigger_price", t.trigger_price, 94.5)
    check("level_value is the new extreme", t.level_value, 93.0)
    check("retrace_pct", t.extras["retrace_pct"], 30.0)
    check("retrace_minutes", t.extras["retrace_minutes"], 0.0)
    check("extreme_close is the bar before the reversal candle", t.extras["extreme_close"], 94.3)


def test_reversal_same_bar_fire_keeps_its_alert() -> None:
    # Triggers are not announced live, so the alert is the only place the
    # new 93.0 stop shows up -- it must not be dropped for the trigger.
    sc = _rev_scanner(ReversalLong)
    for b in _same_bar_long_bars(94.5):
        fired = sc.on_bar(b)
        alerts = sc.pop_alerts()
    check("fired on the last bar", len(fired), 1)
    check("its alert kept", [a.stop for a in alerts], [93.0])


def test_reversal_same_bar_needs_a_close_against_its_open() -> None:
    # Closes 94.1 -- 22% off the low, but below its 94.2 open
    triggers = _feed(_rev_scanner(ReversalLong), _same_bar_long_bars(94.1))
    check("no trigger", len(triggers), 0)


def test_reversal_same_bar_needs_the_min_retrace() -> None:
    # 94.3 open, 93.0 low, 93.4 close: green, but only 8% of the leg back
    bars = _canonical_long_bars()[:4] + [bar(4, 93.3, 94.4, 93.0, 93.4, 50_000, symbol="REV")]
    triggers = _feed(_rev_scanner(ReversalLong), bars)
    check("no trigger", len(triggers), 0)


def test_reversal_same_bar_short_mirrors_long() -> None:
    bars = _canonical_short_bars()[:4] + [bar(4, 106.5, 107.7, 105.9, 106.0, 50_000, symbol="REV")]
    triggers = _feed(_rev_scanner(ReversalShort), bars)
    check("fires on the extreme bar", len(triggers), 1)
    check("trigger_price", triggers[0].trigger_price, 106.0)
    check("level_value", triggers[0].level_value, 107.7)


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


def test_profile_header_helpers() -> None:
    from astor.cli import _human_dollars, _pct_change

    # Gap: the open against the prior close. Real case: INSP 2026-09-18
    # opened 72.29 against a 72.74 close.
    check("gap is signed, down", round(_pct_change(72.29, 72.74), 2), -0.62)
    check("gap is signed, up", round(_pct_change(75.0, 72.74), 2), 3.11)
    check("no reference price -> n/a rather than a division", _pct_change(72.29, 0.0), None)

    check("millions, two decimals", _human_dollars(24_294_254.0), "$24.29M")
    check("billions once past the boundary", _human_dollars(1_500_000_000.0), "$1.50B")
    check("exactly a billion reads as billions, not $1000.00M",
          _human_dollars(1_000_000_000.0), "$1.00B")
    check("just under stays in millions", _human_dollars(999_999_999.0), "$1000.00M")


def test_session_is_complete_guards_the_intraday_cache() -> None:
    """A day still being traded must never be written to the cache: IBKR
    answers with the session so far, and a cache entry is never revisited
    once written. Real case: INSP 2026-09-18 cached at 10:32 with 63 bars,
    so every later replay/explain/profile of that date saw a 10:32 close.
    """
    from astor.drivers.replay import session_is_complete

    session = date(2026, 9, 18)

    def at(hh: int, mm: int, day: int = 18):
        return datetime(2026, 9, day, hh, mm, tzinfo=ET)

    check("mid-session", session_is_complete(session, at(10, 32)), False)
    check("one minute before the close", session_is_complete(session, at(15, 59)), False)
    # The 15:59 bar completes at 16:00 -- not a minute before.
    check("at the close", session_is_complete(session, at(16, 0)), True)
    check("after the close", session_is_complete(session, at(18, 30)), True)
    check("any earlier session is done", session_is_complete(session, at(9, 31, day=19)), True)
    check("a future session has nothing yet", session_is_complete(session, at(16, 0, day=17)), False)


def _avt_scanner(setup_cls, symbol: str = "AVT", setup_kwargs: dict | None = None) -> Scanner:
    """ADR 2% of a $100 prior close, so 1 ADR = $2.00: the touch band is
    +/-$0.12, the recovery line $0.24 past AVWAP, and a close more than
    $0.12 the wrong side kills the leg."""
    levels = DailyLevels(
        symbol, date(2026, 8, 13), 0.0, 0.0, adr_pct=2.0, prev_close=100.0, adv=100_000_000.0
    )
    scanner = Scanner({symbol: levels}, [setup_cls],
                      {setup_cls.name: setup_kwargs} if setup_kwargs else None)
    scanner.curves[symbol] = _generous_curve(symbol)
    return scanner


def _avt_long_bars() -> list[BarEvent]:
    """A run up to 104.0 on volume, a pullback that touches AVWAP (which sits
    near 101 after the push), then a bar that closes back above it far enough
    to be the continuation. Deliberately built so the touch bar itself is NOT
    already recovered -- that is the rule most of these tests turn on.
    """
    return [
        bar(30, 100.0, 100.5, 99.5, 100.0, 50_000, symbol="AVT"),
        bar(31, 100.0, 102.0, 100.0, 102.0, 50_000, symbol="AVT"),
        bar(32, 102.0, 104.0, 102.0, 103.8, 50_000, symbol="AVT"),   # day high 104.0
        bar(33, 103.8, 103.9, 102.5, 102.6, 50_000, symbol="AVT"),   # pulling back, still well above
        bar(34, 102.6, 102.6, 101.85, 102.0, 30_000, symbol="AVT"),  # touch: low 0.04 ADR off AVWAP
        bar(35, 102.0, 103.0, 102.0, 102.9, 60_000, symbol="AVT"),   # crosses back out: the entry
    ]


def test_avt_long_fires_on_the_continuation() -> None:
    sc = _avt_scanner(AVWAPTouchLong)
    triggers = _feed(sc, _avt_long_bars())
    check("one trigger", len(triggers), 1)
    t = triggers[0]
    check("fires on the continuation bar", t.bar_start.strftime("%H:%M"), "10:05")
    check("setup", t.setup, "AVT-L")
    check("entry is that bar's close", t.trigger_price, 102.9)
    e = t.extras
    check("touch is inside the band", abs(e["touch_adr"]) <= 0.06, True)
    check("depth clears its floor", e["depth_adr"] >= 0.15, True)
    check("recovery clears its floor", e["recovery_adr"] >= 0.12, True)
    check("the touch it reports is the pullback's own low, not this bar's",
          e["touch_at"].endswith("10:04:00-04:00"), True)
    check("...and that bar's price", e["touch_price"], 101.85)


def test_avt_requires_a_transition_not_a_state() -> None:
    """The rule that killed 2026-09-18 FIVN 09:42 and 14:52: a bar already
    sitting beyond the recovery threshold before the touch is not a
    continuation, however well it scores on distance."""
    sc = _avt_scanner(AVWAPTouchLong)
    bars = _avt_long_bars()
    # Same shape, except the touch bar itself already closes past the
    # recovery line -- so nothing crosses on the next bar.
    bars[4] = bar(34, 102.6, 103.1, 101.85, 103.0, 30_000, symbol="AVT")
    check("no trigger when price never crosses in", _feed(sc, bars), [])


def test_avt_touch_must_reach_the_band() -> None:
    sc = _avt_scanner(AVWAPTouchLong)
    bars = _avt_long_bars()
    # Pullback stops well short of AVWAP: no touch, so no trigger however
    # strong the bar that follows.
    bars[4] = bar(34, 102.6, 102.7, 102.45, 102.5, 30_000, symbol="AVT")
    bars[5] = bar(35, 102.7, 103.5, 102.7, 103.4, 60_000, symbol="AVT")
    check("a pullback that never reaches AVWAP does not arm it", _feed(sc, bars), [])


def test_avt_close_through_avwap_kills_the_leg() -> None:
    """This is a touch setup, not an AVWAP breakdown: a close too far the
    wrong side ends the leg until a new extreme."""
    sc = _avt_scanner(AVWAPTouchLong)
    bars = _avt_long_bars()
    bars[4] = bar(34, 102.6, 102.6, 101.0, 101.2, 30_000, symbol="AVT")  # closes well below
    check("no trigger after a real break of AVWAP", _feed(sc, bars), [])


def test_avt_leg_cannot_die_before_the_touch_floor() -> None:
    """Before the floor AVWAP is a few bars' average and the day's range a
    bar or two wide, so a "break" then is noise. TMUS 2026-10-09 died at
    09:32 off a low set at the open, and stayed dead all day."""
    bars = _avt_long_bars()
    # 10:03 closes well under AVWAP, then 10:04 touches and 10:05 continues
    bars[3] = bar(33, 103.8, 103.9, 100.9, 101.0, 50_000, symbol="AVT")
    check(
        "a break after the floor kills the leg",
        _feed(_avt_scanner(AVWAPTouchLong, setup_kwargs={"earliest_touch": time(9, 34)}), bars),
        [],
    )
    fired = _feed(_avt_scanner(AVWAPTouchLong, setup_kwargs={"earliest_touch": time(10, 4)}), bars)
    check("the same break before the floor does not",
          [t.bar_start.strftime("%H:%M") for t in fired], ["10:05"])


def test_avt_rearm_lets_the_next_interaction_fire() -> None:
    """A leg killed by a break comes back once price leaves AVWAP the
    trade's way again (rearm_adr, 0.15 since 2026-10-12): the next touch is
    judged on its own. Turned off, the same bars wait for a new extreme."""
    bars = _avt_long_bars()[:4] + [
        bar(34, 102.6, 102.6, 101.0, 101.2, 30_000, symbol="AVT"),   # breaks AVWAP: dead
        bar(35, 101.2, 102.9, 101.2, 102.8, 60_000, symbol="AVT"),   # leaves it the trade's way
        bar(36, 102.8, 102.8, 101.9, 102.1, 30_000, symbol="AVT"),   # touches again
        bar(37, 102.1, 103.0, 102.1, 102.95, 60_000, symbol="AVT"),  # continues
    ]
    check("current default", AVWAPTouchLong().rearm_adr, 0.15)
    off = _avt_scanner(AVWAPTouchLong, setup_kwargs={"rearm_adr": None})
    check("off: waits for a new extreme", _feed(off, bars), [])
    fired = _feed(_avt_scanner(AVWAPTouchLong), bars)
    check("on: the second touch fires", [t.bar_start.strftime("%H:%M") for t in fired], ["10:07"])
    too_close = _avt_scanner(AVWAPTouchLong, setup_kwargs={"rearm_adr": 0.5})
    check("not re-armed when price never gets that far from AVWAP", _feed(too_close, bars), [])


def test_avt_recovery_must_land_within_the_bar_window() -> None:
    sc = _avt_scanner(AVWAPTouchLong, setup_kwargs={"max_bars_since_touch": 1})
    bars = _avt_long_bars()
    drift = [
        bar(35, 102.0, 102.1, 101.45, 101.70, 20_000, symbol="AVT"),  # neither touch nor recovery
        bar(36, 101.7, 101.8, 101.45, 101.70, 20_000, symbol="AVT"),
        bar(37, 102.4, 103.2, 102.4, 103.1, 60_000, symbol="AVT"),    # 3 bars after the touch
    ]
    check("a stale pullback does not fire", _feed(sc, bars[:5] + drift), [])
    check("...but the same bars fire with the default 5-bar window",
          len(_feed(_avt_scanner(AVWAPTouchLong), bars[:5] + drift)), 1)


def test_avt_stop_sits_the_far_side_of_avwap() -> None:
    """The stop is a fraction of today's range beyond AVWAP, not a function
    of the pullback's low -- the thesis is that AVWAP held, so that is where
    being wrong is measured. It is also exactly where max_break_adr calls
    the leg dead, so stop and invalidation agree by construction."""
    sc = _avt_scanner(AVWAPTouchLong)
    t = _feed(sc, _avt_long_bars())[0]
    avwap, day_range = t.extras["avwap"], 4.5  # 104.00 high, 99.50 low
    check("stop is stop_adr of today's range below AVWAP",
          round(t.level_value, 4), round(avwap - 0.05 * day_range, 4))
    check("named for what it is anchored to", t.level_name, "AVW")
    check("and it sits below the entry", t.level_value < t.trigger_price, True)

    # Widen it and the stop moves with it; nothing about the pullback changes.
    wide = _feed(_avt_scanner(AVWAPTouchLong, setup_kwargs={"stop_adr": 0.20}), _avt_long_bars())[0]
    check("a wider stop_adr moves the stop, not the entry",
          (round(wide.level_value, 4), wide.trigger_price),
          (round(avwap - 0.20 * day_range, 4), t.trigger_price))


def test_avt_raises_a_touch_alert_before_the_continuation() -> None:
    """The heads-up fires on the touch bar itself, one per pullback, and
    carries the stop and size so there is nothing left to work out when the
    continuation prints."""
    sc = _avt_scanner(AVWAPTouchLong)
    bars = _avt_long_bars()
    alerts, triggers = [], []
    for b in bars:
        triggers.extend(sc.on_bar(b))
        alerts.extend(sc.pop_alerts())

    check("one alert", len(alerts), 1)
    a = alerts[0]
    check("kind", a.kind, "avwap_touch")
    check("raised on the touch bar, before the trigger", a.bar_start, bars[4].start)
    check("...which is a bar earlier than the trigger", triggers[0].bar_start, bars[5].start)
    check("carries a stop", a.stop < a.price, True)
    check("and the size that stop allows", a.shares is not None, True)
    check("touch distance is inside the band", abs(a.touch_adr) <= 0.05, True)

    # A second in-band bar in the same pullback does not re-alert.
    check("still just the one alert", len([x for x in alerts if x.kind == "avwap_touch"]), 1)


def test_avt_touch_alert_needs_the_same_day_quality_as_a_trigger() -> None:
    """A touch on a day not worth joining is not worth interrupting anyone
    for, so the alert is gated on the range and volume filters too."""
    sc = _avt_scanner(AVWAPTouchLong, setup_kwargs={"min_day_range_adr": 99.0})
    alerts = []
    for b in _avt_long_bars():
        sc.on_bar(b)
        alerts.extend(sc.pop_alerts())
    check("no alert when the day is too quiet to trade", alerts, [])


def test_avt_target_is_measured_from_the_touch() -> None:
    """A pullback that only grazed the band was already most of the way
    back; asking it for a fixed distance past AVWAP gave up the entry. The
    target is recovery_adr past the touch -- or past AVWAP when the touch
    overshot through it, since price has to reclaim the line first."""
    from astor.core.setups import AVWAPTouchLong, AVWAPTouchShort

    long_setup, short_setup = AVWAPTouchLong(), AVWAPTouchShort()
    avwap, yard = 100.0, 10.0  # recovery_adr 0.05 -> half a point

    # Long, touch above AVWAP: the touch is the anchor, so the target sits
    # closer than the old AVWAP + 0.10 x yard (101.00) did.
    check("grazed the band from above", long_setup._target_from_touch(100.4, avwap, yard), 100.9)
    # Long, touch below AVWAP: AVWAP is the anchor instead.
    check("overshot through AVWAP", long_setup._target_from_touch(99.6, avwap, yard), 100.5)
    # Short mirrors both.
    check("short, grazed from below", short_setup._target_from_touch(99.6, avwap, yard), 99.1)
    check("short, overshot through", short_setup._target_from_touch(100.4, avwap, yard), 99.5)


def test_avt_stop_and_invalidation_are_the_same_line() -> None:
    """The bar that stops the trade out is the bar that ends the leg. These
    drifted apart once (stop moved to 0.05 while the break stayed 0.06),
    which would let a leg stop you out and then offer a second entry off
    the same pullback -- so the invariant is pinned here rather than left
    to the defaults happening to match."""
    from astor.core.setups import AVWAPTouchLong, AVWAPTouchShort

    for cls in (AVWAPTouchLong, AVWAPTouchShort):
        s = cls()
        check(f"{cls.name}: stop and break are one distance",
              s.stop_adr, s.max_break_adr)
        # ...and on the same side of AVWAP, so they describe one price.
        avwap, yard = 100.0, 10.0
        stop = s._stop_from_avwap(avwap, yard)
        breach = avwap - s.max_break_adr * yard if cls.direction == "long" else (
            avwap + s.max_break_adr * yard)
        check(f"{cls.name}: at the same price", round(stop, 6), round(breach, 6))


def test_avt_yardstick_is_capped_on_a_wide_day() -> None:
    """Past a certain width the pullback geometry stops scaling with the
    day: a tenth of a 25% range is a vast distance to ask price to travel,
    and a stop that far from AVWAP is not a tight stop. The cap is what
    'switch to fixed percents above 10%' means -- 0.05 of a 10% range is
    0.5%, so the two descriptions are one rule."""
    from astor.core.setups import AVWAPTouchLong

    s = AVWAPTouchLong()
    low, avwap = 100.0, 110.0

    # A 6% day: under the cap, so distances scale with the range.
    narrow = min(6.0, s.max_yard_pct / 100 * low)
    check("narrow day uses its own range", narrow, 6.0)
    check("...and the stop with it", s._stop_from_avwap(avwap, narrow), 110.0 - 0.05 * 6.0)

    # A 25% day: capped at 10% of price, so the stop stays 0.5% of price.
    wide = min(25.0, s.max_yard_pct / 100 * low)
    check("wide day is capped", wide, 10.0)
    check("stop is 0.5% of price, not 1.25%", s._stop_from_avwap(avwap, wide), 110.0 - 0.5)


def test_avt_day_qualifies_on_adr_percent_or_the_overnight_gap() -> None:
    """Same two-route size test Reversal uses, and the gap counts: a name
    that gapped 0.4x ADR and has added 0.4x since the open has travelled
    0.8x from where it last closed."""
    from astor.core.setups import _leg_is_big_enough

    adr, low = 1.0, 50.0  # 1.0 ADR(14); 6% of 50 is 3.0

    # Route one: the plain ADR multiple.
    check("0.8x ADR qualifies", _leg_is_big_enough(0.8, adr, low, 0.8, 6.0, 0.6), True)
    check("0.7x alone does not", _leg_is_big_enough(0.7, adr, low, 0.8, 6.0, 0.6), False)
    # Route two: a big percent with the 0.6x floor still under it.
    check("0.7x and 6% qualifies", _leg_is_big_enough(3.5, 5.0, low, 0.8, 6.0, 0.6), True)
    check("percent alone does not clear the floor",
          _leg_is_big_enough(3.5, 10.0, low, 0.8, 6.0, 0.6), False)

    # The gap: intraday range 0.4x ADR, but 0.8x from the prior close.
    # Numbers kept clear of the threshold: 50.85 - 50.0 is 0.85 in decimal
    # but lands a hair under 0.8 if you pick 50.8, and a test should not
    # hinge on binary float representation.
    prev_close, session_high, session_low = 50.0, 50.85, 50.4
    day_range = session_high - session_low
    qualifying = max(day_range, abs(session_high - prev_close))
    check("intraday range alone falls short",
          _leg_is_big_enough(day_range, adr, session_low, 0.8, 6.0, 0.6), False)
    check("counting the gap, it qualifies",
          _leg_is_big_enough(qualifying, adr, session_low, 0.8, 6.0, 0.6), True)


def test_avt_speaks_once_per_depth_until_the_pullback_goes_deeper() -> None:
    """A leg nudging AVWAP at the same depth over and over is one setup,
    not six. ALGM 2026-10-02 fired twelve AVT-L triggers and WOLF four in
    seven minutes, all off pullbacks no deeper than the one before."""
    bars = _avt_long_bars()
    # After the trigger at 10:05, another pullback to the same place: it
    # touches, it recovers, and it is the same setup replayed.
    again = [
        bar(36, 102.9, 103.0, 101.85, 102.0, 30_000, symbol="AVT"),   # same depth
        bar(37, 102.0, 103.0, 102.0, 102.9, 60_000, symbol="AVT"),    # would fire
    ]

    def run(extra, **kw):
        sc = _avt_scanner(AVWAPTouchLong, setup_kwargs=kw or None)
        return [t.bar_start.strftime("%H:%M") for t in _feed(sc, bars + extra)]

    check("a repeat at the same depth is suppressed", run(again), ["10:05"])
    check("the rule can be switched off",
          len(run(again, require_deeper_touch=False)), 2)

    # The gate itself. Tested directly rather than through a fixture,
    # because constructing a *deeper* second touch that is still inside the
    # band is often impossible by then: AVWAP has drifted toward price, so
    # going deeper means leaving the band. That interaction makes the rule
    # stricter in practice than it reads -- in an uptrend the usual way a
    # second setup qualifies is a fresh extreme, which resets the gate
    # outright rather than having to clear it.
    from datetime import datetime

    s = AVWAPTouchLong()
    check("nothing has spoken yet, so anything may", s._may_speak(0.20), True)
    s._pullback_bar = bars[4]
    s._mark_spoken(0.30)
    check("the same pullback may speak again (alert, then its trigger)",
          s._may_speak(0.30), True)
    s._pullback_bar = bars[5]  # a different pullback
    check("a shallower one may not", s._may_speak(0.25), False)
    check("one at the same depth may not", s._may_speak(0.30), False)
    check("a deeper one may", s._may_speak(0.31), True)


def test_avt_short_mirrors_long() -> None:
    sc = _avt_scanner(AVWAPTouchShort)
    bars = [
        bar(30, 100.0, 100.5, 99.5, 100.0, 50_000, symbol="AVT"),
        bar(31, 100.0, 100.0, 98.0, 98.0, 50_000, symbol="AVT"),
        bar(32, 98.0, 98.0, 96.0, 96.2, 50_000, symbol="AVT"),      # day low 96.0
        bar(33, 96.2, 97.5, 96.1, 97.4, 50_000, symbol="AVT"),      # pulling back up
        bar(34, 97.4, 98.15, 97.4, 98.0, 30_000, symbol="AVT"),     # touch: high 0.04 ADR off AVWAP
        bar(35, 98.0, 98.0, 97.0, 97.1, 60_000, symbol="AVT"),      # crosses back out: the entry
    ]
    triggers = _feed(sc, bars)
    check("one trigger", len(triggers), 1)
    check("setup", triggers[0].setup, "AVT-S")
    check("short direction", triggers[0].direction, "short")
    check("stop sits above the entry", triggers[0].level_value > triggers[0].trigger_price, True)


def test_avt_touch_requires_the_session_to_be_going_the_trade_s_way() -> None:
    """A continuation setup needs something to continue: the day has to be
    up on its open for a long, down for a short."""
    sc = _avt_scanner(AVWAPTouchLong)
    bars = _avt_long_bars()
    # Same shape, shifted so the whole move happens *below* the session open:
    # the pullback still touches AVWAP, but the day is red.
    down = [
        bar(30, 106.0, 106.5, 105.5, 106.0, 50_000, symbol="AVT"),  # opens at 106
        bar(31, 106.0, 106.0, 104.0, 104.0, 50_000, symbol="AVT"),
        bar(32, 104.0, 104.0, 102.0, 102.2, 50_000, symbol="AVT"),
        bar(33, 102.2, 103.5, 102.1, 103.4, 50_000, symbol="AVT"),
        bar(34, 103.4, 103.5, 102.85, 103.0, 30_000, symbol="AVT"),
        bar(35, 103.0, 104.0, 103.0, 103.9, 60_000, symbol="AVT"),
    ]
    check("no long trigger while the day is below its open", _feed(sc, down), [])
    # The control: the same geometry with the day green does fire.
    check("the with-session version fires", len(_feed(_avt_scanner(AVWAPTouchLong), bars)), 1)


def test_avt_ignores_touches_before_the_time_floor() -> None:
    """AVWAP sits on top of price for the first few minutes, so everything
    "touches" it before the day has shape.

    The floor's *value* is a judgement that keeps moving -- 09:40, then
    09:37, now 09:34 -- so it is asserted once, separately, and the
    behaviour is exercised against an explicit floor that does not care
    what the default happens to be this week.
    """
    from astor.core.setups import AVWAPTouchLong

    check("current default", AVWAPTouchLong().earliest_touch, time(9, 34))

    bars = _avt_long_bars()  # touch at 10:04, continuation at 10:05
    check(
        "a floor past the touch suppresses it",
        _feed(_avt_scanner(AVWAPTouchLong, setup_kwargs={"earliest_touch": time(10, 10)}), bars),
        [],
    )
    check(
        "...and a floor before it does not",
        len(_feed(_avt_scanner(AVWAPTouchLong, setup_kwargs={"earliest_touch": time(9, 0)}), bars)),
        1,
    )



def test_avt_and_reversal_run_independently_on_one_symbol() -> None:
    """Both setups see every bar; neither can consume the other's state."""
    sc = Scanner(
        {"AVT": DailyLevels("AVT", date(2026, 8, 13), 0.0, 0.0,
                            adr_pct=4.0, prev_close=100.0, adv=100_000_000.0)},
        [AVWAPTouchLong, ReversalLong],
    )
    sc.curves["AVT"] = _generous_curve("AVT")
    triggers = _feed(sc, _avt_long_bars())
    check("the AVT setup still fires alongside Reversal",
          [t.setup for t in triggers], ["AVT-L"])


def test_leg_qualifies_by_adr_or_by_percent() -> None:
    """ADR alone under-reads a high-ADR name: a stock that routinely swings
    10% has an ADR wide enough that a real move can still come in under the
    multiple. Real cases, 2026-09-28: SHMD 8.33% at 0.85x ADR and SRFM
    11.63% at 0.76x, both rejected as too small."""
    from astor.core.setups import _leg_is_big_enough

    def ok(leg_size, adr, anchor, **kw):
        return _leg_is_big_enough(
            leg_size, adr, anchor,
            kw.get("min_leg_adr", 0.9), kw.get("min_leg_pct", 6.0),
            kw.get("min_leg_pct_adr", 0.6),
        )

    # SHMD: $0.39 leg, $0.46 ADR, anchor 4.68 -> 0.85x ADR, 8.33%.
    check("high-ADR name clears on percent", ok(0.39, 0.46, 4.68), True)
    # SRFM: $0.12 leg, $0.16 ADR, anchor 1.07 -> 0.76x, 11.63%.
    check("and again on a $1 name", ok(0.12, 0.16, 1.07), True)
    # The ADR route on its own still works.
    check("a full-ADR leg needs no percent", ok(1.0, 1.0, 1000.0), True)

    # The floor under the percent route: a big percent on a leg that is a
    # tiny fraction of the name's own range is drift, not capitulation.
    check("percent alone is not enough", ok(0.05, 1.0, 0.5), False)
    # And a big ADR multiple with a trivial percent still clears, since the
    # ADR route does not consult percent at all.
    check("ADR route ignores percent", ok(0.95, 1.0, 10_000.0), True)

    # Switched off, the second route disappears entirely.
    check("min_leg_pct 0 restores the old single test",
          ok(0.39, 0.46, 4.68, min_leg_pct=0.0), False)


def test_live_scans_what_is_held() -> None:
    """A position is the one symbol that cannot afford to go unwatched, and
    the watchlist is rebuilt each morning from what looked interesting the
    night before -- not the same question."""
    from astor.cli import _with_open_positions

    syms, added = _with_open_positions(["AAPL", "NVDA"], ["DELL", "NVDA"])
    check("held names not on the list are added", added, ["DELL"])
    check("and the watchlist keeps its order and place", syms, ["AAPL", "NVDA", "DELL"])
    check("nothing held, nothing added", _with_open_positions(["AAPL"], []), (["AAPL"], []))
    check("an empty watchlist still scans the position",
          _with_open_positions([], ["DELL"]), (["DELL"], ["DELL"]))


def test_alert_marks_a_symbol_already_held() -> None:
    from astor.core.types import TouchAlert
    from astor.drivers.live import _format_order_alert

    a = TouchAlert("NNE", "long", OPEN, OPEN + timedelta(minutes=1), 16.85, 16.83,
                   0.031, 0.36, 0.94, None, 0.22, 3.57, target=16.95,
                   target_basis="recovery", exit_target=17.40, rr=3.0, stop=16.80,
                   shares=666, dollars=11289.0, max_loss_dollars=100.0)
    check("a fresh name carries no marker",
          "(position)" in _format_order_alert("AVWAP TOUCH", a, held=False), False)
    held_line = _format_order_alert("AVWAP TOUCH", a, held=True)
    check("a held one does", "(position)" in held_line, True)
    check("and it sits right after the symbol, before the direction",
          held_line.index("(position)") < held_line.index("long"), True)


def test_a_raised_stop_is_still_a_protective_stop() -> None:
    """A stop trailed above entry -- what the TRACK alerts tell this trader
    to do at +1R -- used to read as "wrong side" and block tracking. What
    makes a stop protective is which side of the *market* it sits on: a
    sell stop above the market would already have fired."""

    def rejected(stop: float, market: float | None, long: bool) -> bool:
        return bool(market) and ((stop > market) if long else (stop < market))

    # SPCX 2026-10-04: in at 109.34, stop trailed to 142.46, trading ~152.
    check("a winner's raised stop is accepted", rejected(142.46, 152.0, True), False)
    # The case the check is actually for: a stale sell stop above the market.
    check("a sell stop above the market is rejected", rejected(160.0, 152.0, True), True)
    # Mirror, for a short.
    check("a buy stop below the market is rejected", rejected(140.0, 152.0, False), True)
    check("a short's stop above the market is fine", rejected(160.0, 152.0, False), False)
    # No market price available: the check cannot run, so it does not block.
    check("unknown market price does not fail closed", rejected(160.0, None, True), False)


def test_reward_to_risk() -> None:
    from astor.core.setups import AVWAPTouchLong, AVWAPTouchShort, _reward_to_risk

    # Long: 2 of risk, 10 of reward.
    check("long", _reward_to_risk(entry=100, stop=98, exit_price=110), 5.0)
    # Short: stop above, exit below.
    check("short", _reward_to_risk(entry=100, stop=102, exit_price=94), 3.0)
    check("an exit behind price is not reward",
          _reward_to_risk(entry=100, stop=98, exit_price=99), None)
    check("no risk, no ratio", _reward_to_risk(entry=100, stop=100, exit_price=110), None)

    # AVT's exit is a measured move: the pullback's depth times the
    # multiple, projected from the touch. One expression, both directions.
    long_, short_ = AVWAPTouchLong(), AVWAPTouchShort()
    check("long projects above the day's high",
          long_._exit_from_touch(touch=100.0, extreme=110.0), 115.0)
    check("short projects below the day's low",
          short_._exit_from_touch(touch=100.0, extreme=90.0), 85.0)
    check("the multiple is tunable",
          AVWAPTouchLong(exit_depth_mult=2.0)._exit_from_touch(100.0, 110.0), 120.0)


def test_rev_rvol_threshold_tiers() -> None:
    """The more liquid the name, the less relative volume it takes to stand
    out. Boundaries sit 20% below the round figures they stand for, because
    the ADV they judge is IBKR volume, which excludes trades outside the
    NBBO and so reads low.
    """
    from astor.core.setups import rev_rvol_threshold

    check("illiquid name gets the strictest floor", rev_rvol_threshold(50_000_000.0), 2.0)
    check("just under the $400M rung", rev_rvol_threshold(399_999_999.0), 2.0)
    check("exactly on it stays strict (comparison is >)", rev_rvol_threshold(400_000_000.0), 2.0)
    # Real case: F's ADV ran $427M-$511M through Sept 2026 -- under the old
    # $500M rung it read 2.0 most days and flipped to 1.75 on others.
    check("F, mid-range", rev_rvol_threshold(494_933_742.0), 1.75)
    check("exactly on the $4B rung stays at 1.75", rev_rvol_threshold(4_000_000_000.0), 1.75)
    check("past it, the most liquid tier", rev_rvol_threshold(4_000_000_001.0), 1.5)
    check("a zero/unknown ADV is treated as illiquid", rev_rvol_threshold(0.0), 2.0)


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


def _revived_leg_bars() -> list[BarEvent]:
    """leg1 dies at bar 5, the retracement tops out at 96.9 (bar 10), and
    leg2 drives from there to 86.0 by bar 15. Anchor stays 98.0 all day."""
    return [
        bar(0, 98.0, 98.0, 97.5, 97.8, 500_000, symbol="REV"),
        bar(1, 97.8, 98.0, 96.0, 96.2, 500_000, symbol="REV"),
        bar(2, 96.2, 96.5, 95.0, 95.3, 500_000, symbol="REV"),
        bar(3, 95.3, 96.0, 94.0, 94.3, 500_000, symbol="REV"),
        bar(4, 94.3, 95.0, 93.0, 93.3, 500_000, symbol="REV"),
        bar(5, 93.3, 98.0, 93.5, 98.0, 500_000, symbol="REV"),  # retrace 100% -- kills leg1
        bar(6, 98.0, 98.0, 96.0, 96.5, 500_000, symbol="REV"),
        bar(7, 96.5, 96.8, 96.0, 96.6, 500_000, symbol="REV"),
        bar(8, 96.6, 96.9, 96.0, 96.7, 500_000, symbol="REV"),
        bar(9, 96.7, 97.0, 96.0, 96.8, 500_000, symbol="REV"),
        bar(10, 96.8, 97.1, 96.0, 96.9, 500_000, symbol="REV"),  # retracement tops at 96.9
        bar(11, 96.9, 97.0, 92.0, 92.3, 5_000, symbol="REV"),  # leg2 begins
        bar(12, 92.3, 92.7, 90.5, 90.8, 10_000, symbol="REV"),
        bar(13, 90.8, 91.0, 89.0, 89.3, 20_000, symbol="REV"),
        bar(14, 89.3, 89.6, 87.5, 87.8, 30_000, symbol="REV"),
        bar(15, 87.8, 88.0, 86.0, 86.3, 40_000, symbol="REV"),  # leg2 extreme 86.0
        bar(16, 86.3, 87.5, 86.1, 87.4, 5_000, symbol="REV"),  # retrace 11.7%, fires
    ]


def test_pace_measures_size_over_the_same_window_as_duration() -> None:
    """Regression test for a window mismatch in `pace`.

    leg_duration_minutes measures from push_started_at, which on a revived
    leg jumps forward to the dead leg's own retracement turnaround. The
    size it was divided by did not: that stayed abs(anchor - extreme), and
    the anchor here is the session high set back at bar 0. So pace was
    dividing the *whole day's* 12.0-point range -- sixteen bars in the
    making -- by only the revival's 5 minutes, and reporting the result as
    speed. The numerator has to span the same clock the denominator does.

    leg_size itself is deliberately left spanning anchor-to-extreme, since
    that is what the leg-size filter, the stop and R:R all mean by "the
    leg" -- so this changes no firing decision, only what pace reports.
    """
    sc = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_price": 0.0})
    t = _feed(sc, _revived_leg_bars())[0]
    setup = sc._state[("REV", "REV-L")]

    check("push started at the retracement's turnaround", setup._push_started_price, 96.9)
    check("anchor is still the session high", setup._anchor, 98.0)
    check("leg_size still spans anchor to extreme", abs(setup._anchor - setup._extreme), 12.0)
    check("push_size spans the revival only", round(setup._push_size(), 4), 10.9)

    minutes = t.extras["leg_duration_minutes"]
    check("duration is the revival's, not the whole day's", minutes, 5.0)
    # adr 4.0 and day_range 12.0 (98.0 high, 86.0 low) at fire time.
    check("pace_adr uses push_size", t.extras["pace_adr"], round(10.9 / 5.0 / 4.0, 5))
    check("pace_adr1 uses push_size", t.extras["pace_adr1"], round(10.9 / 5.0 / 12.0, 5))
    # The old numerator was the day's range itself, which is what made
    # pace_adr1 collapse to 1/minutes. On a revived leg it no longer does,
    # and that inequality is precisely the signature of a revived leg.
    check(
        "pace_adr1 no longer collapses to 1/minutes on a revived leg",
        t.extras["pace_adr1"] == round(1.0 / minutes, 5),
        False,
    )


def test_pace_is_unchanged_on_an_ordinary_leg() -> None:
    """The other half of the same fix: when the leg never died,
    push_started_at *is* the anchor's own timestamp, so push_size and
    leg_size are the same number and pace reports exactly what it did
    before. This is also where pace_adr1's documented collapse to
    1/minutes holds -- an ordinary Reversal leg runs between the session's
    own extremes, so its size is the day's range by construction.
    """
    sc = _rev_scanner(ReversalShort, setup_kwargs={"min_accel_price": 0.0, "min_accel_vol": 0.0})
    triggers = _feed(sc, _cdna_style_bars())
    check("the fixture fires", len(triggers) >= 1, True)
    t = triggers[0]
    setup = sc._state[("REV", "REV-S")]
    check(
        "push_size equals leg_size on a leg that never died",
        round(setup._push_size(), 6),
        round(abs(setup._anchor - setup._extreme), 6),
    )
    check(
        "pace_adr1 collapses to 1/minutes, as documented",
        t.extras["pace_adr1"],
        round(1.0 / t.extras["leg_duration_minutes"], 5),
    )


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


def test_capitulation_alert_refreshes_every_bar() -> None:
    """A live capitulation re-alerts on every bar, with the target and stop
    recomputed each time.

    This reverses an earlier rule (one alert per leg, added because FEIM
    2026-09-11 alerted three times in three minutes as one up-move set
    marginally deeper highs). What changed is what an alert is *for*: it is
    now an order ticket, and its numbers go stale every bar -- the 9EMA leg
    of the target redraws, and a deepening extreme moves both the target and
    the stop. A repeat carrying the same levels would be noise; a repeat
    carrying corrected ones is the point.
    """
    sc = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_price": 0.0})
    bars = [
        bar(0, 98.0, 98.0, 97.5, 97.8, 50_000, symbol="REV"),
        bar(1, 97.8, 97.9, 96.0, 96.2, 50_000, symbol="REV"),
        bar(2, 96.2, 96.3, 95.0, 95.1, 50_000, symbol="REV"),
        bar(3, 95.1, 95.2, 94.0, 94.1, 50_000, symbol="REV"),  # floors clear here
        bar(4, 94.1, 94.2, 93.0, 93.1, 50_000, symbol="REV"),  # deeper
        bar(5, 93.1, 93.2, 92.5, 92.6, 50_000, symbol="REV"),  # deeper again
    ]
    per_bar = []
    for b in bars:
        sc.on_bar(b)
        per_bar.append(sc.pop_alerts())

    check("one alert per bar once the leg qualifies",
          [len(a) for a in per_bar], [0, 0, 0, 1, 1, 1])
    live = [a[0] for a in per_bar if a]
    check("each one names the extreme as it stood",
          [a.leg_extreme for a in live], [94.0, 93.0, 92.5])
    check("the stop follows the extreme down", [a.stop for a in live], [94.0, 93.0, 92.5])
    check("and so does the target -- these are not stale repeats",
          len({a.target for a in live}), 3)


def test_capitulation_alert_is_silent_while_the_ticket_is_unchanged() -> None:
    """The refresh only speaks when it has something different to say: a
    bar that would print the same target and stop is not a correction."""
    sc = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_price": 0.0, "min_accel_vol": 0.0})
    bars = [
        bar(0, 98.0, 98.0, 97.5, 97.8, 50_000, symbol="REV"),
        bar(1, 97.8, 97.9, 96.0, 96.2, 50_000, symbol="REV"),
        bar(2, 96.2, 96.3, 95.0, 95.1, 50_000, symbol="REV"),
        bar(3, 95.1, 95.2, 94.0, 94.1, 50_000, symbol="REV"),  # qualifies, alerts
        # The extreme now sits still and price barely moves, so the ticket
        # would read the same bar after bar.
        bar(4, 94.1, 94.2, 94.0, 94.1, 50_000, symbol="REV"),
        bar(5, 94.1, 94.2, 94.0, 94.1, 50_000, symbol="REV"),
        bar(6, 94.1, 94.2, 93.0, 93.1, 50_000, symbol="REV"),  # new extreme: levels move
    ]
    per_bar = []
    for b in bars:
        sc.on_bar(b)
        per_bar.append(len(sc.pop_alerts()))
    check("alerts only where the ticket changed", per_bar, [0, 0, 0, 1, 0, 0, 1])


def test_capitulation_realert_gates_the_target_but_not_the_stop() -> None:
    """The stop re-alerts on any move; the target has to clear
    min_alert_move_adr first, because it carries the 9EMA and drifts on its
    own while nothing is happening."""
    # A leg that keeps deepening -- so the stop moves on most bars -- with
    # some bars where price wanders and the EMA (and so the target) inches.
    bars = [
        bar(0, 98.0, 98.0, 97.5, 97.8, 50_000, symbol="REV"),
        bar(1, 97.8, 97.9, 96.0, 96.2, 50_000, symbol="REV"),
        bar(2, 96.2, 96.3, 95.0, 95.1, 50_000, symbol="REV"),
        bar(3, 95.1, 95.2, 94.0, 94.1, 50_000, symbol="REV"),
        bar(4, 94.1, 94.2, 94.0, 94.05, 50_000, symbol="REV"),  # stop static, EMA inches
        bar(5, 94.05, 94.1, 94.0, 94.02, 50_000, symbol="REV"),  # same
        bar(6, 94.02, 94.1, 93.0, 93.1, 50_000, symbol="REV"),  # new extreme: stop moves
    ]

    def run(min_move: float) -> list[int]:
        sc = _rev_scanner(
            ReversalLong,
            setup_kwargs={"min_accel_price": 0.0, "min_accel_vol": 0.0,
                          "min_alert_move_adr": min_move},
        )
        out = []
        for b in bars:
            sc.on_bar(b)
            out.append(len(sc.pop_alerts()))
        return out

    # A floor nothing can clear: only the bars where the stop moved survive.
    stop_only = run(99.0)
    check("an unreachable floor leaves only stop moves", stop_only, [0, 0, 0, 1, 0, 0, 1])
    # No floor at all: every changed ticket re-alerts, including EMA drift.
    check("no floor lets the target's own drift through",
          sum(run(0.0)) >= sum(stop_only), True)



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
    for i, b in enumerate(bars):
        sc.on_bar(b)
        alerts.extend((i, a) for a in sc.pop_alerts())

    leg1 = [a for i, a in alerts if i <= 4]
    dead = [a for i, a in alerts if 5 <= i <= 10]
    leg2 = [a for i, a in alerts if i >= 11]
    check("leg1 alerts while it is live", len(leg1) >= 1, True)
    check("leg1's alerts name leg1's own extreme, never leg2's",
          {a.leg_extreme for a in leg1} <= {94.0, 93.0}, True)
    check("nothing at all while the leg is dead", dead, [])
    check("the revived leg alerts again", len(leg2) >= 1, True)
    check("and names its own extreme", {a.leg_extreme for a in leg2} <= {87.5, 86.0}, True)


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
        alerts.extend(sc_adr.pop_alerts())
    check("leg too small relative to ADR never alerts", alerts, [])

    demanding_curve = VolumeCurve(
        "REV", sessions=10,
        cum_volume_by_minute={(OPEN + timedelta(minutes=4)).timetz().replace(tzinfo=None): 10_000_000.0},
    )
    sc_rvol = _rev_scanner(ReversalLong, curve=demanding_curve)
    alerts = []
    for b in _canonical_long_bars():
        sc_rvol.on_bar(b)
        alerts.extend(sc_rvol.pop_alerts())
    check("thin RVOL relative to average never alerts", alerts, [])

    sc_accel = _rev_scanner(ReversalLong, setup_kwargs={"min_accel_price": 10.0})
    alerts = []
    for b in _canonical_long_bars():
        sc_accel.on_bar(b)
        alerts.extend(sc_accel.pop_alerts())
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
        alerts.extend(sc_fast.pop_alerts())
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
        alerts.extend(sc_loose.pop_alerts())
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
    rows, empty, session_open = build_profile_rows(bars, levels, curve, 5)

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
    check("session open comes back for the header", session_open, 10.00)
    # AVWAP through 09:34 is the volume-weighted typical price of bars 0-4;
    # price ran straight up, so the close sits above it.
    check("close is above AVWAP on a straight push up", first.avwap_pct > 0, True)
    # Dollars over dollars, the same convention every setup filter uses --
    # NOT the percent-over-ADR% reading, which anchors to AVWAP instead of
    # the prior close and disagrees whenever price has moved off it.
    # Recover AVWAP from the percent column and redo the division in dollars.
    avwap = first.close / (1 + first.avwap_pct / 100)
    check(
        "xADR(14) is the dollar distance over the dollar ADR",
        round(first.avwap_adr, 4),
        round((first.close - avwap) / levels.adr, 4),
    )
    check(
        "...which is NOT percent-over-ADR%, since that anchors to AVWAP "
        "rather than the prior close",
        round(first.avwap_adr, 4) != round(first.avwap_pct / levels.adr_pct, 4),
        True,
    )
    check("and it is signed, not a magnitude", rows[-1].avwap_pct < first.avwap_pct, True)
    day_range = first.close / (1 + first.from_high_pct / 100) - first.close / (
        1 + first.from_low_pct / 100
    )
    check(
        "xADR(1) divides the same dollar distance by today's dollar range",
        round(first.avwap_day_adr, 4),
        round((first.close - avwap) / day_range, 4),
    )
    # A 6% ADR name that has only travelled 3.41% so far reads bigger
    # against its own day than against the prior-session average.
    check(
        "today's range is the tighter yardstick early on",
        abs(first.avwap_day_adr) > abs(first.avwap_adr),
        True,
    )
    # Today's own range, high/low - 1: 10.76 high, 10.00 low at this point.
    check("session range is this day's own, not an average",
          round(rows[-1].session_range_pct, 2), 7.62)
    check("interval range is that bucket's own", round(rows[0].interval_range_pct, 2), 3.41)
    check("move from the session open", round(first.from_open_pct, 2), 3.0)
    # The running extremes include the current bar, so these two can only
    # ever be <= 0 and >= 0 respectively.
    check("from the day high, never positive", all(r.from_high_pct <= 0 for r in rows), True)
    check("from the day low, never negative", all(r.from_low_pct >= 0 for r in rows), True)
    # The last bucket closed at 10.48, against a 10.74 high and a 9.98 low.
    check("distance below the high", round(rows[-1].from_high_pct, 2), -2.42)
    check("distance above the low", round(rows[-1].from_low_pct, 2), 5.01)
    # The two must span the day's range: (1+lo%)*(1+hi%) reconstructs it.
    lo, hi = rows[-1].from_low_pct / 100, rows[-1].from_high_pct / 100
    check(
        "the pair brackets the day's range",
        round(((1 + lo) / (1 + hi) - 1) * 100, 2),
        round(rows[-1].session_range_pct, 2),
    )


def test_build_profile_rows_window_does_not_rebase_the_session() -> None:
    """--start/--end trim which rows print; RVOL and the session-to-date
    columns still accumulate from the open."""
    from astor.cli import build_profile_rows

    bars, levels, curve = _profile_fixture()
    full, _, _ = build_profile_rows(bars, levels, curve, 5)
    windowed, _, _ = build_profile_rows(bars, levels, curve, 5, time(9, 40), time(9, 50))

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
    rows, empty, _ = build_profile_rows(padded, levels, curve, 5)
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
