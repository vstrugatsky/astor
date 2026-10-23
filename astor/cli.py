"""Astor CLI: replay and live.

    uv run python -m astor.cli replay --date 2026-08-14 --tickers AAPL,NVDA,TSLA
    uv run python -m astor.cli replay --date 2026-08-14 --tickers-file watchlist.txt \
        --start 09:30 --end 11:00

    uv run python -m astor.cli live --tickers-file watchlist.txt
        (then at the '> ' prompt: 'add TSLA,NVDA', 'remove TSLA',
        'track NVDA', 'track NVDA 137.50 130.10' or
        'track NVDA U1234566', 'trail NVDA', 'trail NVDA 137.50 130.10 U1234566',
        'untrail NVDA', 'untrack NVDA', 'positions', 'mute', 'unmute', 'quit'; --dry-run-trail to have trail place no orders)
        orders: 'buy NVDA stop 35.99' (sized by astor, Adaptive Normal), 'buy NVDA 100 stop 35.99',
        'sell SPCX stop 172.01 when 170.10 lim 170.00 loss 250' (stop-limit),
        'enter SPCX short 20 stp 170.10 stop 172.01', 'stop SPCX 170.10',
        'stop SPCX 1/2 168.05 1/2 170.01', 'cancel SPCX', 'exit SPCX [lmt 168.20]'
        (--dry-run-trail covers these too)

    uv run python -m astor.cli warm-cache --tickers-file broad_watchlist.txt
        (run overnight via cron so a mid-session Live 'add' hits disk, not IBKR)

    uv run python -m astor.cli purge-db
        (wipes the ENTIRE store, every session -- only after a schema change;
        a same-day rerun of replay/live never needs this)

    uv run python -m astor.cli explain --date 2026-08-28 --ticker NTNX --setup REV-S
        (bar-by-bar reasons why that setup did or did not fire)

    uv run python -m astor.cli profile --date 2026-09-14 --ticker RUM --interval 5
        (how RVOL and the day's own range built through that session)

    uv run python -m astor.cli positions
        (open positions from both IBKR feeds, plus working orders)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ib_async import IB

from astor.core.exit_plan import parse_trade_command
from astor.core.indicators import SessionIndicators, compute_interval_rvol, compute_rvol
from astor.core.order_entry import (
    CommandError,
    account_token,
    is_account,
    parse_cancel,
    parse_enter,
    parse_exit,
    parse_stop,
)
from astor.core.scanner import Scanner
from astor.core.setups import ALL_SETUPS, SETUPS_BY_NAME, pace_label, rev_rvol_threshold
from astor.core.store import TriggerStore
from astor.drivers.live import _ANSI_GREEN, _ANSI_RED, LiveDriver, _accent, position_pnl
from astor.drivers.replay import (
    ADR_SESSIONS,
    ET,
    RVOL_SESSIONS,
    ReplayDriver,
    next_trading_session,
    run_replay,
)
from astor.drivers.trades_report import format_trades, todays_trades

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


def _setup_kwargs(args: argparse.Namespace) -> dict:
    reversal_kwargs = {
        "min_leg_adr": args.min_leg_adr,
        "min_leg_pct": args.min_leg_pct,
        "min_leg_pct_adr": args.min_leg_pct_adr,
        "min_retrace_pct": args.min_retrace_pct,
        "max_retrace_pct": args.max_retrace_pct,
        "min_confirm_minutes": args.min_confirm_minutes,
        "min_leg_minutes": args.min_leg_minutes,
        "min_ema9_extension_adr": args.min_ema9_extension_adr,
        "min_alert_move_adr": args.min_alert_move_adr,
        "min_accel_price": args.min_accel_price,
        "min_accel_vol": args.min_accel_vol,
        "accel_window": args.accel_window,
        "max_loss_dollars": args.max_loss,
    }
    avt_kwargs = {
        "min_day_range_adr": args.min_avt_day_range_adr,
        "min_day_range_pct": args.min_avt_day_range_pct,
        "min_day_range_pct_adr": args.min_avt_day_range_pct_adr,
        "max_yard_pct": args.avt_max_yard_pct,
        "touch_band_adr": args.avt_touch_band_adr,
        "min_depth_adr": args.min_avt_depth_adr,
        "recovery_adr": args.avt_recovery_adr,
        "max_bars_since_touch": args.avt_max_bars_since_touch,
        "max_break_adr": args.avt_max_break_adr,
        "rearm_adr": args.avt_rearm_adr if args.avt_rearm_adr >= 0 else None,
        "stop_adr": args.avt_stop_adr,
        "earliest_touch": parse_hhmm(args.avt_earliest_touch),
        "require_deeper_touch": args.avt_require_deeper_touch,
        "exit_depth_mult": args.avt_exit_depth_mult,
        "max_loss_dollars": args.max_loss,
    }
    return {
        "REV-L": reversal_kwargs,
        "REV-S": reversal_kwargs,
        "AVT-L": avt_kwargs,
        "AVT-S": avt_kwargs,
    }


def _setups_and_kwargs(args: argparse.Namespace):
    setups = (
        [SETUPS_BY_NAME[n.strip().upper()] for n in args.setups.split(",")]
        if args.setups
        else ALL_SETUPS
    )
    return setups, _setup_kwargs(args)


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
        "adr_sessions": args.adr_sessions,
"setup_kwargs": _setup_kwargs(args),
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
        logger.info("Fetching volume curves for RVOL...")
        curves = await driver.fetch_volume_curve(symbols)
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
    scanner.curves = curves

    store = TriggerStore(Path(args.db))
    store.start_run(run_id, "replay", params)

    def on_trigger(t):
        # In Replay the honest analogue of a live detection timestamp is the
        # instant the bar completed -- the earliest a live system could have
        # seen it. Live records real wall clock, which will be this plus feed
        # latency, so Replay's value is the floor.
        store.record(run_id, t, t.bar_end)

    def on_setup_alert(a):
        store.record_alert(
            run_id,
            a.kind,
            a.symbol,
            a.direction,
            a.bar_start,
            a.price,
            a.bar_end,
            a.store_extras(),
            bar_end=a.bar_end,
        )

    fed = run_replay(scanner, bars, start_t, end_t, on_trigger, on_setup_alert)
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
        f"Filters (REV-L/S)   : leg >= {args.min_leg_adr}x ADR built over "
        f">= {args.min_leg_minutes}min, ema9 ext >= {args.min_ema9_extension_adr} ADR, "
        f"accel_price >= {args.min_accel_price}, accel_vol >= {args.min_accel_vol}, "
        f"retrace {args.min_retrace_pct}%-{args.max_retrace_pct}% of leg (or EMA9 reclaim), "
        f"held >= {args.min_confirm_minutes}min since the extreme"
    )
    print(f"Bars fed to scanner : {fed}")
    print(f"Bars evaluated      : {scanner.bars_evaluated}")
    print(f"Dropped, zero volume: {scanner.skipped_zero_volume}")
    if scanner.skipped_no_levels:
        print(f"Dropped, no levels  : {', '.join(sorted(scanner.skipped_no_levels))}")
    print(f"\nTriggers: {len(triggers)}")
    from collections import Counter
    by_kind = Counter(a.kind for a in scanner.alerts)
    print(
        "Alerts: "
        + (
            ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items()))
            if by_kind
            else "none"
        )
    )

    if triggers:
        print(
            f"\n{'time':<9} {'symbol':<7} {'setup':<5} {'grd':>3} {'price':>9} {'level':>9}"
            f" {'leg(x)':>7} {'retr%':>7} {'m_rvol':>7} {'ema9x':>7} {'accel':>6}"
            f" {'avwap':>9} {'rvol':>6}"
        )
        print("-" * 96)
        for t in triggers:
            e = t.extras
            leg_adr, retrace_pct = e.get("leg_size_adr"), e.get("retrace_pct")
            move_rvol, ema9_ext = e.get("move_rvol"), e.get("ema9_extension_adr")
            accel = e.get("accel_price")
            avwap, rvol = e.get("avwap"), e.get("rvol")
            auto_grade = e.get("auto_grade")
            grade_s = str(auto_grade) if auto_grade is not None else "-"
            leg_s = f"{leg_adr:.2f}" if leg_adr is not None else "-"
            retrace_s = f"{retrace_pct:.1f}%" if retrace_pct is not None else "-"
            move_rvol_s = f"{move_rvol:.2f}" if move_rvol is not None else "-"
            ema9_s = f"{ema9_ext:.2f}" if ema9_ext is not None else "-"
            accel_s = f"{accel:.2f}" if accel is not None else "-"
            avwap_s = f"{avwap:.2f}" if avwap is not None else "-"
            rvol_s = f"{rvol:.2f}" if rvol is not None else "-"
            print(
                f"{t.bar_start:%H:%M:%S}  {t.symbol:<7} {t.setup:<5} {grade_s:>3} "
                f"{t.trigger_price:>9.2f} {t.level_value:>9.2f} "
                f"{leg_s:>7} {retrace_s:>7} {move_rvol_s:>7} {ema9_s:>7} {accel_s:>6}"
                f" {avwap_s:>9} {rvol_s:>6}"
            )

        by_symbol: dict[str, int] = {}
        for t in triggers:
            by_symbol[t.symbol] = by_symbol.get(t.symbol, 0) + 1
        busiest = sorted(by_symbol.items(), key=lambda kv: -kv[1])[:5]
        print("\nMost active: " + ", ".join(f"{s} ({n})" for s, n in busiest))

    print(f"\nStored in {args.db}  (run_id={run_id})")


def _alert_timeline(replay: list, received: list[dict]) -> list[dict]:
    """Alert lines for `explain`'s timeline, in the same {time, text} shape
    as the trace, flagged `alert` so they sort after the decision point of
    the same bar.

    A replay alert is matched to a live one by bar: same bar_start means
    the trader got it, and when; one with no live match was not received
    (live was not watching the symbol, or saw different data). A live alert
    the replay does not reproduce is listed too -- IBKR revises intraday
    volume after the fact, so live and a later replay can disagree, and live
    is what was actually seen.
    """
    by_bar = {r["bar_start"]: r for r in received}
    out = []

    def ticket(target, stop, shares, rr, pace) -> str:
        t = f"{target:.2f}" if target is not None else "n/a"
        # Capitulation's R:R is 9.0 by construction; its pace is the
        # number that varies, as on the live alert line.
        if pace is not None:
            lead = f" pace {pace_label(pace)}"
        else:
            lead = f" R:R {rr:.1f}" if rr is not None else ""
        sh = f" {shares} sh" if shares is not None else ""
        return f"target {t} stop {stop:.2f}{sh}{lead}"

    for a in replay:
        line = f"ALERT {ticket(a.target, a.stop, a.shares, a.rr, getattr(a, 'pace_adr', None))}"
        got = by_bar.pop(a.bar_start, None)
        if got is not None:
            line += f"  <- RECEIVED {got['detection_ts']:%H:%M:%S}"
        else:
            line += f"  <- not received; would have come ~{a.bar_end:%H:%M:%S}"
        out.append({"time": a.bar_start, "text": line, "alert": True})

    for r in by_bar.values():
        x = r["extras"]
        line = (
            f"ALERT {ticket(x.get('target'), x.get('stop'), x.get('level_stop_shares'), x.get('rr'), x.get('pace_adr'))}"
            f"  <- RECEIVED {r['detection_ts']:%H:%M:%S} (live only: the replay does not "
            "reproduce it -- likely IBKR's revised volume)"
        )
        out.append({"time": r["bar_start"], "text": line, "alert": True})
    return out


async def cmd_explain(args: argparse.Namespace) -> int:
    """Why did (or didn't) one setup fire for one symbol on one day.

    Runs the exact same Setup.on_bar/Scanner.on_bar code every replay/live
    run uses, just for a single symbol and a single setup, with the `trace`
    hook (astor.core.setups) turned on -- so this is not a second
    implementation of the filter logic that could quietly drift out of sync
    with the real one; it is the real one, just narrated.
    """
    symbol = args.ticker.upper().replace(".", " ")
    setup_cls = SETUPS_BY_NAME.get(args.setup.upper())  # rev-s == REV-S
    if setup_cls is None:
        logger.error("Unknown setup %r. Choices: %s", args.setup, ", ".join(SETUPS_BY_NAME))
        return 2

    session = date.fromisoformat(args.date)
    kwargs = _setup_kwargs(args)[setup_cls.name]

    ib = IB()
    logger.info("Connecting to %s:%d...", args.host, args.port)
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)
    try:
        driver = ReplayDriver(ib, session, "1 min", Path(args.cache_dir))
        levels = await driver.fetch_levels([symbol], args.adr_sessions)
        bars_by_symbol = await driver.fetch_bars([symbol])
        curves = await driver.fetch_volume_curve([symbol])
    finally:
        ib.disconnect()

    lv = levels.get(symbol)
    if lv is None:
        logger.error("No reference levels for %s on %s -- nothing to explain.", symbol, session)
        return 1

    scanner = Scanner({symbol: lv}, [setup_cls], {setup_cls.name: kwargs})
    if symbol in curves:
        scanner.curves[symbol] = curves[symbol]

    trace: dict[str, list[dict]] = {}
    fired = []
    replay_alerts = []
    for b in bars_by_symbol.get(symbol, []):
        fired.extend(scanner.on_bar(b, trace=trace))
        replay_alerts.extend(scanner.pop_alerts())

    # What live actually sent, to set against what the replay says it would
    # have. Read-only: explain never writes to the store.
    alert_kind = "avwap_touch" if setup_cls.name.startswith("AVT-") else "capitulation"
    store = TriggerStore(Path(args.db))
    try:
        received = store.live_alerts(symbol, session, alert_kind, setup_cls.direction)
    finally:
        store.close()
    alert_lines = _alert_timeline(replay_alerts, received)

    print("\n" + "=" * 78)
    print(f"EXPLAIN {setup_cls.name} {symbol} {session}")
    print("=" * 78)
    print(
        f"PDH {lv.pdh:.2f}  PDL {lv.pdl:.2f}  ADR({args.adr_sessions}) {lv.adr_pct:.2f}% "
        f"(${lv.adr:.2f})  ADV ${lv.adv:,.0f}  move_rvol floor {rev_rvol_threshold(lv.adv)}"
    )
    ind = scanner.indicators.get(symbol)
    if ind is not None and ind.session_open is not None:
        gap = _pct_change(ind.session_open, lv.prev_close)
        gap_adr = (
            (ind.session_open - lv.prev_close) / lv.adr if lv.adr > 0 else None
        )
        gap_str = (
            f" (gap {gap:+.2f}%" + (f", {gap_adr:+.2f}x ADR)" if gap_adr is not None else ")")
            if gap is not None
            else ""
        )
        print(
            f"Session open {ind.session_open:.2f}{gap_str}  high {ind.session_high:.2f}  "
            f"low {ind.session_low:.2f}"
        )
    print(
        f"Bars evaluated: {scanner.bars_evaluated}  "
        f"(dropped zero-volume: {scanner.skipped_zero_volume})"
    )

    lines = sorted(
        trace.get(setup_cls.name, []) + alert_lines, key=lambda e: (e["time"], e.get("alert", False))
    )
    if not lines:
        print(
            f"\nNo decision points at all -- {setup_cls.name} never even armed or "
            f"tracked a leg for {symbol} on {session}."
        )
    else:
        print(
            "\nEach filter reads value/threshold; leg reads "
            "size (xADR/min xADR, percent move)."
        )
        n_alerts = sum(1 for e in lines if e.get("alert"))
        print(
            f"\n{len(lines) - n_alerts} decision point(s), {n_alerts} alert line(s) "
            f"({len(received)} received live):"
        )
        for entry in lines:
            print(f"  {entry['time']:%H:%M}  {entry['text']}")

    if fired:
        print(f"\n{len(fired)} trigger(s) fired:")
        for t in fired:
            leg_duration = t.extras.get("leg_duration_minutes")
            duration_s = f", leg built over {leg_duration:.0f}m" if leg_duration is not None else ""
            print(
                f"  {t.bar_start:%H:%M:%S}  {t.symbol} {t.setup} {t.direction} "
                f"@ {t.trigger_price:.2f}{duration_s}"
            )
    else:
        print("\nNo trigger fired.")
    return 0


RTH_OPEN = time(9, 30)


@dataclass(frozen=True, slots=True)
class ProfileRow:
    """One interval of a session, from two angles: what happened *in* the
    interval, and where the day stood at the end of it."""

    at: datetime  # the interval's first minute -- the label it prints under
    bars: int
    close: float
    # Within the interval.
    volume: float
    interval_rvol: float | None
    interval_range_pct: float | None
    # Session to date, as of this interval's last bar.
    cum_volume: float
    rvol: float | None
    session_range_pct: float | None
    from_open_pct: float | None
    # Where in the day's range this close sits. The running extremes include
    # the current bar, so from_high is never positive and from_low never
    # negative -- a bar printing the day's high reads 0.00% on one and the
    # full range on the other.
    from_high_pct: float | None
    from_low_pct: float | None
    # Against session AVWAP, as of this interval's last bar. Signed: which
    # side of AVWAP price is on is the whole point. The distance is then
    # given twice over: against the prior-session average range, and against
    # the range this day has actually made so far.
    avwap_pct: float | None
    avwap_adr: float | None
    avwap_day_adr: float | None


def _range_pct(high: float | None, low: float | None) -> float | None:
    """A range as a percent of its own low -- (High/Low - 1) x 100, the same
    arithmetic ADR% is built from, but for one day (or one interval) alone
    with no averaging across sessions. What a trader means by "this name has
    done 7% today".
    """
    if high is None or low is None or low <= 0:
        return None
    return (high / low - 1.0) * 100.0


def _pct_change(value: float | None, reference: float | None) -> float | None:
    """`value` against `reference` as a signed percent -- close vs AVWAP,
    close vs the open, the open vs the prior close. Signed throughout: which
    side of the reference price sits on is the point of every one of them."""
    if not reference or value is None:
        return None
    return (value / reference - 1.0) * 100.0


def _per_adr(distance: float | None, yardstick: float | None) -> float | None:
    """A dollar distance in range multiples: 0.50x is half a range. The
    yardstick is either the dollar ADR (prior-session average) or the range
    today has made so far, both in dollars.

    Dollars over dollars, deliberately, rather than percent over percent.
    The two are not the same: a percent-of-AVWAP over ADR% implicitly
    anchors the yardstick to AVWAP, while DailyLevels.adr is anchored to the
    prior close on purpose -- see its docstring -- and every setup filter
    divides by that. On 2026-09-18 FLNC, trading 5% under its prior close,
    the two readings of the same distance differed by exactly that 5%, so
    profile and explain disagreed about a number they were both calling
    xADR.
    """
    if distance is None or not yardstick or yardstick <= 0:
        return None
    return distance / yardstick


def build_profile_rows(
    bars: list,
    levels,
    curve,
    interval_minutes: int,
    start: time | None = None,
    end: time | None = None,
) -> tuple[list[ProfileRow], int, float | None]:
    """Bucket a session's 1-min bars into `interval_minutes` windows.

    Returns (rows, empty_intervals, session_open). Buckets are anchored to
    the 09:30 open, not to the first bar, so a 5-min run always reads
    09:30/09:35/09:40 however late the symbol's first print lands.

    RVOL and the session-to-date columns are always accumulated from the
    open -- `start`/`end` trim which rows *print*, never what they are
    computed from, so windowing a run can't quietly rebase the day's pace.

    Zero-volume bars are dropped exactly as Scanner does (astor.core.scanner
    ._gate): IBKR pads thin tickers with them, and a minute nothing traded in
    must not move AVWAP, the extremes, or the volume this RVOL divides.
    """
    indicators = SessionIndicators(levels.symbol)
    buckets: dict[datetime, list] = {}
    rows: list[ProfileRow] = []

    for b in bars:
        if b.volume <= 0:
            continue
        anchor = b.start.replace(hour=RTH_OPEN.hour, minute=RTH_OPEN.minute, second=0, microsecond=0)
        idx = int((b.start - anchor).total_seconds() // 60) // interval_minutes
        buckets.setdefault(anchor + timedelta(minutes=idx * interval_minutes), []).append(b)

    for at in sorted(buckets):
        group = buckets[at]
        for b in group:
            indicators.update(b)
        last = group[-1]
        volume = sum(b.volume for b in group)
        hi, lo = max(b.high for b in group), min(b.low for b in group)
        avwap_pct = _pct_change(last.close, indicators.avwap)
        session_range_pct = _range_pct(indicators.session_high, indicators.session_low)
        # Signed dollar distance from AVWAP, and the two dollar yardsticks it
        # gets divided by -- see _per_adr for why this is not done in percents.
        avwap_distance = (
            last.close - indicators.avwap if indicators.avwap is not None else None
        )
        day_range = (
            indicators.session_high - indicators.session_low
            if indicators.session_high is not None and indicators.session_low is not None
            else None
        )
        rows.append(
            ProfileRow(
                at=at,
                bars=len(group),
                close=last.close,
                volume=volume,
                interval_rvol=(
                    compute_interval_rvol(volume, group[0].start, last.start, curve)
                    if curve is not None
                    else None
                ),
                interval_range_pct=_range_pct(hi, lo),
                cum_volume=indicators.cum_volume,
                rvol=(
                    compute_rvol(indicators.cum_volume, last.start, curve)
                    if curve is not None
                    else None
                ),
                session_range_pct=session_range_pct,
                from_open_pct=_pct_change(last.close, indicators.session_open),
                from_high_pct=_pct_change(last.close, indicators.session_high),
                from_low_pct=_pct_change(last.close, indicators.session_low),
                avwap_pct=avwap_pct,
                # Divided by ADR% (not the dollar ADR) so both sides of the
                # ratio are percents and it reads as a plain multiple: 0.50x
                # means price sits half a normal day's range off AVWAP.
                avwap_adr=_per_adr(avwap_distance, levels.adr),
                # The same distance against *today's* range instead of the
                # prior-session average -- how far off AVWAP price is
                # relative to what this day has actually delivered so far,
                # which on a big day is a very different number.
                avwap_day_adr=_per_adr(avwap_distance, day_range),
            )
        )

    in_window = [
        r
        for r in rows
        if (start is None or r.at.time() >= start) and (end is None or r.at.time() < end)
    ]
    # Buckets between the first and last printed row that saw no trades at
    # all -- for a thin name that gap is itself the finding, so it gets
    # counted rather than silently closed up.
    empty = 0
    if in_window:
        span = (in_window[-1].at - in_window[0].at).total_seconds() / 60
        empty = int(span // interval_minutes) + 1 - len(in_window)
    # The session's own open, from the same SessionIndicators the columns are
    # built on -- so it is the first bar that actually traded, not whatever
    # zero-volume padding IBKR put in front of it.
    return in_window, empty, indicators.session_open


def _human_volume(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return f"{v:.0f}"


def _human_dollars(v: float) -> str:
    """A dollar figure short enough to sit in a header line: $1.23B or
    $24.29M. Nothing in this codebase trades in amounts where the exact
    dollar matters -- ADV is read as a magnitude."""
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    return f"${v / 1_000_000:.2f}M"


def print_profile(
    rows: list[ProfileRow], empty: int, session_open: float | None, levels, curve, args
) -> None:
    def n(v: float | None, suffix: str = "", nd: int = 2) -> str:
        return f"{v:.{nd}f}{suffix}" if v is not None else "n/a"

    def signed(v: float | None, suffix: str = "%") -> str:
        return f"{v:+.2f}{suffix}" if v is not None else "n/a"

    # 7 (TIME) + the three column groups below -- kept in one place so the
    # rules, the group header and the columns can't drift apart.
    interval_w, session_w, avwap_w = 8 + 9 + 8, 8 + 8 + 12 + 10 + 10, 9 + 10 + 9
    rule = "=" * (7 + interval_w + session_w + avwap_w)
    print("\n" + rule)
    print(f"PROFILE {levels.symbol} {args.date}   {args.interval}-min intervals")
    print(rule)
    open_str = f"{session_open:.2f}" if session_open is not None else "n/a"
    gap_pct = _pct_change(session_open, levels.prev_close)
    gap = signed(gap_pct)
    if gap_pct is not None and levels.adr_pct > 0:
        # Also in ADR multiples, so it reads directly against the range
        # columns and against what AVT's day-size filter actually counts.
        gap = f"{gap} ({gap_pct / levels.adr_pct:+.2f}x ADR)"
    print(
        f"open {open_str}   gap {gap}   prev close {levels.prev_close:.2f}   "
        f"PDH {levels.pdh:.2f}   PDL {levels.pdl:.2f}   "
        f"ADR({levels.adr_sessions}) {levels.adr_pct:.2f}% (${levels.adr:.2f})   "
        f"ADV {_human_dollars(levels.adv)}"
    )
    print(
        f"RVOL curve: {curve.sessions} prior sessions"
        if curve is not None
        else "RVOL curve: none -- RVOL columns will read n/a"
    )
    print(
        "\nRANGE% is this day's own high/low - 1, no averaging across sessions: "
        "the session-to-date\ncolumn is today's ADR% as it stands. The ADR() "
        "above is the prior-session average, for scale.\navwap is the close "
        f"against session AVWAP, signed, then that same figure over "
        f"ADR({levels.adr_sessions})%\nand over today's own range so far -- "
        "on a big day those two read very differently."
    )
    print(
        "\n"
        + (
            " " * 7
            + "--- in the interval ---".center(interval_w)
            + "--- session to date ---".center(session_w)
            + "--- avwap ---".center(avwap_w)
        ).rstrip()
    )
    # The two xADR columns name their own denominator -- (14) is the
    # prior-session average, (1) is this one day -- so neither can be
    # mistaken for the other, and the label follows --adr-sessions.
    print(
        f"{'TIME':<7}{'CLOSE':>8}{'VOL':>9}{'RANGE%':>8}"
        f"{'RVOL':>8}{'RANGE%':>8}{'FROM OPEN':>12}"
        f"{'FROM HIGH':>10}{'FROM LOW':>10}"
        f"{'%':>9}{f'xADR({levels.adr_sessions})':>10}{'xADR(1)':>9}"
    )
    for r in rows:
        print(
            f"{r.at:%H:%M}  {r.close:>8.2f}{_human_volume(r.volume):>9}"
            f"{n(r.interval_range_pct, '%'):>8}"
            f"{n(r.rvol):>8}"
            f"{n(r.session_range_pct, '%'):>8}"
            f"{signed(r.from_open_pct):>12}"
            f"{signed(r.from_high_pct):>10}{signed(r.from_low_pct):>10}"
            f"{signed(r.avwap_pct):>9}{signed(r.avwap_adr, 'x'):>10}"
            f"{signed(r.avwap_day_adr, 'x'):>9}"
        )
    if not rows:
        print("  (no bars with volume in this window)")
        return
    last = rows[-1]
    print(
        f"\nClose of window: RVOL {n(last.rvol)}   "
        f"day range {n(last.session_range_pct, '%')} "
        f"(prior {levels.adr_sessions}-session average {levels.adr_pct:.2f}%)   "
        f"{len(rows)} interval(s)"
        + (f", {empty} with no trades at all" if empty else "")
    )


async def cmd_profile(args: argparse.Namespace) -> int:
    """How one symbol's session actually developed: RVOL and ADR usage,
    interval by interval.

    Built for tuning the filters rather than for trading off -- when a
    trigger fires late, or an accel/RVOL floor lets through something it
    shouldn't have, this is the tape those numbers were read from. It reuses
    the same SessionIndicators/compute_rvol the live path runs on, off the
    same cached 1-min bars, so what it prints is what the algorithm saw.
    """
    symbol = args.ticker.upper().replace(".", " ")
    session = date.fromisoformat(args.date)
    if args.interval < 1:
        logger.error("--interval must be at least 1 minute (got %s)", args.interval)
        return 2
    start_t, end_t = parse_hhmm(args.start), parse_hhmm(args.end)

    ib = IB()
    logger.info("Connecting to %s:%d...", args.host, args.port)
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)
    try:
        driver = ReplayDriver(ib, session, "1 min", Path(args.cache_dir))
        levels = await driver.fetch_levels([symbol], args.adr_sessions)
        bars_by_symbol = await driver.fetch_bars([symbol])
        curves = await driver.fetch_volume_curve([symbol], args.rvol_sessions)
    finally:
        ib.disconnect()

    lv = levels.get(symbol)
    if lv is None:
        logger.error("No reference levels for %s on %s -- nothing to profile.", symbol, session)
        return 1
    bars = bars_by_symbol.get(symbol, [])
    if not bars:
        logger.error("No intraday bars for %s on %s.", symbol, session)
        return 1

    curve = curves.get(symbol)
    if curve is None:
        logger.warning("No volume curve for %s -- RVOL columns will read n/a.", symbol)
    rows, empty, session_open = build_profile_rows(
        bars, lv, curve, args.interval, start_t, end_t
    )
    print_profile(rows, empty, session_open, lv, curve, args)
    return 0


def _pnl_color(text: str, value: float) -> str:
    return _accent(text, _ANSI_GREEN if value > 0 else _ANSI_RED) if value else text


async def cmd_trades(args: argparse.Namespace) -> int:
    """Today's trades -- or --date's, while IBKR still has its fills --
    from the fills, with the original stops astor recorded. Read-only."""
    ib = IB()
    logger.info("Connecting to %s:%d...", args.host, args.port)
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15, readonly=True)
    try:
        session = date.fromisoformat(args.date) if args.date else datetime.now(ET).date()
        trades = await todays_trades(ib, TriggerStore(Path(args.db)), session)
        print(f"Trades {session}")
        for line in format_trades(trades, _pnl_color):
            print(line)
    finally:
        ib.disconnect()
    return 0


async def cmd_positions(args: argparse.Namespace) -> int:
    """What IBKR thinks you are holding, from two independent sources.

    IBKR reports positions two different ways, and the desktop app's
    Portfolio view is driven by the second one:

      reqPositions   -> ib.positions()   -- the account's positions
      PnLSingle      -> per position     -- IB's valuation of it: its own
                                            position count, market value,
                                            unrealized and daily P&L

    They come from separate subscriptions, so when a position vanishes from
    the desktop Portfolio view this says whether it vanished from the data
    or only from that window: if reqPositions still has it and IB still
    values it, the position is fine and the app's feed is stale. A restart
    would have "fixed" it either way, which is why it is worth checking
    before restarting.

    The second source used to be the portfolio feed (`ib.portfolio()`),
    which matches that window most closely but is empty on a login with
    more than one account -- see `position_pnl`.

    Read-only -- it opens a connection, asks, prints, and disconnects.
    """
    ib = IB()
    logger.info("Connecting to %s:%d...", args.host, args.port)
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)
    try:
        await ib.reqPositionsAsync()
        positions = [p for p in ib.positions() if p.position]
        pnls = await asyncio.gather(*(position_pnl(ib, p) for p in positions))
        orders = await ib.reqAllOpenOrdersAsync()
    finally:
        ib.disconnect()

    print("\n" + "=" * 78)
    print(f"POSITIONS   {datetime.now(ET):%Y-%m-%d %H:%M:%S} ET")
    print("=" * 78)

    rows = sorted(zip(positions, pnls, strict=True), key=lambda r: (r[0].account, r[0].contract.symbol))
    print(f"\nreqPositions ({len(positions)}):")
    if not positions:
        print("  (none)")
    for p, _ in rows:
        print(
            f"  {p.contract.symbol:<8} {p.position:>10,g} @ avg {p.avgCost:>10,.4f}"
            f"   {p.account}"
        )

    valued = [(p, v) for p, v in rows if v is not None]
    print(f"\nIB valuation / PnLSingle ({len(valued)}):")
    if not valued:
        print("  (none)")
    for p, v in valued:
        mkt = v.value / v.position if v.position else float("nan")
        print(
            f"  {p.contract.symbol:<8} {v.position:>10,g} @ mkt {mkt:>10,.4f}"
            f"   value {v.value:>12,.2f}   unreal {v.unrealizedPnL:>10,.2f}"
            f"   day {v.dailyPnL:>10,.2f}   {p.account}"
        )

    # The whole point: do the two sources agree?
    unvalued = [p for p, v in rows if v is None]
    disagree = [(p, v) for p, v in valued if v.position != p.position]
    print()
    if not unvalued and not disagree:
        print(f"Both sources agree: {len(positions)} open position(s).")
    else:
        print("MISMATCH between the two sources:")
        for p in unvalued:
            print(f"  {p.contract.symbol} ({p.account}): in reqPositions, IB sent no valuation")
        for p, v in disagree:
            print(
                f"  {p.contract.symbol} ({p.account}): reqPositions has {p.position:,g},"
                f" IB values {v.position:,g}"
            )

    working = [
        t for t in orders
        if t.orderStatus.status not in ("Filled", "Cancelled", "Inactive", "ApiCancelled")
    ]
    print(f"\nWorking orders ({len(working)}):")
    if not working:
        print("  (none)")
    for t in sorted(working, key=lambda t: t.contract.symbol):
        o = t.order
        price = o.auxPrice or o.lmtPrice or 0.0
        print(
            f"  {t.contract.symbol:<8} {o.action:<4} {o.totalQuantity:>8,.0f} {o.orderType:<6}"
            f" @ {price:>10,.4f}   {t.orderStatus.status:<12} {o.account}"
        )
    return 0


async def cmd_warm_cache(args: argparse.Namespace) -> int:
    """Pre-fetch PDH/PDL/ADR/ADV and the RVOL volume curve for a whole
    watchlist into BarCache (data/bars), so a symbol added mid-session in
    Live hits disk instead of IBKR.

    Meant to run overnight via cron against a broad watchlist (hundreds of
    tickers) that's mostly a research/screening list, not the smaller set
    actually tracked live -- so a name spotted moving pre-market (e.g. via
    Deepvue's RVOL-sorted broad-watchlist view) can be added to Live without
    the ~20s/ticker cost of a cold fetch_volume_curve. Only fetch_levels and
    fetch_volume_curve run here -- fetch_bars (today's own bars) is useless
    to warm before today's bars exist.

    Concurrency/pacing default far more conservative than replay/live's:
    this is hundreds of symbols x up to ~20 requests each on a cold cache,
    which risks an IBKR pacing violation at replay's defaults (see
    ReplayDriver.request_pace_seconds). After the first run, only one new
    trading day rolls into each symbol's window per night, so subsequent
    runs are far cheaper and pacing matters less -- but the conservative
    default costs only time, not correctness, so it stays conservative
    every night rather than needing a separate "first run" mode.

    Finishes by pruning BarCache entries older than --retention-days,
    unless that date was ever an actual replay/live session (checked
    against --db's runs table) -- run nightly forever, this is what keeps
    disk usage from growing unbounded.
    """
    symbols = parse_symbols(args)
    if not symbols:
        logger.error("No tickers given. Use --tickers or --tickers-file.")
        return 2

    session = (
        date.fromisoformat(args.date)
        if args.date
        else next_trading_session(datetime.now(ET))
    )

    ib = IB()
    logger.info("Connecting to %s:%d...", args.host, args.port)
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)
    start = asyncio.get_running_loop().time()
    try:
        driver = ReplayDriver(
            ib,
            session,
            args.bar_size,
            Path(args.cache_dir),
            concurrency=args.concurrency,
            request_pace_seconds=args.request_pace,
        )
        logger.info(
            "Warming PDH/PDL/ADR/ADV for %d symbols (session=%s)...",
            len(symbols),
            session,
        )
        levels = await driver.fetch_levels(symbols, args.adr_sessions)
        logger.info(
            "Warming %d-session RVOL curves for %d symbols...",
            args.rvol_sessions,
            len(symbols),
        )
        curves = await driver.fetch_volume_curve(symbols, args.rvol_sessions)
    finally:
        ib.disconnect()
    elapsed = asyncio.get_running_loop().time() - start

    missing_levels = sorted(set(symbols) - set(levels))
    missing_curves = sorted(set(symbols) - set(curves))
    print(
        f"\nWarmed levels for {len(levels)}/{len(symbols)} symbols, "
        f"curves for {len(curves)}/{len(symbols)}, in {elapsed:.0f}s "
        f"(session={session}, cache={args.cache_dir})"
    )
    if missing_levels:
        print(f"No levels: {', '.join(missing_levels)}")
    if missing_curves:
        print(f"No curve : {', '.join(missing_curves)}")

    if not args.no_prune:
        protected: set[str] = set()
        db_path = Path(args.db)
        if db_path.exists():
            con = sqlite3.connect(db_path)
            protected = {
                row[0]
                for row in con.execute(
                    "SELECT DISTINCT json_extract(params_json, '$.session') FROM runs"
                )
                if row[0]
            }
            con.close()
        removed = driver.cache.prune(protected, args.retention_days, date.today())
        total_removed = sum(removed.values())
        if total_removed:
            print(
                f"Pruned {total_removed} cache entries older than "
                f"{args.retention_days}d (never an actual session): "
                f"{removed['levels']} levels, {removed['curve']} curve, "
                f"{removed['intraday']} intraday"
            )
    return 0


async def cmd_purge_db(args: argparse.Namespace) -> int:
    """Wipe every run and trigger from the store. No IB connection needed --
    this only touches the sqlite file.

    Rows are cleared in place (DELETE, not file replacement): replacing the
    file would strand any open DataGrip connection on the old inode, which
    the OS keeps serving stale rows from indefinitely with no error. See
    TriggerStore.purge_runs.

    Separate from `replay`/`live` (which used to carry a `--purge` flag) on
    purpose -- a routine rerun of a session never needs this (latest_triggers
    already supersedes an earlier run of the same session on its own), and a
    flag sitting on every routine invocation is exactly the shape of mistake
    that deletes months of trigger history because it seemed convenient in
    the moment. This command exists, but has to be reached for deliberately.
    """
    db_path = Path(args.db)
    store = TriggerStore(db_path)
    n_runs = store.conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    n_triggers = store.conn.execute("SELECT count(*) FROM triggers").fetchone()[0]
    if n_runs == 0:
        print(f"{db_path} has no runs to purge.")
        store.close()
        return 0

    print(
        f"This will PERMANENTLY delete {n_runs} run(s) and {n_triggers} "
        f"trigger(s) from {db_path} -- every session ever recorded, not "
        f"just one day. Any grades/notes on those triggers are lost too."
    )
    if not args.yes:
        reply = input("Type 'yes' to confirm: ").strip().lower()
        if reply != "yes":
            print("Aborted -- nothing deleted.")
            store.close()
            return 1

    removed = store.purge_runs()
    store.close()
    print(f"Purged {removed} run(s) from {db_path}.")
    return 0


def _with_open_positions(symbols: list[str], held: list[str]) -> tuple[list[str], list[str]]:
    """The scan list plus anything held that is not already on it.

    Returns (symbols, added). Order is preserved and the watchlist comes
    first, so a held name that is already on it keeps its place rather than
    being moved to the end.
    """
    added = [h for h in held if h not in symbols]
    return symbols + added, added


async def cmd_live(args: argparse.Namespace) -> int:
    symbols = parse_symbols(args)
    setups, kwargs = _setups_and_kwargs(args)
    scanner = Scanner({}, setups, kwargs)

    ib = IB()
    logger.info("Connecting to %s:%d...", args.host, args.port)
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)
    if args.client_id == 0:
        # Binds hand-entered orders to this session, so `trail` can move and
        # cancel the stops placed in IBKR Desktop (see astor.drivers.orders).
        ib.reqAutoOpenOrders(True)
    elif not args.dry_run_trail:
        logger.warning(
            "client ID %d is not 0: trail can't modify hand-entered stops", args.client_id
        )

    try:
        driver = LiveDriver(ib, Path(args.cache_dir), args.bar_size, args.dry_run_trail)
        driver.max_loss = args.max_loss
        driver.start_background()
        run_id = f"{driver.session:%Y%m%d}-live-{uuid.uuid4().hex[:8]}"
        logger.info("Live session target: %s (run_id=%s)", driver.session, run_id)

        store = TriggerStore(Path(args.db))
        store.start_run(
            run_id,
            "live",
            {
                "session": str(driver.session),
                "bar_size": args.bar_size,
                "setups": [s.name for s in setups],
# Every setup threshold actually in force, taken from the same
                # place the setups get them. Hand-listing these drifted: the
                # 2026-10-02 live run recorded neither min_leg_pct nor the
                # AVT thresholds, so its own record could not say what rules
                # it ran under -- which is the one question a run record
                # exists to answer.
                "setup_kwargs": _setup_kwargs(args),
            },
        )

        # Whatever is already held gets scanned, watchlist or not. A
        # position is the one symbol that cannot afford to go unwatched,
        # and the watchlist is rebuilt each morning from what looked
        # interesting the night before -- which is not the same question.
        #
        # Scanned, not tracked. Sell-rule tracking is for a trade being
        # managed now; a position carried for weeks would spend every
        # session re-announcing a +1R it passed long ago. Auto-tracking
        # positions *opened during the session* is the version worth having,
        # and is not this.
        held = sorted({p.contract.symbol for p in ib.positions() if p.position})
        symbols, extra = _with_open_positions(symbols, held)
        if extra:
            logger.info("Open positions not on the watchlist, adding: %s", ", ".join(extra))

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


_CONSOLE_COMMANDS = (
    "Commands: 'add SYM1,SYM2', 'remove|rem|rm SYM1,SYM2', 'track SYM [ENTRY STOP] [ACCOUNT]', "
    "'trail SYM [ENTRY STOP] [ACCOUNT] [1.5R] [PCT gb|avwap|be ...] [close 2.5R|close exit]' "
    "(e.g. 'trail XYZ 1.5R 35% gb 35% avwap 30% be close exit'; track takes the same), "
    "'untrail SYM [ACCOUNT]', 'untrack SYM [ACCOUNT]', "
    "'positions', 'trades', 'mute', 'unmute', 'list', 'quit'.\n"
    "Orders ([ACCOUNT] = ira/reg/ID): "
    "'buy|sell SYM [ACCOUNT] [QTY] stop P [when P] [lim P] [urgent] [mkt] [loss $]' "
    "(no QTY: astor sizes it from the stop and the max loss; Adaptive Normal unless "
    "told otherwise; when = a stop entry, lim = a price cap, both = stop-limit), "
    "'buy|sell SYM [ACCOUNT] orb [MINUTES] [lim P] [loss $]' (opening-range break), "
    "'enter SYM [ACCOUNT] l|s QTY ...' (the same, direction spelled out), "
    "'stop SYM [ACCOUNT] P', 'stop SYM 1/2 P1 1/2 P2' or 'stop SYM be', "
    "'cancel SYM [ACCOUNT]', 'exit SYM [ACCOUNT] [SHARES|1/3] [adn|adu|mkt|lmt P]'."
)

_ORDER_PARSERS = {
    "enter": parse_enter,
    "buy": lambda rest: parse_enter(rest, "long"),
    "sell": lambda rest: parse_enter(rest, "short"),
    "stop": parse_stop,
    "cancel": parse_cancel,
    "exit": parse_exit,
}


def _by_symbol(trades: dict) -> list:
    return sorted(trades.values(), key=lambda t: (t.symbol, t.account or ""))


async def _confirm(loop) -> bool:
    """The order prompt, for entries and exits alike: only y or yes, in
    any case, sends. Enter alone doesn't -- a stray one must not place or
    close a position."""
    answer = await loop.run_in_executor(None, input, "  send? [y/n] ")
    return answer.strip().lower() in ("y", "yes")


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
        + _CONSOLE_COMMANDS
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

        # 'exit' closes a position now, so only 'quit' leaves -- a bare
        # 'exit' typed out of habit must not take the session down with it.
        if cmd == "quit":
            break

        if cmd in _ORDER_PARSERS:
            try:
                req = _ORDER_PARSERS[cmd](rest)
            except CommandError as e:
                print(f"  {e}")
                continue
            if cmd in ("enter", "buy", "sell"):
                ticket = await driver.prepare_entry(req)
                if isinstance(ticket, str):
                    print(f"  {ticket}")
                    continue
                print(f"  {ticket.summary}")
                if not await _confirm(loop):
                    print("  not sent")
                    continue
                print(f"  {await driver.send_entry(ticket, scanner, run_id, store)}")
            elif cmd == "stop":
                print(f"  {await driver.set_stop(req, run_id, store)}")
            elif cmd == "cancel":
                print(f"  {await driver.cancel(req, run_id, store)}")
            else:
                planned = await driver.plan_exit(req)
                if isinstance(planned, str):
                    print(f"  {planned}")
                    continue
                print(f"  {planned.summary}")
                if not await _confirm(loop):
                    print("  not sent")
                    continue
                print(f"  {await driver.exit_position(req, run_id, store)}")
            continue

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
        elif cmd in ("remove", "rem", "rm"):
            symbols = split_symbols(rest)
            if not symbols:
                print("Usage: remove SYM1,SYM2,...")
                continue
            results = await driver.remove_symbols(symbols)
            for sym, status in results.items():
                print(f"  {sym}: {status}")
            print(f"Tracking {driver.symbol_count()}/{cap or '∞'}")
        elif cmd in ("track", "trail"):
            try:
                parsed = parse_trade_command(rest, cmd)
            except CommandError as e:
                print(f"  {e}")
                continue
            symbol, entry, stop, account = parsed.symbol, parsed.entry, parsed.stop, parsed.account
            if symbol not in driver.status()["live"] and symbol not in driver.status()["pending"]:
                room = cap - driver.symbol_count() if cap else None
                if room is not None and room < 1:
                    print(f"No room to {cmd} {symbol} (cap {cap}, currently {driver.symbol_count()}).")
                    continue
            start = driver.track if cmd == "track" else driver.trail
            msg = await start(scanner, symbol, entry, stop, run_id, store, account, plan=parsed.plan)
            print(f"  {msg}")
        elif cmd in ("untrack", "untrail"):
            # An account narrows it to that account's trade; without one,
            # every account's trade in the symbol goes. untrail ends trail
            # alone; untrack ends track and trail alike.
            words = rest.split()
            account = account_token(words.pop()) if len(words) > 1 and is_account(words[-1]) else None
            symbols = split_symbols(" ".join(words))
            if not symbols:
                print(f"Usage: {cmd} SYM1,SYM2,... [ACCOUNT]")
                continue
            try:
                account = driver.resolve_account(account)
            except CommandError as e:
                print(f"  {e}")
                continue
            stop = driver.untrack if cmd == "untrack" else driver.untrail
            for sym in symbols:
                removed = stop(sym, account)
                if not removed:
                    print(f"  {sym}: not {'tracked' if cmd == 'untrack' else 'trailed'}")
                    continue
                where = ", ".join(t.account or "?" for t in removed)
                print(f"  {sym}: {cmd}ed ({where})")
                # the rule had already fired its exit: untrailing can't recall it
                if any(t.state == "done" for t in removed):
                    print(f"  {sym}: trail had already sent its exit -- check the position")
        elif cmd in ("mute", "unmute"):
            driver.muted = cmd == "mute"
            print(
                "  Setup alerts muted: still shown, no bell. track/trail/orders still ring."
                if driver.muted else "  Setup alerts unmuted."
            )
        elif cmd in ("positions", "pos"):
            for line in await driver.position_lines():
                print(line)
        elif cmd == "trades":
            for line in format_trades(await todays_trades(driver.ib, store, driver.session), _pnl_color):
                print(line)
        elif cmd in ("list", "status", "tickers"):
            tracked = driver.status()
            total = len(tracked["live"]) + len(tracked["pending"])
            print(f"Tracking {total}/{cap or '∞'}" + ("  (setup alerts muted)" if driver.muted else ""))
            if tracked["live"]:
                print(f"  live    ({len(tracked['live'])}): {', '.join(tracked['live'])}")
            if tracked["pending"]:
                print(
                    f"  pending ({len(tracked['pending'])}): "
                    f"{', '.join(tracked['pending'])}"
                )
            if not total:
                print("  (none yet)")
            if driver.tracker.trades:
                print(f"  sell-rule tracking ({len(driver.tracker.trades)}):")
                for trade in _by_symbol(driver.tracker.trades):
                    sym = trade.symbol
                    peak = f" peak {trade.peak:.2f}" if trade.peak is not None else ""
                    where = f" [{trade.account}]" if trade.account else ""
                    print(
                        f"    {sym:<6} {trade.direction:<5} {trade.state:<8} "
                        f"entry {trade.entry:.2f} stop {trade.stop:.2f} R {trade.r:.2f}{peak}{where}"
                    )
            if driver.trailer.trades:
                dry = ", dry run" if driver.executor.dry_run else ""
                print(f"  trail ({len(driver.trailer.trades)}{dry}):")
                for trade in _by_symbol(driver.trailer.trades):
                    sym = trade.symbol
                    print(
                        f"    {sym:<6} {trade.direction:<5} {trade.state:<8} "
                        f"entry {trade.entry:.2f} stop {trade.stop:.2f} R {trade.r:.2f}"
                        f" peak {trade.peak:.2f} eod {trade.eod_exit:%H:%M} [{trade.account}]"
                    )
        else:
            print(_CONSOLE_COMMANDS)


def _add_avt_args(p) -> None:
    """AVT-L/AVT-S thresholds, shared by replay/live/explain. One helper
    rather than the flag-by-flag repetition the Reversal arguments still use
    across the three -- seven knobs times three subparsers is where that
    pattern stops paying.
    """
    p.add_argument(
        "--min-avt-day-range-adr",
        type=float,
        default=0.8,
        help="Minimum day range, in ADR(14) multiples, for the move to be "
        "worth joining (AVT).",
    )
    p.add_argument(
        "--avt-touch-band-adr",
        type=float,
        default=0.05,
        help="How close the pullback wick must come to AVWAP, as a fraction of "
        "today's range. Symmetric -- either side counts as a touch (AVT).",
    )
    p.add_argument(
        "--min-avt-depth-adr",
        type=float,
        default=0.15,
        help="Minimum pullback depth from the day's extreme, as a fraction of "
        "today's range (AVT).",
    )
    p.add_argument(
        "--min-avt-day-range-pct",
        type=float,
        default=6.0,
        help="Second route to a big-enough day: a range of at least this "
        "percent qualifies even below --min-avt-day-range-adr (0 = off).",
    )
    p.add_argument(
        "--min-avt-day-range-pct-adr",
        type=float,
        default=0.6,
        help="ADR floor that still applies on the percent route (AVT).",
    )
    p.add_argument(
        "--avt-max-yard-pct",
        type=float,
        default=10.0,
        help="Cap on the day's range as a yardstick, in percent of price. "
        "Past this the pullback distances stop scaling with the day -- a "
        "tenth of a 25%% range is not a tight anything (AVT).",
    )
    p.add_argument(
        "--avt-recovery-adr",
        type=float,
        default=0.05,
        help="How far past the touch the bar must close, as a fraction of "
        "today's range, to count as the continuation. Measured from AVWAP "
        "instead when the touch overshot through it, and it must CROSS the "
        "target, not already sit past it (AVT).",
    )
    p.add_argument(
        "--avt-max-bars-since-touch",
        type=int,
        default=5,
        help="How many bars the recovery may lag the touch; 0 means one bar "
        "did both (AVT).",
    )
    p.add_argument(
        "--avt-max-break-adr",
        type=float,
        default=0.05,
        help="A close this far the wrong side of AVWAP, as a fraction of today's "
        "range, kills the leg until a new extreme -- this is a touch setup, "
        "not a breakdown (AVT).",
    )
    p.add_argument(
        "--avt-rearm-adr",
        type=float,
        default=0.15,
        help="A dead leg comes back once a close is this far past AVWAP the "
        "trade's way, as a fraction of today's range -- the next touch is a new "
        "interaction, not waiting for a new extreme. Negative turns it off (AVT).",
    )
    p.add_argument(
        "--avt-earliest-touch",
        default="09:34",
        help="Earliest HH:MM ET a touch counts -- AVWAP sits on top of price "
        "for the first few minutes, so everything touches it before the day "
        "has shape (AVT).",
    )
    p.add_argument(
        "--avt-require-deeper-touch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Only alert or fire on a pullback that went deeper than the one "
        "already acted on, until a new extreme resets the leg. A leg nudging "
        "AVWAP at the same depth over and over is one setup, not six (AVT).",
    )
    p.add_argument(
        "--avt-exit-depth-mult",
        type=float,
        default=1.5,
        help="Where AVT expects to be out, as a multiple of the pullback's "
        "own depth projected from the touch -- the reward half of R:R (AVT).",
    )
    p.add_argument(
        "--avt-stop-adr",
        type=float,
        default=0.06,
        help="Where the stop sits: this much of today's range the far side of "
        "AVWAP. Same distance that kills the leg, so stop and invalidation "
        "agree (AVT).",
    )


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
        "--setups", default="", help="Comma separated, e.g. 'REV-L'. Default: all"
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
        "--min-leg-adr",
        type=float,
        default=0.9,
        help="Minimum directional-leg size, in ADR units, for both the final "
        "trigger and the earlier capitulation-in-progress alert "
        "(1.0 would mean the leg must be at least the symbol's full ADR).",
    )
    r.add_argument(
        "--min-leg-minutes",
        type=float,
        default=4.0,
        help="Minimum time the leg itself took to build, anchor to extreme -- "
        "for both the final trigger and the capitulation alert. Below this "
        "it's too fast to trust as a real leg.",
    )
    r.add_argument(
        "--min-ema9-extension-adr",
        type=float,
        default=0.125,
        help="Minimum distance the leg's extreme finished from the frozen "
        "9EMA (1-min), in ADR units, for both the final trigger and the "
        "capitulation alert (0 = off).",
    )
    r.add_argument(
        "--min-accel-price",
        type=float,
        # Off since 2026-10-20: a 17-session replay found the gate dropped
        # triggers as good as the ones it kept (CLF that day among them).
        # Still computed and recorded on every trigger.
        default=0.0,
        help="Minimum accel_price (net close-to-close speed, last "
        "accel-window bars vs. the rest of the leg's) for both the final "
        "trigger and the capitulation alert (0 = off).",
    )
    r.add_argument(
        "--min-accel-vol",
        type=float,
        # Off since 2026-10-20: a 17-session replay found the gate dropped
        # triggers as good as the ones it kept (CLF that day among them).
        # Still computed and recorded on every trigger.
        default=0.0,
        help="Minimum accel_vol (average volume, last accel-window bars vs. "
        "the rest of the leg's) for both the final trigger and the "
        "capitulation alert (0 = off).",
    )
    r.add_argument(
        "--accel-window",
        type=int,
        default=3,
        help="Bars used for the accel_price/accel_vol diagnostics: the last "
        "N bars' net close-to-close speed (accel_price) and average volume "
        "(accel_vol) against the rest of the leg's.",
    )
    r.add_argument(
        "--min-retrace-pct",
        type=float,
        default=10.0,
        help="Minimum retracement of the leg, as %% of the leg's size, to "
        "count as a real reversal -- OR'd with an EMA9 reclaim (whichever "
        "comes first).",
    )
    r.add_argument(
        "--max-retrace-pct",
        type=float,
        default=50.0,
        help="Maximum retracement of the leg, as %% of the leg's size -- "
        "past this the move has round-tripped (too late) regardless of an "
        "EMA9 reclaim, and the leg is dead until a new, deeper extreme forms.",
    )
    r.add_argument(
        "--min-confirm-minutes",
        type=float,
        default=0.0,
        help="Minutes that must elapse since the leg's extreme before a "
        "retracement in the target band is trusted (0 = the very next bar "
        "can confirm).",
    )
    r.add_argument(
        "--max-loss",
        type=float,
        default=100.0,
        help="Max loss in dollars a stop-out is assumed to cost, for the "
        "position-size diagnostics recorded on each trigger (both a stop at "
        "PDH/PDL and a stop at the session's actual high/low are sized).",
    )
    _add_avt_args(r)
    r.add_argument(
        "--min-alert-move-adr",
        type=float,
        default=0.01,
        help="How far the capitulation alert's target must move, in ADR "
        "units, to re-alert while the leg is still live. The stop re-alerts "
        "on any move; this gates the target alone, which drifts with the "
        "9EMA (0 = re-alert on any change).",
    )
    r.add_argument(
        "--min-leg-pct",
        type=float,
        default=6.0,
        help="Second route to a big-enough leg: a move of at least this "
        "percent qualifies even below --min-leg-adr, which otherwise "
        "under-reads a high-ADR name (0 = off).",
    )
    r.add_argument(
        "--min-leg-pct-adr",
        type=float,
        default=0.6,
        help="ADR floor that still applies on the percent route, so a "
        "sleepy name's drift cannot qualify on percent alone.",
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
        "--setups", default="", help="Comma separated, e.g. 'REV-L'. Default: all"
    )
    lv.add_argument(
        "--min-leg-adr",
        type=float,
        default=0.9,
        help="Minimum directional-leg size, in ADR units, for both the final "
        "trigger and the earlier capitulation-in-progress alert.",
    )
    lv.add_argument(
        "--min-leg-minutes",
        type=float,
        default=4.0,
        help="Minimum time the leg itself took to build, anchor to extreme -- "
        "for both the final trigger and the capitulation alert.",
    )
    lv.add_argument(
        "--min-ema9-extension-adr",
        type=float,
        default=0.125,
        help="Minimum distance the leg's extreme finished from the frozen "
        "9EMA (1-min), in ADR units, for both the final trigger and the "
        "capitulation alert (0 = off).",
    )
    lv.add_argument(
        "--min-accel-price",
        type=float,
        # Off since 2026-10-20: a 17-session replay found the gate dropped
        # triggers as good as the ones it kept (CLF that day among them).
        # Still computed and recorded on every trigger.
        default=0.0,
        help="Minimum accel_price for both the final trigger and the "
        "capitulation alert (0 = off).",
    )
    lv.add_argument(
        "--min-accel-vol",
        type=float,
        # Off since 2026-10-20: a 17-session replay found the gate dropped
        # triggers as good as the ones it kept (CLF that day among them).
        # Still computed and recorded on every trigger.
        default=0.0,
        help="Minimum accel_vol for both the final trigger and the "
        "capitulation alert (0 = off).",
    )
    lv.add_argument(
        "--accel-window",
        type=int,
        default=3,
        help="Bars used for the accel_price/accel_vol diagnostics.",
    )
    lv.add_argument(
        "--min-retrace-pct",
        type=float,
        default=10.0,
        help="Minimum retracement of the leg, as %% of the leg's size, to "
        "count as a real reversal -- OR'd with an EMA9 reclaim.",
    )
    lv.add_argument(
        "--max-retrace-pct",
        type=float,
        default=50.0,
        help="Maximum retracement of the leg, as %% of the leg's size, "
        "before the leg is dead until a new extreme forms -- regardless of "
        "an EMA9 reclaim.",
    )
    lv.add_argument(
        "--min-confirm-minutes",
        type=float,
        default=0.0,
        help="Minutes since the leg's extreme before a retracement in the "
        "target band is trusted (0 = the very next bar can confirm).",
    )
    lv.add_argument(
        "--max-loss",
        type=float,
        default=100.0,
        help="Max loss in dollars a stop-out is assumed to cost, for the "
        "position-size diagnostics on each trigger (both a stop at PDH/PDL "
        "and a stop at the session's actual high/low are sized).",
    )
    lv.add_argument(
        "--min-alert-move-adr",
        type=float,
        default=0.01,
        help="How far the capitulation alert's target must move, in ADR "
        "units, to re-alert while the leg is still live. The stop re-alerts "
        "on any move; this gates the target alone, which drifts with the "
        "9EMA (0 = re-alert on any change).",
    )
    lv.add_argument(
        "--min-leg-pct",
        type=float,
        default=6.0,
        help="Second route to a big-enough leg: a move of at least this "
        "percent qualifies even below --min-leg-adr, which otherwise "
        "under-reads a high-ADR name (0 = off).",
    )
    lv.add_argument(
        "--min-leg-pct-adr",
        type=float,
        default=0.6,
        help="ADR floor that still applies on the percent route, so a "
        "sleepy name's drift cannot qualify on percent alone.",
    )
    _add_avt_args(lv)
    lv.add_argument(
        "--max-tickers",
        type=int,
        # IBKR's documented limit on live (keepUpToDate) bar requests is 50,
        # but the early spike ran 70 without trouble; 55 since 2026-10-15,
        # raised step by step while it holds.
        default=55,
        help="Cap enforced by the add/remove console (0 = no cap)",
    )
    lv.add_argument("--db", default="data/astor.db")
    lv.add_argument("--cache-dir", default="data/bars")
    lv.add_argument("--host", default="127.0.0.1")
    lv.add_argument("--port", type=int, default=4001)
    # 0, not a number of its own like every other command: client 0 is the
    # one IBKR binds hand-entered orders to, and `trail` has to modify and
    # cancel those stops. Still distinct from replay's, so a live run and a
    # replay backtest can share one gateway.
    lv.add_argument("--client-id", type=int, default=0)
    lv.add_argument(
        "--dry-run-trail",
        action="store_true",
        help="trail reads stops and positions but places, moves and cancels "
        "nothing -- it prints what it would have done",
    )

    ex = sub.add_parser(
        "explain",
        help="Show, bar by bar, why one setup did or did not fire for one "
        "symbol on one day -- the real detection code, narrated",
    )
    ex.add_argument("--date", required=True, help="Market day, YYYY-MM-DD")
    ex.add_argument("--ticker", required=True, help="Single symbol")
    ex.add_argument(
        "--setup", required=True, help=f"One of: {', '.join(SETUPS_BY_NAME)}"
    )
    ex.add_argument("--adr-sessions", type=int, default=14)
    ex.add_argument("--min-leg-adr", type=float, default=0.9)
    ex.add_argument("--min-leg-minutes", type=float, default=4.0)
    ex.add_argument("--min-ema9-extension-adr", type=float, default=0.125)
    ex.add_argument("--min-accel-price", type=float, default=0.0)  # off, as in live
    ex.add_argument("--min-accel-vol", type=float, default=0.0)
    ex.add_argument("--accel-window", type=int, default=3)
    ex.add_argument("--min-retrace-pct", type=float, default=10.0)
    ex.add_argument("--max-retrace-pct", type=float, default=50.0)
    ex.add_argument("--min-confirm-minutes", type=float, default=0.0)
    ex.add_argument(
        "--min-alert-move-adr",
        type=float,
        default=0.01,
        help="How far the capitulation alert's target must move, in ADR "
        "units, to re-alert while the leg is still live. The stop re-alerts "
        "on any move; this gates the target alone, which drifts with the "
        "9EMA (0 = re-alert on any change).",
    )
    ex.add_argument(
        "--min-leg-pct",
        type=float,
        default=6.0,
        help="Second route to a big-enough leg: a move of at least this "
        "percent qualifies even below --min-leg-adr, which otherwise "
        "under-reads a high-ADR name (0 = off).",
    )
    ex.add_argument(
        "--min-leg-pct-adr",
        type=float,
        default=0.6,
        help="ADR floor that still applies on the percent route, so a "
        "sleepy name's drift cannot qualify on percent alone.",
    )
    ex.add_argument("--max-loss", type=float, default=100.0)
    _add_avt_args(ex)
    ex.add_argument(
        "--db", default="data/astor.db", help="Read only, for the alerts live actually sent"
    )
    ex.add_argument("--cache-dir", default="data/bars")
    ex.add_argument("--host", default="127.0.0.1")
    ex.add_argument("--port", type=int, default=4001)
    # Distinct from replay/live/warm-cache/purge-db's client IDs.
    ex.add_argument("--client-id", type=int, default=25)

    pr = sub.add_parser(
        "profile",
        help="Interval-by-interval RVOL and ADR usage for one symbol on one "
        "day -- the tape the filters were read from",
    )
    pr.add_argument("--date", required=True, help="Market day, YYYY-MM-DD")
    pr.add_argument("--ticker", required=True, help="Single symbol")
    pr.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Minutes per row (default: 5; 1 for minute-by-minute)",
    )
    pr.add_argument("--start", default="", help="First interval to print, HH:MM ET (default: open)")
    pr.add_argument("--end", default="", help="Stop before this HH:MM ET (default: close)")
    pr.add_argument("--adr-sessions", type=int, default=ADR_SESSIONS)
    pr.add_argument("--rvol-sessions", type=int, default=RVOL_SESSIONS)
    pr.add_argument("--cache-dir", default="data/bars")
    pr.add_argument("--host", default="127.0.0.1")
    pr.add_argument("--port", type=int, default=4001)
    # Distinct from replay/live/warm-cache/purge-db/explain's client IDs.
    pr.add_argument("--client-id", type=int, default=26)

    po = sub.add_parser(
        "positions",
        help="Show open positions from both of IBKR's feeds plus working "
        "orders -- read-only, for checking whether a position that vanished "
        "from the desktop Portfolio view is really gone",
    )
    po.add_argument("--host", default="127.0.0.1")
    po.add_argument("--port", type=int, default=4001)
    # Distinct from every other command's client ID.
    po.add_argument("--client-id", type=int, default=27)

    tr = sub.add_parser(
        "trades",
        help="Today's trades from IBKR's fills -- entry/exit times and prices, "
        "shares, the original stop, P&L -- read-only",
    )
    tr.add_argument("--date", default="", help="YYYY-MM-DD; IBKR keeps fills for the current day")
    tr.add_argument("--db", default="data/astor.db")
    tr.add_argument("--host", default="127.0.0.1")
    tr.add_argument("--port", type=int, default=4001)
    # Distinct from every other command's client ID.
    tr.add_argument("--client-id", type=int, default=28)

    wc = sub.add_parser(
        "warm-cache",
        help="Pre-fetch PDH/PDL/ADR/ADV and RVOL curves for a watchlist into "
        "BarCache, so a mid-session Live add hits disk instead of IBKR",
    )
    wc.add_argument("--tickers", default="", help="Comma or space separated")
    wc.add_argument(
        "--tickers-file", default="", help="e.g. a broad research/screening watchlist"
    )
    wc.add_argument(
        "--date",
        default="",
        help="Session to warm for, YYYY-MM-DD (default: the next trading day -- "
        "what a cron run the night before wants)",
    )
    wc.add_argument(
        "--bar-size",
        default="1 min",
        help="Must match what Replay/Live will request, or the cache misses",
    )
    wc.add_argument("--adr-sessions", type=int, default=ADR_SESSIONS)
    wc.add_argument("--rvol-sessions", type=int, default=RVOL_SESSIONS)
    wc.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Concurrent IBKR historical-data requests (default 3, well below "
        "Replay/Live's 10 -- a cold run across hundreds of symbols is the "
        "case that actually risks an IBKR pacing violation)",
    )
    wc.add_argument(
        "--request-pace",
        type=float,
        default=0.5,
        help="Seconds to wait after each historical-data request completes, "
        "per concurrency slot, before that slot takes another (default 0.5; "
        "0 disables pacing entirely)",
    )
    wc.add_argument(
        "--retention-days",
        type=int,
        default=30,
        help="Delete cache entries older than this many days, unless that "
        "date was an actual replay/live session (--db's runs table) -- "
        "covers the RVOL window (20 sessions) plus weekends/holidays with "
        "margin (default 30)",
    )
    wc.add_argument(
        "--no-prune",
        action="store_true",
        help="Skip pruning old cache entries entirely",
    )
    wc.add_argument("--db", default="data/astor.db", help="Only read, to find protected session dates")
    wc.add_argument("--cache-dir", default="data/bars")
    wc.add_argument("--host", default="127.0.0.1")
    wc.add_argument("--port", type=int, default=4001)
    # Distinct from replay's (21) and live's (22) so a scheduled overnight
    # warm-cache run never clashes with an interactive session left open.
    wc.add_argument("--client-id", type=int, default=23)

    pd = sub.add_parser(
        "purge-db",
        help="Permanently delete every run and trigger from the store -- "
        "the whole database, not just one session. Only needed after a "
        "schema change (new/renamed extras fields); a same-day rerun never "
        "needs this, since latest_triggers already supersedes old runs of "
        "the same session on its own.",
    )
    pd.add_argument("--db", default="data/astor.db")
    pd.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (for scripted use)",
    )

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
    cmd = {
        "replay": cmd_replay,
        "live": cmd_live,
        "warm-cache": cmd_warm_cache,
        "purge-db": cmd_purge_db,
        "explain": cmd_explain,
        "profile": cmd_profile,
        "positions": cmd_positions,
        "trades": cmd_trades,
    }[args.command]
    raise SystemExit(asyncio.run(cmd(args)))


if __name__ == "__main__":
    main()
