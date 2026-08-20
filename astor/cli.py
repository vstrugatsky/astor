"""Astor CLI: replay and live.

    uv run python -m astor.cli replay --date 2026-08-14 --tickers AAPL,NVDA,TSLA
    uv run python -m astor.cli replay --date 2026-08-14 --tickers-file watchlist.txt \
        --start 09:30 --end 11:00

    uv run python -m astor.cli live --tickers-file watchlist.txt
        (then at the '> ' prompt: 'add TSLA,NVDA', 'remove TSLA', 'quit')
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from datetime import date, datetime, time
from pathlib import Path

from ib_async import IB

from astor.core.scanner import Scanner
from astor.core.setups import ALL_SETUPS, SETUPS_BY_NAME
from astor.core.store import TriggerStore
from astor.drivers.live import LiveDriver
from astor.drivers.replay import ET, ReplayDriver, run_replay

logger = logging.getLogger("astor")


class _ETFormatter(logging.Formatter):
    """Log timestamps in ET, not the operator's local clock.

    Every other clock in this codebase -- bar timestamps, session dates,
    RTH_OPEN, the store's trigger_time column -- is ET; a trader running this
    from a different timezone reading local-time log lines next to ET-anchored
    behavior (open transitions, IB's own nightly maintenance window) is a real
    source of confusion, not a hypothetical one.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=ET)
        return dt.strftime(datefmt or "%H:%M:%S")


def split_symbols(raw: str) -> list[str]:
    syms = [item.upper().replace(".", " ") for item in raw.replace(",", " ").split()]
    seen: dict[str, None] = {}
    for s in syms:
        seen.setdefault(s, None)
    return list(seen)


def parse_symbols(args: argparse.Namespace) -> list[str]:
    raw = ""
    if args.tickers_file:
        raw = Path(args.tickers_file).read_text(encoding="utf-8")
        raw = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("#")
        )
    if args.tickers:
        raw += "\n" + args.tickers
    return split_symbols(raw)


def _setups_and_kwargs(args: argparse.Namespace):
    setups = (
        [SETUPS_BY_NAME[n] for n in args.setups.split(",")]
        if args.setups
        else ALL_SETUPS
    )
    kwargs = {
        "U&R": {
            "allow_same_bar_reclaim": args.allow_same_bar_reclaim,
            "min_excursion_pct": args.min_excursion_pct,
            "min_reclaim_pct": args.min_reclaim_pct,
            "min_combined_pct": args.min_combined_pct,
        },
        "O&F": {
            "allow_same_bar_fail": args.allow_same_bar_reclaim,
            "min_excursion_pct": args.min_excursion_pct,
            "min_reclaim_pct": args.min_reclaim_pct,
            "min_combined_pct": args.min_combined_pct,
        },
    }
    return setups, kwargs


def parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    return datetime.strptime(value, "%H:%M").time()


async def cmd_replay(args: argparse.Namespace) -> int:
    symbols = parse_symbols(args)
    if not symbols:
        logger.error("No tickers given. Use --tickers or --tickers-file.")
        return 2

    session = date.fromisoformat(args.date)
    start_t, end_t = parse_hhmm(args.start), parse_hhmm(args.end)
    setups, kwargs = _setups_and_kwargs(args)

    run_id = f"{session:%Y%m%d}-replay-{uuid.uuid4().hex[:8]}"
    params = {
        "session": str(session),
        "symbols": symbols,
        "bar_size": args.bar_size,
        "start": args.start,
        "end": args.end,
        "setups": [s.name for s in setups],
        "allow_same_bar_reclaim": args.allow_same_bar_reclaim,
        "min_excursion_pct": args.min_excursion_pct,
        "min_reclaim_pct": args.min_reclaim_pct,
        "min_combined_pct": args.min_combined_pct,
        "adr_sessions": args.adr_sessions,
    }

    ib = IB()
    logger.info("Connecting to %s:%d...", args.host, args.port)
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)
    try:
        driver = ReplayDriver(ib, session, args.bar_size, Path(args.cache_dir))
        logger.info("Fetching PDH/PDL for %d symbols...", len(symbols))
        levels = await driver.fetch_levels(symbols, args.adr_sessions)
        logger.info("Fetching %s bars for %s...", args.bar_size, session)
        bars = await driver.fetch_bars(symbols)
    finally:
        ib.disconnect()

    if not levels:
        logger.error("No reference levels resolved; nothing to replay.")
        return 1

    prior_sessions = {lv.session for lv in levels.values()}
    logger.info(
        "Levels from prior session(s): %s",
        ", ".join(str(s) for s in sorted(prior_sessions)),
    )

    scanner = Scanner(levels, setups, kwargs)

    store = TriggerStore(Path(args.db))
    if args.purge:
        # Rows, not the file: replacing the file would strand any open DataGrip
        # connection on the old inode, where it would read stale rows forever.
        removed = store.purge_runs()
        if removed:
            logger.info("Purged %d earlier run(s) from %s", removed, args.db)
    store.start_run(run_id, "replay", params)

    def on_trigger(t):
        # In Replay the honest analogue of a live detection timestamp is the
        # instant the bar completed -- the earliest a live system could have
        # seen it. Live records real wall clock, which will be this plus feed
        # latency, so Replay's value is the floor.
        store.record(run_id, t, t.bar_end)

    fed = run_replay(scanner, bars, start_t, end_t, on_trigger)
    store.commit()
    store.close()

    report(scanner, fed, levels, bars, run_id, args)
    return 0


