"""IBKR capability spike for Astor.

Answers the questions the Astor design currently *assumes*:

  observe  How does a keepUpToDate=True 1-min bar stream actually behave?
           - real update cadence (doc assumes 4-6s)
           - bar-completion latency: how long after a bar closes do we learn it closed?
           - do bars go MISSING for quiet tickers? (breaks naive "wait for next bar")
           - does the forming bar mutate between updates?

  ramp     How many concurrent keepUpToDate subscriptions actually work?
           (doc assumes 50) Ramps up in waves until errors appear.

  pacing   How long does a bulk historical backfill take? This is the Replay
           dataset build, and IBKR pacing rules are the bottleneck.

Run each mode BOTH during RTH and outside it -- IBKR behaves differently.

Usage:
    python -m spike.ib_spike observe --count 50 --minutes 20
    python -m spike.ib_spike ramp    --start 10 --step 10 --max 60
    python -m spike.ib_spike pacing  --count 50 --days 5

Results are written to spike/results/<mode>_<timestamp>.jsonl (raw events) and
summarized to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ib_async import IB, BarDataList, Contract, Stock

ET = ZoneInfo("America/New_York")

ROOT = Path(__file__).parent.resolve()
RESULTS_DIR = ROOT / "results"

# IBKR error codes that are informational farm-status noise, not real failures.
BENIGN_ERROR_CODES = {2104, 2106, 2107, 2108, 2119, 2158, 2100, 2150}

logger = logging.getLogger("ib_spike")


def now_et() -> datetime:
    return datetime.now(ET)


def to_et(dt: Any) -> datetime | None:
    """Normalize an IBKR bar timestamp to tz-aware ET.

    ib_async returns tz-aware datetimes for intraday bars (in the TWS timezone)
    and plain dates for daily bars. Anything else we treat as unusable.
    """
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def bar_tuple(bar: Any) -> tuple:
    return (bar.open, bar.high, bar.low, bar.close, bar.volume)


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


class EventLog:
    """Append-only JSONL sink so a long spike run survives a crash."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8")

    def write(self, **fields: Any) -> None:
        fields.setdefault("wall_et", now_et().isoformat())
        self._fh.write(json.dumps(fields, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._fh.close()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


async def connect(args: argparse.Namespace, log: EventLog) -> IB:
    ib = IB()

    def on_error(reqId: int, code: int, msg: str, contract: Contract | None):
        sym = getattr(contract, "symbol", None)
        log.write(event="ib_error", req_id=reqId, code=code, message=msg, symbol=sym)
        if code not in BENIGN_ERROR_CODES:
            logger.warning("IBKR error %d for %s: %s", code, sym or "-", msg)

    ib.errorEvent += on_error

    logger.info(
        "Connecting to %s:%d (clientId=%d)...", args.host, args.port, args.client_id
    )
    await ib.connectAsync(
        host=args.host, port=args.port, clientId=args.client_id, timeout=15
    )
    ib.reqMarketDataType(args.md_type)
    logger.info(
        "Connected. Server time: %s", (await ib.reqCurrentTimeAsync()).astimezone(ET)
    )
    log.write(event="connected", md_type=args.md_type)
    return ib


def load_symbols(args: argparse.Namespace) -> list[str]:
    text = Path(args.tickers_file).read_text(encoding="utf-8")
    syms = [
        t.strip().upper().replace(".", " ")
        for line in text.splitlines()
        if not line.strip().startswith("#")
        for item in line.split(",")
        for t in item.split()
        if t.strip()
    ]
    seen: dict[str, None] = {}
    for s in syms:
        seen.setdefault(s, None)
    ordered = list(seen)
    if args.count:
        ordered = ordered[: args.count]
    return ordered


async def qualify(ib: IB, symbols: list[str], log: EventLog) -> list[Contract]:
    raw = [Stock(s, "SMART", "USD") for s in symbols]
    qualified = await ib.qualifyContractsAsync(*raw)
    good = [c for c in qualified if c and getattr(c, "conId", 0) > 0]
    bad = sorted({s for s in symbols} - {c.symbol for c in good})
    if bad:
        logger.warning("Failed to qualify %d symbols: %s", len(bad), ", ".join(bad))
        log.write(event="qualify_failed", symbols=bad)
    logger.info("Qualified %d/%d contracts.", len(good), len(symbols))
    return good


# ---------------------------------------------------------------------------
# observe: what does a live keepUpToDate stream actually do?
# ---------------------------------------------------------------------------


@dataclass
class SymbolStats:
    updates: int = 0
    new_bars: int = 0
    update_intervals: list[float] = field(default_factory=list)
    completion_latency: list[float] = field(default_factory=list)
    forming_mutations: int = 0
    last_update_wall: float | None = None
    last_forming: tuple | None = None
    last_forming_time: datetime | None = None


class StreamObserver:
    """Attaches to BarDataList.updateEvent and measures real-world behaviour."""

    def __init__(self, log: EventLog, bar_seconds: int):
        self.log = log
        self.bar_seconds = bar_seconds
        self.stats: dict[str, SymbolStats] = defaultdict(SymbolStats)
        self.started_wall = time.monotonic()

    def on_update(self, bars: BarDataList, has_new_bar: bool) -> None:
        sym = getattr(bars.contract, "symbol", "?")
        st = self.stats[sym]
        wall = time.monotonic()
        wall_et = now_et()

        st.updates += 1
        if st.last_update_wall is not None:
            st.update_intervals.append(wall - st.last_update_wall)
        st.last_update_wall = wall

        if not bars:
            return

        last = bars[-1]
        last_time = to_et(last.date)

        if has_new_bar:
            st.new_bars += 1
            # A new bar appearing is our signal that the PREVIOUS bar is final.
            # The new bar is stamped at its own start, which is exactly the
            # instant the previous bar closed -- so this delta is the latency
            # Astor would pay before it could evaluate a completed bar.
            if last_time is not None:
                latency = (wall_et - last_time).total_seconds()
                st.completion_latency.append(latency)
                self.log.write(
                    event="new_bar",
                    symbol=sym,
                    bar_time=last_time.isoformat(),
                    completion_latency_sec=round(latency, 3),
                    n_bars=len(bars),
                )
        else:
            # Same bar, updated in place? That is a partial/forming bar update.
            cur = bar_tuple(last)
            if (
                st.last_forming_time is not None
                and last_time == st.last_forming_time
                and cur != st.last_forming
            ):
                st.forming_mutations += 1
                self.log.write(
                    event="forming_update",
                    symbol=sym,
                    bar_time=last_time.isoformat() if last_time else None,
                    ohlcv=cur,
                )

        st.last_forming = bar_tuple(last)
        st.last_forming_time = last_time

    # -- reporting ---------------------------------------------------------

    def gap_report(
        self, subs: dict[str, BarDataList], window_start: datetime, window_end: datetime
    ) -> dict[str, list[str]]:
        """Which expected bar timestamps never showed up, per symbol.

        A missing minute means the ticker simply did not trade. Astor cannot
        rely on "next bar arrived" to close the previous bar -- it needs a
        wall-clock timer.
        """
        step = timedelta(seconds=self.bar_seconds)
        expected: list[datetime] = []
        t = window_start.replace(second=0, microsecond=0)
        while t < window_end:
            expected.append(t)
            t += step

        gaps: dict[str, list[str]] = {}
        for sym, bars in subs.items():
            seen = {
                bt
                for b in bars
                if (bt := to_et(b.date)) is not None and window_start <= bt < window_end
            }
            missing = [e.isoformat() for e in expected if e not in seen]
            if missing:
                gaps[sym] = missing
        return gaps


async def run_observe(args: argparse.Namespace, ib: IB, log: EventLog) -> None:
    symbols = load_symbols(args)
    contracts = await qualify(ib, symbols, log)
    if not contracts:
        logger.error("Nothing to observe.")
        return

    bar_seconds = parse_bar_seconds(args.bar_size)
    obs = StreamObserver(log, bar_seconds)
    subs: dict[str, BarDataList] = {}

    logger.info(
        "Subscribing to %d keepUpToDate streams (%s, useRTH=%s), %.1fs apart...",
        len(contracts),
        args.bar_size,
        args.use_rth,
        args.sub_delay,
    )
    sub_start = now_et()
    for i, c in enumerate(contracts, 1):
        try:
            bars = await ib.reqHistoricalDataAsync(
                c,
                endDateTime="",  # required by keepUpToDate
                durationStr=args.duration,
                barSizeSetting=args.bar_size,
                whatToShow=args.what_to_show,
                useRTH=args.use_rth,
                formatDate=1,
                keepUpToDate=True,
            )
            bars.updateEvent += obs.on_update
            subs[c.symbol] = bars
            log.write(
                event="subscribed", symbol=c.symbol, index=i, initial_bars=len(bars)
            )
        except Exception as e:  # noqa: BLE001 - spike: record and continue
            logger.warning("Subscribe failed for %s (#%d): %s", c.symbol, i, e)
            log.write(event="subscribe_failed", symbol=c.symbol, index=i, error=str(e))
        await asyncio.sleep(args.sub_delay)

    logger.info(
        "%d/%d subscriptions live. Observing for %d minutes...",
        len(subs),
        len(contracts),
        args.minutes,
    )
    window_start = now_et().replace(second=0, microsecond=0) + timedelta(
        seconds=bar_seconds
    )
    await asyncio.sleep(args.minutes * 60)
    window_end = now_et().replace(second=0, microsecond=0)

    gaps = obs.gap_report(subs, window_start, window_end)

    for bars in subs.values():
        with contextlib.suppress(Exception):
            ib.cancelHistoricalData(bars)

    summarize_observe(obs, subs, gaps, sub_start, window_start, window_end, args, log)


def summarize_observe(
    obs: StreamObserver,
    subs: dict[str, BarDataList],
    gaps: dict[str, list[str]],
    sub_start: datetime,
    window_start: datetime,
    window_end: datetime,
    args: argparse.Namespace,
    log: EventLog,
) -> None:
    all_intervals = [i for s in obs.stats.values() for i in s.update_intervals]
    all_latency = [x for s in obs.stats.values() for x in s.completion_latency]
    silent = sorted(set(subs) - {s for s, st in obs.stats.items() if st.updates > 0})
    total_mutations = sum(s.forming_mutations for s in obs.stats.values())

    print("\n" + "=" * 72)
    print("OBSERVE SUMMARY")
    print("=" * 72)
    print(f"Subscribed          : {len(subs)}")
    print(f"Subscribe wall time : {(window_start - sub_start).total_seconds():.1f}s")
    print(f"Window              : {window_start:%H:%M:%S} -> {window_end:%H:%M:%S} ET")
    print(f"Streams w/ updates  : {len(subs) - len(silent)}")
    if silent:
        print(f"  SILENT (no data)  : {', '.join(silent)}")

    print(f"\nUpdate cadence (doc assumes 4-6s), n={len(all_intervals)}")
    print_stats(all_intervals, "s")

    print(
        f"\nBar-completion latency -- how late we learn a bar closed, n={len(all_latency)}"
    )
    print_stats(all_latency, "s")
    print("  ^ This is the floor on Astor's Notify latency, on top of bar duration.")

    print(f"\nForming-bar mutations observed: {total_mutations}")
    print(
        "  ^ >0 confirms partial bars are visible mid-interval. Evaluating only on\n"
        "    completed bars (as decided) correctly ignores these."
    )

    print(
        f"\nMissing bars (ticker did not trade that interval): {len(gaps)} symbols affected"
    )
    for sym, missing in sorted(gaps.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"  {sym:<8} {len(missing):>4} missing  e.g. {missing[:3]}")
    if gaps:
        print(
            "  ^ Astor must close bars on a wall-clock timer, NOT on 'next bar arrived'."
        )

    log.write(
        event="observe_summary",
        subscribed=len(subs),
        silent=silent,
        interval_stats=describe(all_intervals),
        completion_latency_stats=describe(all_latency),
        forming_mutations=total_mutations,
        symbols_with_gaps=len(gaps),
        gap_detail={k: len(v) for k, v in gaps.items()},
        args=vars(args),
    )
    print(f"\nRaw events: {log.path}")


# ---------------------------------------------------------------------------
# ramp: find the real concurrent-subscription ceiling
# ---------------------------------------------------------------------------


async def run_ramp(args: argparse.Namespace, ib: IB, log: EventLog) -> None:
    symbols = load_symbols(args)
    contracts = await qualify(ib, symbols, log)
    if len(contracts) < args.max_subs:
        logger.warning(
            "Only %d contracts available; capping ramp at that.", len(contracts)
        )

    bar_seconds = parse_bar_seconds(args.bar_size)
    obs = StreamObserver(log, bar_seconds)
    subs: dict[str, BarDataList] = {}
    errors: list[dict] = []

    def on_error(reqId: int, code: int, msg: str, contract: Contract | None):
        if code not in BENIGN_ERROR_CODES:
            errors.append(
                {"code": code, "msg": msg, "symbol": getattr(contract, "symbol", None)}
            )

    ib.errorEvent += on_error

    waves: list[dict] = []
    target = args.start
    idx = 0
    while target <= min(args.max_subs, len(contracts)):
        errors.clear()
        wave_failures = 0
        while idx < target and idx < len(contracts):
            c = contracts[idx]
            idx += 1
            try:
                bars = await ib.reqHistoricalDataAsync(
                    c,
                    endDateTime="",
                    durationStr=args.duration,
                    barSizeSetting=args.bar_size,
                    whatToShow=args.what_to_show,
                    useRTH=args.use_rth,
                    formatDate=1,
                    keepUpToDate=True,
                )
                bars.updateEvent += obs.on_update
                subs[c.symbol] = bars
            except Exception as e:  # noqa: BLE001
                wave_failures += 1
                logger.warning("Subscribe failed at #%d (%s): %s", idx, c.symbol, e)
                log.write(
                    event="subscribe_failed", symbol=c.symbol, index=idx, error=str(e)
                )
            await asyncio.sleep(args.sub_delay)

        logger.info(
            "Wave: %d subscriptions live. Dwelling %ds...", len(subs), args.dwell
        )
        before = {s: obs.stats[s].updates for s in subs}
        await asyncio.sleep(args.dwell)
        producing = sum(1 for s in subs if obs.stats[s].updates > before.get(s, 0))

        wave = {
            "target": target,
            "live_subs": len(subs),
            "producing_during_dwell": producing,
            "subscribe_failures": wave_failures,
            "errors": list(errors),
        }
        waves.append(wave)
        log.write(event="ramp_wave", **wave)
        logger.info(
            "  -> %d/%d producing, %d subscribe failures, %d IBKR errors",
            producing,
            len(subs),
            wave_failures,
            len(errors),
        )

        if wave_failures or errors:
            logger.warning(
                "Failures appeared at target=%d. Continuing to confirm.", target
            )

        target += args.step

    for bars in subs.values():
        with contextlib.suppress(Exception):
            ib.cancelHistoricalData(bars)

    print("\n" + "=" * 72)
    print("RAMP SUMMARY  (doc assumes 50 concurrent keepUpToDate streams)")
    print("=" * 72)
    print(f"{'target':>7} {'live':>6} {'producing':>10} {'sub_fail':>9} {'errors':>7}")
    for w in waves:
        print(
            f"{w['target']:>7} {w['live_subs']:>6} {w['producing_during_dwell']:>10} "
            f"{w['subscribe_failures']:>9} {len(w['errors']):>7}"
        )
    clean = [
        w["target"]
        for w in waves
        if not w["subscribe_failures"]
        and not w["errors"]
        and w["producing_during_dwell"] == w["live_subs"]
    ]
    if clean:
        print(f"\nHighest fully-clean wave: {max(clean)} concurrent streams")
    else:
        print("\nNo fully-clean wave -- investigate errors above.")
    print(f"\nRaw events: {log.path}")
    log.write(event="ramp_summary", waves=waves, args=vars(args))


# ---------------------------------------------------------------------------
# pacing: how long does a Replay dataset build take?
# ---------------------------------------------------------------------------


async def run_pacing(args: argparse.Namespace, ib: IB, log: EventLog) -> None:
    symbols = load_symbols(args)
    contracts = await qualify(ib, symbols, log)
    sem = asyncio.Semaphore(args.concurrency)
    results: list[dict] = []

    async def fetch(c: Contract) -> None:
        async with sem:
            t0 = time.monotonic()
            try:
                bars = await asyncio.wait_for(
                    ib.reqHistoricalDataAsync(
                        c,
                        endDateTime="",
                        durationStr=f"{args.days} D",
                        barSizeSetting=args.bar_size,
                        whatToShow=args.what_to_show,
                        useRTH=args.use_rth,
                        formatDate=1,
                        keepUpToDate=False,
                        # ib_async applies its OWN timeout (default 60s) and returns
                        # an empty list rather than raising. Without this, a silent
                        # internal timeout would be recorded as a success with 0 bars.
                        timeout=args.timeout,
                    ),
                    timeout=args.timeout + 15,
                )
                dt = time.monotonic() - t0
                rec = {
                    "symbol": c.symbol,
                    "bars": len(bars),
                    "seconds": round(dt, 2),
                    "ok": bool(bars),
                }
                if not bars:
                    rec["error"] = f"empty result after {dt:.0f}s (ib_async timeout)"
            except TimeoutError:
                rec = {
                    "symbol": c.symbol,
                    "bars": 0,
                    "seconds": round(time.monotonic() - t0, 2),
                    "ok": False,
                    "error": f"timeout>{args.timeout}s",
                }
            except Exception as e:  # noqa: BLE001
                rec = {
                    "symbol": c.symbol,
                    "bars": 0,
                    "seconds": round(time.monotonic() - t0, 2),
                    "ok": False,
                    "error": str(e),
                }
            results.append(rec)
            log.write(event="pacing_fetch", **rec)

    logger.info(
        "Fetching %s of %s bars for %d symbols (concurrency=%d)...",
        f"{args.days} D",
        args.bar_size,
        len(contracts),
        args.concurrency,
    )
    t_start = time.monotonic()
    await asyncio.gather(*[fetch(c) for c in contracts])
    elapsed = time.monotonic() - t_start

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    durations = [r["seconds"] for r in ok]

    print("\n" + "=" * 72)
    print("PACING SUMMARY  (this is the Replay dataset build cost)")
    print("=" * 72)
    print(
        f"Request           : {args.days} D of {args.bar_size}, useRTH={args.use_rth}"
    )
    print(f"Symbols           : {len(contracts)}   concurrency: {args.concurrency}")
    print(f"Total wall time   : {elapsed:.1f}s")
    print(f"Succeeded         : {len(ok)}    Failed: {len(failed)}")
    if ok:
        print(
            f"Bars per symbol   : median {statistics.median(r['bars'] for r in ok):.0f}"
        )
        print("Per-request time  :")
        print_stats(durations, "s")
    if failed:
        print("\nFailures:")
        for r in failed[:15]:
            print(f"  {r['symbol']:<8} {r.get('error', '?')}")
    if elapsed > 0 and ok:
        per_day = elapsed / max(args.days, 1)
        print(
            f"\nExtrapolation: ~{per_day:.1f}s per market day for {len(contracts)} symbols."
            f"\n  A 40-day replay corpus would take roughly {per_day * 40 / 60:.1f} min."
        )
    print(f"\nRaw events: {log.path}")
    log.write(
        event="pacing_summary",
        elapsed_sec=round(elapsed, 2),
        ok=len(ok),
        failed=len(failed),
        duration_stats=describe(durations),
        args=vars(args),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def parse_bar_seconds(bar_size: str) -> int:
    """'1 min' -> 60. Supports the IBKR barSizeSetting vocabulary we care about."""
    parts = bar_size.strip().split()
    if len(parts) != 2:
        raise ValueError(f"Unrecognized bar size: {bar_size!r}")
    n, unit = int(parts[0]), parts[1].rstrip("s")
    factor = {"sec": 1, "min": 60, "hour": 3600, "day": 86400}.get(unit)
    if factor is None:
        raise ValueError(f"Unrecognized bar size unit: {unit!r}")
    return n * factor


def describe(xs: list[float]) -> dict[str, float] | None:
    if not xs:
        return None
    s = sorted(xs)
    return {
        "n": len(s),
        "min": round(s[0], 3),
        "median": round(statistics.median(s), 3),
        "mean": round(statistics.fmean(s), 3),
        "p95": round(s[min(int(len(s) * 0.95), len(s) - 1)], 3),
        "max": round(s[-1], 3),
    }


def print_stats(xs: list[float], unit: str) -> None:
    d = describe(xs)
    if not d:
        print("  (no samples)")
        return
    print(
        f"  min {d['min']:.2f}{unit}   median {d['median']:.2f}{unit}   "
        f"mean {d['mean']:.2f}{unit}   p95 {d['p95']:.2f}{unit}   max {d['max']:.2f}{unit}"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("mode", choices=["observe", "ramp", "pacing"])
    p.add_argument("--tickers-file", default=str(ROOT / "tickers.txt"))
    p.add_argument(
        "--count", type=int, default=0, help="Limit symbols (0 = all in file)"
    )

    p.add_argument("--host", default="127.0.0.1")
    p.add_argument(
        "--port",
        type=int,
        default=4001,
        help="4001 IBGW live, 4002 IBGW paper, 7496/7497 TWS",
    )
    p.add_argument("--client-id", type=int, default=17)
    p.add_argument(
        "--md-type",
        type=int,
        default=1,
        help="1 live, 2 frozen, 3 delayed, 4 delayed-frozen",
    )

    p.add_argument("--bar-size", default="1 min")
    p.add_argument("--duration", default="1 D")
    p.add_argument("--what-to-show", default="TRADES")
    p.add_argument(
        "--use-rth", type=lambda s: s.lower() not in ("0", "false", "no"), default=True
    )
    p.add_argument(
        "--sub-delay",
        type=float,
        default=0.5,
        help="Seconds between subscribe calls. IBKR allows ~60 historical requests per 10 min.",
    )

    p.add_argument(
        "--minutes", type=int, default=20, help="observe: observation window"
    )
    p.add_argument("--start", type=int, default=10, help="ramp: first wave size")
    p.add_argument("--step", type=int, default=10, help="ramp: wave increment")
    p.add_argument("--max-subs", type=int, default=60, help="ramp: highest wave")
    p.add_argument(
        "--dwell", type=int, default=90, help="ramp: seconds to hold each wave"
    )
    p.add_argument(
        "--days", type=int, default=5, help="pacing: days of history per symbol"
    )
    p.add_argument(
        "--concurrency", type=int, default=10, help="pacing: parallel requests"
    )
    p.add_argument(
        "--timeout", type=float, default=60.0, help="pacing: per-request timeout"
    )
    return p


async def main_async(args: argparse.Namespace) -> None:
    stamp = now_et().strftime("%Y%m%d_%H%M%S")
    log = EventLog(RESULTS_DIR / f"{args.mode}_{stamp}.jsonl")
    log.write(event="run_start", mode=args.mode, args=vars(args))

    ib = await connect(args, log)
    try:
        if args.mode == "observe":
            await run_observe(args, ib, log)
        elif args.mode == "ramp":
            await run_ramp(args, ib, log)
        else:
            await run_pacing(args, ib, log)
    finally:
        log.write(event="run_end")
        with contextlib.suppress(Exception):
            ib.disconnect()
        log.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = build_parser().parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted -- partial results are in spike/results/")


if __name__ == "__main__":
    main()
