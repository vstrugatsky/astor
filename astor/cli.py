"""Astor CLI. For now: replay.

    uv run python -m astor.cli replay --date 2026-08-14 --tickers AAPL,NVDA,TSLA
    uv run python -m astor.cli replay --date 2026-08-14 --tickers-file watchlist.txt \
        --start 09:30 --end 11:00
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
from astor.drivers.replay import ReplayDriver, run_replay

logger = logging.getLogger("astor")


def parse_symbols(args: argparse.Namespace) -> list[str]:
    raw = ""
    if args.tickers_file:
        raw = Path(args.tickers_file).read_text(encoding="utf-8")
        raw = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("#")
        )
    if args.tickers:
        raw += "\n" + args.tickers
    syms = [item.upper().replace(".", " ") for item in raw.replace(",", " ").split()]
    seen: dict[str, None] = {}
    for s in syms:
        seen.setdefault(s, None)
    return list(seen)


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
    setups = (
        [SETUPS_BY_NAME[n] for n in args.setups.split(",")]
        if args.setups
        else ALL_SETUPS
    )

    run_id = f"{session:%Y%m%d}-replay-{uuid.uuid4().hex[:8]}"
    params = {
        "session": str(session),
        "symbols": symbols,
        "bar_size": args.bar_size,
        "start": args.start,
        "end": args.end,
        "setups": [s.name for s in setups],
        "allow_same_bar_reclaim": args.allow_same_bar_reclaim,
        "min_excursion_adr": args.min_excursion_adr,
        "min_reclaim_adr": args.min_reclaim_adr,
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

    # Filters are scaled by ADR, so a symbol without ADR cannot be filtered.
    # Surface that rather than letting it look like a clean unfiltered pass.
    no_adr = sorted(s for s, lv in levels.items() if lv.adr <= 0)
    if no_adr and (args.min_excursion_adr or args.min_reclaim_adr):
        logger.warning(
            "No ADR for %s -- filters cannot be scaled, these run unfiltered",
            ", ".join(no_adr),
        )

    kwargs = {
        "U&R": {
            "allow_same_bar_reclaim": args.allow_same_bar_reclaim,
            "min_excursion_adr": args.min_excursion_adr,
            "min_reclaim_adr": args.min_reclaim_adr,
        },
        "O&F": {
            "allow_same_bar_fail": args.allow_same_bar_reclaim,
            "min_excursion_adr": args.min_excursion_adr,
            "min_reclaim_adr": args.min_reclaim_adr,
        },
    }
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
        f"Filters             : excursion >= {args.min_excursion_adr}x ADR, "
        f"reclaim >= {args.min_reclaim_adr}x ADR"
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
            f" {'exc':>7} {'rec':>7} {'beyond':>7} {'armed':>6}"
        )
        print("-" * 78)
        for t in triggers:
            e = t.extras
            nbars = e.get("bars_below", e.get("bars_above"))
            exc, rec = e.get("excursion_adr"), e.get("reclaim_adr")
            exc_s = f"{exc:.2f}x" if exc is not None else "-"
            rec_s = f"{rec:.2f}x" if rec is not None else "-"
            print(
                f"{t.bar_start:%H:%M:%S}  {t.symbol:<7} {t.setup:<5} "
                f"{t.trigger_price:>9.2f} {t.level_value:>9.2f} "
                f"{exc_s:>7} {rec_s:>7} {nbars:>7} {e.get('bars_armed', 0):>6}"
            )

        by_symbol: dict[str, int] = {}
        for t in triggers:
            by_symbol[t.symbol] = by_symbol.get(t.symbol, 0) + 1
        busiest = sorted(by_symbol.items(), key=lambda kv: -kv[1])[:5]
        print("\nMost active: " + ", ".join(f"{s} ({n})" for s, n in busiest))

    print(f"\nStored in {args.db}  (run_id={run_id})")


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
        "--min-excursion-adr",
        type=float,
        default=0.1,
        help="Minimum breach depth past the level, in ADR units (0 = off)",
    )
    r.add_argument(
        "--min-reclaim-adr",
        type=float,
        default=0.1,
        help="Minimum reclaim distance past the level, in ADR units (0 = off)",
    )
    r.add_argument(
        "--adr-sessions",
        type=int,
        default=14,
        help="Sessions used for ADR (default 14)",
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
    return p


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(cmd_replay(args)))


if __name__ == "__main__":
    main()