def report(scanner, fed, levels, bars, run_id, args) -> None:
    triggers = sorted(scanner.triggers, key=lambda t: (t.bar_start, t.symbol))
    print("\n" + "=" * 78)
    print(f"REPLAY {args.date}   run_id={run_id}")
    print("=" * 78)
    print(f"Symbols with levels : {len(levels)}")
    adrs = [lv.adr_pct for lv in levels.values() if lv.adr_pct > 0]
    if adrs:
        print(
            f"ADR({args.adr_sessions})          : "
            f"{min(adrs):.2f}% - {max(adrs):.2f}% across symbols"
        )
    print(
        f"Filters             : excursion >= {args.min_excursion_pct}% of level, "
        f"reclaim >= {args.min_reclaim_pct}% of level, "
        f"excursion+reclaim >= {args.min_combined_pct}%"
    )
    print(f"Bars fed to scanner : {fed}")
    print(f"Bars evaluated      : {scanner.bars_evaluated}")
    print(f"Dropped, zero volume: {scanner.skipped_zero_volume}")
    if scanner.skipped_no_levels:
        print(f"Dropped, no levels  : {', '.join(sorted(scanner.skipped_no_levels))}")
    print(f"\nTriggers: {len(triggers)}")

    if triggers:
        print(
            f"\n{'time':<9} {'symbol':<7} {'setup':<5} {'price':>9} {'level':>9}"
            f" {'exc(%)':>7} {'rec(%)':>7} {'sum(%)':>7}"
            f" {'avwap':>9} {'rvol':>6}"
            f" {'beyond':>7} {'armed':>6}"
        )
        print("-" * 88)
        for t in triggers:
            e = t.extras
            nbars = e.get("bars_below", e.get("bars_above"))
            exc_pct, rec_pct, sum_pct = (
                e.get("excursion_pct"),
                e.get("reclaim_pct"),
                e.get("combined_pct"),
            )
            avwap, rvol = e.get("avwap"), e.get("rvol")
            exc_pct_s = f"{exc_pct:.2f}%" if exc_pct is not None else "-"
            rec_pct_s = f"{rec_pct:.2f}%" if rec_pct is not None else "-"
            sum_pct_s = f"{sum_pct:.2f}%" if sum_pct is not None else "-"
            avwap_s = f"{avwap:.2f}" if avwap is not None else "-"
            rvol_s = f"{rvol:.2f}" if rvol is not None else "-"
            print(
                f"{t.bar_start:%H:%M:%S}  {t.symbol:<7} {t.setup:<5} "
                f"{t.trigger_price:>9.2f} {t.level_value:>9.2f} "
                f"{exc_pct_s:>7} {rec_pct_s:>7} {sum_pct_s:>7}"
                f" {avwap_s:>9} {rvol_s:>6}"
                f" {nbars:>7} {e.get('bars_armed', 0):>6}"
            )

        by_symbol: dict[str, int] = {}
        for t in triggers:
            by_symbol[t.symbol] = by_symbol.get(t.symbol, 0) + 1
        busiest = sorted(by_symbol.items(), key=lambda kv: -kv[1])[:5]
        print("\nMost active: " + ", ".join(f"{s} ({n})" for s, n in busiest))

    print(f"\nStored in {args.db}  (run_id={run_id})")


