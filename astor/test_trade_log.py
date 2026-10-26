"""The `trades` report's pairing of fills into round trips, and matching of
original stops.

uv run python -m astor.test_trade_log
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astor.core.trade_log import Fill, attach_stops, round_trips

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 19, 9, 30, tzinfo=ET)
REG = "U7654329"


def at(minute: int) -> datetime:
    return OPEN + timedelta(minutes=minute)


def check(label: str, got, want) -> None:
    assert got == want, f"{label}: expected {want}, got {got}"


def test_a_short_round_trip() -> None:
    [t] = round_trips([
        Fill(at(32), "ARQT", REG, -200, 30.25, 1.0),
        Fill(at(36), "ARQT", REG, 200, 30.24, 1.1),
    ])
    check("short", (t.direction, t.shares, t.entry, t.exit), ("short", 200, 30.25, 30.24))
    check("times", (t.opened_at, t.closed_at), (at(32), at(36)))
    check("P&L net of commissions", round(t.pnl, 2), round(200 * 0.01 - 2.1, 2))


def test_adds_and_partial_exits_stay_in_one_trade() -> None:
    [t] = round_trips([
        Fill(at(1), "XYZ", REG, 100, 10.0),
        Fill(at(2), "XYZ", REG, 100, 11.0),   # add
        Fill(at(3), "XYZ", REG, -50, 12.0),   # partial
        Fill(at(4), "XYZ", REG, -150, 13.0),  # the rest
    ])
    check("averages", (t.entry, t.exit, t.shares), (10.5, 12.75, 200))
    check("closed at the last fill", t.closed_at, at(4))


def test_back_to_flat_then_again_is_two_trades() -> None:
    trades = round_trips([
        Fill(at(1), "CCXI", REG, 500, 10.83),
        Fill(at(8), "CCXI", REG, -500, 10.71),   # stopped out
        Fill(at(14), "CCXI", REG, 500, 10.77),   # re-entry
    ])
    check("two trades", len(trades), 2)
    check("the second is open", (trades[1].is_open, trades[1].entry), (True, 10.77))


def test_a_fill_through_zero_closes_and_opens() -> None:
    long, short = round_trips([Fill(at(1), "XYZ", REG, 100, 10.0), Fill(at(2), "XYZ", REG, -300, 11.0, 3.0)])
    check("long closed on 100", (long.exited, long.exit, long.closed_at), (100, 11.0, at(2)))
    check("short opened on 200", (short.direction, short.shares, short.entry), ("short", 200, 11.0))
    check("commission split by shares", (round(long.commission, 2), round(short.commission, 2)), (1.0, 2.0))


def test_a_position_from_an_earlier_day() -> None:
    [t] = round_trips([Fill(at(5), "NVDA", REG, -100, 200.0)], start={("NVDA", REG): 100})
    check("carried, closed today", (t.carried, t.opened_at, t.closed_at), (100, None, at(5)))
    check("no P&L without the entry", t.pnl, None)


def test_stops_from_the_entry_or_else_track_trail() -> None:
    trades = round_trips([
        Fill(at(30), "CSIQ", REG, -600, 9.54), Fill(at(50), "CSIQ", REG, 600, 9.56),
        Fill(at(60), "BE", REG, 100, 50.0), Fill(at(90), "BE", REG, -100, 51.0),
    ])
    attach_stops(trades, [
        {"time": at(29), "symbol": "CSIQ", "account": REG, "stop": 9.59, "source": "enter"},
        {"time": at(35), "symbol": "CSIQ", "account": REG, "stop": 9.70, "source": "track/trail"},
        {"time": at(70), "symbol": "BE", "account": REG, "stop": 49.0, "source": "track/trail"},
        {"time": at(75), "symbol": "BE", "account": REG, "stop": 49.5, "source": "track/trail"},
    ])
    check("the entry's stop wins", trades[0].stop, 9.59)
    check("else the first track/trail one", trades[1].stop, 49.0)
    check("R off the original stop: +1 a share on 1 of risk", round(trades[1].pnl_r, 2), 1.0)


def test_rvol_at_entry_uses_the_bars_closed_before_it() -> None:
    from datetime import time

    from astor.core.trade_log import entry_rvol
    from astor.core.types import BarEvent, VolumeCurve

    bars = [
        BarEvent("XYZ", at(i), at(i + 1), 10, 10, 10, 10, 1000.0) for i in range(5)
    ]
    curve = VolumeCurve("XYZ", 20, {time(9, 30 + i): 500.0 * (i + 1) for i in range(5)})
    [t] = round_trips([Fill(at(3) + timedelta(seconds=20), "XYZ", REG, 100, 10.0)])
    # 09:33:20: bars 09:30-09:32 have closed -- 3,000 shares against 1,500
    check("RVOL at entry", entry_rvol(t, bars, curve), 2.0)
    [early] = round_trips([Fill(at(0) + timedelta(seconds=20), "XYZ", REG, 100, 10.0)])
    check("no bar closed yet", entry_rvol(early, bars, curve), None)


def test_each_exit_order_on_its_own() -> None:
    [t] = round_trips([
        Fill(at(1), "EWZ", REG, 179, 32.56, 1.0, order_id=1),
        Fill(at(20), "EWZ", REG, -20, 33.00, 0.5, order_id=2),   # one order,
        Fill(at(20), "EWZ", REG, -15, 33.00, 0.5, order_id=2),   # two fills
        Fill(at(90), "EWZ", REG, -36, 33.57, 1.0, order_id=3),
    ])
    check("two exit orders", [(e.shares, e.price) for e in t.exits], [(35, 33.0), (36, 33.57)])
    t.stop = 32.24
    e = t.exits[0]
    # 35 x 0.44 = 15.40, less its own $1 and 35/179 of the entry's $1
    check("an exit's own P&L", round(t.exit_pnl(e), 2), round(35 * 0.44 - 1.0 - 35 / 179, 2))
    check("and R", round(t.exit_r(e), 2), round(t.exit_pnl(e) / (0.32 * 35), 2))
    check("still open", (t.is_open, t.shares - t.exited), (True, 108))


def test_limit_and_adr1_at_entry() -> None:
    from astor.core.trade_log import entry_range_pct
    from astor.core.types import BarEvent

    [t] = round_trips([Fill(at(5) + timedelta(seconds=10), "XYZ", REG, 100, 10.0)])
    attach_stops([t], [{"time": at(4), "symbol": "XYZ", "account": REG, "stop": 9.5,
                        "limit": 10.05, "source": "enter"}])
    check("limit from the entry", (t.stop, t.limit), (9.5, 10.05))
    bars = [BarEvent("XYZ", at(i), at(i + 1), 10, 10.2 + i * 0.1, 9.8, 10, 1e3) for i in range(8)]
    # bars 09:30-09:34 closed by the entry: high 10.6, low 9.8 -> 0.8 on 10.0
    check("ADR(1) at entry", round(entry_range_pct(t, bars), 2), 8.0)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
