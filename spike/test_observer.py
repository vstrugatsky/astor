"""Offline checks for the spike's analysis logic.

The measurement code is only useful if its arithmetic is right, and we cannot
rely on a live IBKR connection to find out. This drives StreamObserver with a
synthetic stream shaped like a real one -- including a ticker that stops trading
-- and asserts the derived numbers.

    uv run python -m spike.test_observer
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

from ib_async import BarData, BarDataList, Stock

from spike.ib_spike import EventLog, StreamObserver, now_et, parse_bar_seconds


def make_bars(symbol: str) -> BarDataList:
    bars = BarDataList()
    bars.contract = Stock(symbol, "SMART", "USD")
    return bars


def bar(dt, o, h, low, c, v) -> BarData:
    return BarData(
        date=dt, open=o, high=h, low=low, close=c, volume=v, average=c, barCount=1
    )


def main() -> None:
    assert parse_bar_seconds("1 min") == 60
    assert parse_bar_seconds("5 secs") == 5

    tmp = Path(tempfile.mkdtemp()) / "test.jsonl"
    log = EventLog(tmp)
    obs = StreamObserver(log, bar_seconds=60)

    # Anchor the synthetic stream just behind the wall clock so completion
    # latency lands in a plausible, assertable range.
    t0 = (now_et() - timedelta(minutes=5)).replace(second=0, microsecond=0)

    liquid = make_bars("LIQD")
    quiet = make_bars("QUIT")

    # LIQD trades every minute: 5 bars, each preceded by two forming updates.
    for i in range(5):
        bt = t0 + timedelta(minutes=i)
        liquid.append(bar(bt, 10.0, 10.0, 10.0, 10.0, 100))
        obs.on_update(liquid, True)  # new bar appended
        # forming updates mutate the last bar in place
        liquid[-1].high = 10.5
        liquid[-1].close = 10.4
        obs.on_update(liquid, False)
        liquid[-1].high = 10.7
        liquid[-1].volume = 250
        obs.on_update(liquid, False)

    # QUIT trades in minutes 0 and 3 only -- minutes 1, 2 and 4 never produce a bar.
    for i in (0, 3):
        bt = t0 + timedelta(minutes=i)
        quiet.append(bar(bt, 5.0, 5.0, 5.0, 5.0, 20))
        obs.on_update(quiet, True)

    liq = obs.stats["LIQD"]
    qut = obs.stats["QUIT"]

    assert liq.updates == 15, liq.updates
    assert liq.new_bars == 5, liq.new_bars
    assert liq.forming_mutations == 10, liq.forming_mutations
    assert len(liq.completion_latency) == 5

    assert qut.updates == 2, qut.updates
    assert qut.new_bars == 2, qut.new_bars
    assert qut.forming_mutations == 0, qut.forming_mutations

    # Completion latency must be positive and increase as bars get older-to-newer
    # relative to a fixed wall clock -- bar 0 is 5 min behind now, bar 4 is 1 min.
    lat = liq.completion_latency
    assert all(x > 0 for x in lat), lat
    assert lat[0] > lat[-1], lat
    # t0 is floored to the minute, so bar 0 sits 300-360s behind the wall clock.
    assert 300 <= lat[0] <= 360, f"bar 0 latency out of expected range: {lat[0]}"
    assert 60 <= lat[-1] <= 120, f"bar 4 latency out of expected range: {lat[-1]}"
    # Consecutive bars are one minute apart, so their latencies must be too.
    assert all(59 < a - b < 61 for a, b in zip(lat, lat[1:])), lat  # noqa: B905

    # Gap detection over the 5-minute window.
    window_start, window_end = t0, t0 + timedelta(minutes=5)
    gaps = obs.gap_report({"LIQD": liquid, "QUIT": quiet}, window_start, window_end)
    assert "LIQD" not in gaps, gaps
    assert len(gaps["QUIT"]) == 3, gaps["QUIT"]
    expected_missing = [(t0 + timedelta(minutes=i)).isoformat() for i in (1, 2, 4)]
    assert gaps["QUIT"] == expected_missing, gaps["QUIT"]

    log.close()
    print("All observer checks passed.")
    print(
        f"  LIQD: {liq.updates} updates, {liq.new_bars} bars, "
        f"{liq.forming_mutations} forming mutations, no gaps"
    )
    print(
        f"  QUIT: {qut.updates} updates, {len(gaps['QUIT'])} missing minutes "
        f"-> {gaps['QUIT']}"
    )


if __name__ == "__main__":
    main()