async def cmd_live(args: argparse.Namespace) -> int:
    symbols = parse_symbols(args)
    setups, kwargs = _setups_and_kwargs(args)
    scanner = Scanner({}, setups, kwargs)

    ib = IB()
    logger.info("Connecting to %s:%d...", args.host, args.port)
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)

    try:
        driver = LiveDriver(ib, Path(args.cache_dir), args.bar_size)
        run_id = f"{driver.session:%Y%m%d}-live-{uuid.uuid4().hex[:8]}"
        logger.info("Live session target: %s (run_id=%s)", driver.session, run_id)

        store = TriggerStore(Path(args.db))
        if args.purge:
            removed = store.purge_runs()
            if removed:
                logger.info("Purged %d earlier run(s) from %s", removed, args.db)
        store.start_run(
            run_id,
            "live",
            {
                "session": str(driver.session),
                "bar_size": args.bar_size,
                "setups": [s.name for s in setups],
                "allow_same_bar_reclaim": args.allow_same_bar_reclaim,
                "min_excursion_pct": args.min_excursion_pct,
                "min_reclaim_pct": args.min_reclaim_pct,
                "min_combined_pct": args.min_combined_pct,
            },
        )

        if symbols:
            results = await driver.add_symbols(scanner, symbols, run_id, store)
            for sym, status in results.items():
                logger.info("%s: %s", sym, status)

        open_task = asyncio.create_task(
            driver.run_open_transition(scanner, run_id, store)
        )
        try:
            await _live_console(driver, scanner, run_id, store, args.max_tickers)
        finally:
            open_task.cancel()
            store.commit()
            store.close()
    finally:
        ib.disconnect()

    print(
        f"\n{len(scanner.triggers)} trigger(s) recorded this run. "
        f"Stored in {args.db}  (run_id={run_id})"
    )
    return 0


