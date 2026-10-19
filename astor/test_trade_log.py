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


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