async def _live_console(
    driver: LiveDriver,
    scanner: Scanner,
    run_id: str,
    store: TriggerStore,
    cap: int,
) -> None:
    """Interactive add/remove prompt. Runs on the same event loop as IB's
    bar-completion callbacks -- `input()` is blocking, so it runs in an
    executor thread rather than stalling those callbacks between commands.
    """
    loop = asyncio.get_event_loop()
    print(
        f"Live scan running (session {driver.session}). "
        f"Tracking {driver.symbol_count()}/{cap or '∞'}. "
        "Commands: 'add SYM1,SYM2', 'remove SYM1,SYM2', 'list', 'quit'."
    )
    while True:
        try:
            line = await loop.run_in_executor(None, input, "> ")
        except EOFError:
            break
        line = line.strip()
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        cmd = cmd.lower()

        if cmd in ("quit", "exit"):
            break

        if cmd == "add":
            symbols = split_symbols(rest)
            if not symbols:
                print("Usage: add SYM1,SYM2,...")
                continue
            room = cap - driver.symbol_count() if cap else None
            if room is not None and len(symbols) > room:
                print(
                    f"Only room for {room} more (cap {cap}, currently "
                    f"{driver.symbol_count()}) -- add fewer or remove some first."
                )
                continue
            results = await driver.add_symbols(scanner, symbols, run_id, store)
            for sym, status in results.items():
                print(f"  {sym}: {status}")
            print(f"Tracking {driver.symbol_count()}/{cap or '∞'}")
        elif cmd == "remove":
            symbols = split_symbols(rest)
            if not symbols:
                print("Usage: remove SYM1,SYM2,...")
                continue
            results = await driver.remove_symbols(symbols)
            for sym, status in results.items():
                print(f"  {sym}: {status}")
            print(f"Tracking {driver.symbol_count()}/{cap or '∞'}")
        elif cmd in ("list", "status", "tickers"):
            tracked = driver.status()
            total = len(tracked["live"]) + len(tracked["pending"])
            print(f"Tracking {total}/{cap or '∞'}")
            if tracked["live"]:
                print(f"  live    ({len(tracked['live'])}): {', '.join(tracked['live'])}")
            if tracked["pending"]:
                print(
                    f"  pending ({len(tracked['pending'])}): "
                    f"{', '.join(tracked['pending'])}"
                )
            if not total:
                print("  (none yet)")
        else:
            print("Commands: 'add SYM1,SYM2', 'remove SYM1,SYM2', 'list', 'quit'.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("replay", help="Replay a market day and record triggers")
    r.add_argument("--date", required=True, help="Market day, YYYY-MM-DD")
    r.add_argument("--tickers", default="", help="Comma or space separated")
    r.add_argument("--tickers-file", default="")
    r.add_argument(
        "--bar-size",
        default="1 min",
        help="Also the update frequency: setups evaluate on bar completion",
    )
    r.add_argument("--start", default="", help="Session start HH:MM ET (default: open)")
    r.add_argument("--end", default="", help="Session end HH:MM ET (default: close)")
    r.add_argument(
        "--setups", default="", help="Comma separated, e.g. 'U&R'. Default: all"
    )
    r.add_argument(
        "--allow-same-bar-reclaim",
        action="store_true",
        help="Let a single bar both breach and reclaim the level",
    )
    r.add_argument(
        "--min-excursion-pct",
        type=float,
        default=0.6,
        help="Minimum breach depth past the level, as %% of the level's price "
        "(0 = off).",
    )
    r.add_argument(
        "--min-reclaim-pct",
        type=float,
        default=0.6,
        help="Minimum reclaim distance past the level, as %% of the level's "
        "price (0 = off).",
    )
    r.add_argument(
        "--min-combined-pct",
        type=float,
        default=1.5,
        help="Minimum excursion + reclaim combined, as %% of the level's "
        "price (0 = off). Catches a move that clears both individual floors "
        "but is still small overall.",
    )
    r.add_argument(
        "--adr-sessions",
        type=int,
        default=14,
        help="Sessions used for ADR (default 14). ADR is no longer used to "
        "filter triggers -- it is still fetched for the watchlist-wide ADR "
        "range in the report header and recorded as a diagnostic on each "
        "trigger.",
    )
    r.add_argument(
        "--purge",
        action="store_true",
        help="Clear earlier runs first, in place (keeps open DB connections valid)",
    )
    r.add_argument("--db", default="data/astor.db")
    r.add_argument("--cache-dir", default="data/bars")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=4001)
    r.add_argument("--client-id", type=int, default=21)

    lv = sub.add_parser(
        "live", help="Run a live scan against IBKR with an interactive add/remove prompt"
    )
    lv.add_argument(
        "--tickers", default="", help="Comma or space separated, initial watchlist"
    )
    lv.add_argument("--tickers-file", default="")
    lv.add_argument(
        "--bar-size",
        default="1 min",
        help="Also the update frequency: setups evaluate on bar completion",
    )
    lv.add_argument(
        "--setups", default="", help="Comma separated, e.g. 'U&R'. Default: all"
    )
    lv.add_argument(
        "--allow-same-bar-reclaim",
        action="store_true",
        help="Let a single bar both breach and reclaim the level",
    )
    lv.add_argument(
        "--min-excursion-pct",
        type=float,
        default=0.6,
        help="Minimum breach depth past the level, as %% of the level's price "
        "(0 = off).",
    )
    lv.add_argument(
        "--min-reclaim-pct",
        type=float,
        default=0.6,
        help="Minimum reclaim distance past the level, as %% of the level's "
        "price (0 = off).",
    )
    lv.add_argument(
        "--min-combined-pct",
        type=float,
        default=1.5,
        help="Minimum excursion + reclaim combined, as %% of the level's "
        "price (0 = off).",
    )
    lv.add_argument(
        "--max-tickers",
        type=int,
        default=50,
        help="Cap enforced by the add/remove console (0 = no cap)",
    )
    lv.add_argument(
        "--purge",
        action="store_true",
        help="Clear earlier runs first, in place (keeps open DB connections valid)",
    )
    lv.add_argument("--db", default="data/astor.db")
    lv.add_argument("--cache-dir", default="data/bars")
    lv.add_argument("--host", default="127.0.0.1")
    lv.add_argument("--port", type=int, default=4001)
    # Different default from replay's so a live run and a replay backtest
    # can point at the same IB Gateway/TWS at once without a clientId clash.
    lv.add_argument("--client-id", type=int, default=22)

    return p


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        _ETFormatter("%(asctime)s ET %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # ib_async logs its own account/portfolio sync (IB pushes a full update
    # roughly every 3 minutes once connected) at INFO -- silence that so it
    # doesn't bury our own INFO/WARNING lines, while keeping ib_async's own
    # WARNING/ERROR (e.g. "reqHistoricalData: Timeout for ...") visible.
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    args = build_parser().parse_args()
    cmd = {"replay": cmd_replay, "live": cmd_live}[args.command]
    raise SystemExit(asyncio.run(cmd(args)))


if __name__ == "__main__":
    main()
