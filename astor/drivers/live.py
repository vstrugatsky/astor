"""Live driver: feed the Scanner a real-time bar stream from IBKR, with
tickers added and removed while the session runs rather than fixed at start.

Reference-data fetching (PDH/PDL/ADR, the RVOL volume curve) is identical to
Replay's -- both are anchored to `session`, the trading day being scanned, not
to "now" -- so this composes a `ReplayDriver` for that part instead of
duplicating it. What's actually new here is: a symbol can be added hours
before the open (reference data only, no bars exist yet) or after it (which
also needs today-so-far backfilled through the Scanner before going live),
and either way it ends up on the same real-time subscription.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from pathlib import Path

from ib_async import IB

from astor.core.grading import NOTIFY_GRADE_THRESHOLD
from astor.core.scanner import Scanner
from astor.core.store import TriggerStore
from astor.core.tracking import SellRuleTracker, TrackAlert, TrackedTrade
from astor.core.types import BarEvent, Trigger
from astor.drivers.replay import (
    ET,
    ReplayDriver,
    next_trading_session,
    parse_bar_seconds,
    to_et,
)

logger = logging.getLogger(__name__)

RTH_OPEN = time(9, 30)

# How long to wait between activating successive symbols in a batch (open
# transition, or a multi-symbol post-open `add`). `_subscribe`'s
# reqHistoricalData(keepUpToDate=True) call is never gated by the request
# semaphore the way every other IB call in this codebase is -- it returns
# immediately rather than awaiting a response, so semaphore-style throttling
# doesn't apply to it. Firing 30 of those plus their backfills at once (as
# happened at the 2026-08-20 open) appears to overwhelm IB: every symbol in
# that batch timed out. Pacing activation out, symbol by symbol, is the fix.
ACTIVATION_PACE_SECONDS = 0.75

# Backfill is opportunistic -- AVWAP/RVOL continuity, not correctness-critical
# the way a stuck subscribe would be -- so it gets a much shorter timeout than
# REQUEST_TIMEOUT. A backfill that's going to fail should fail fast, not eat
# most of a second per symbol out of an already-paced activation batch.
BACKFILL_TIMEOUT = 15.0


def _format_trigger_line(t: Trigger) -> str:
    """Everything Notify shows for a fired trigger: the headline, then the
    two stop/position-size framings (technical level vs. the session's
    actual extreme) side by side, so a wide excursion's real risk is visible
    right next to the tighter, level-only view.
    """
    e = t.extras
    auto_grade = e.get("auto_grade")
    avwap_pct, adr_pct, rvol = e.get("avwap_pct"), e.get("adr_pct"), e.get("rvol")
    level_pct = e.get("reclaim_pct")  # already a clean "how far the good way" magnitude
    level_shares, level_dollars = e.get("level_stop_shares"), e.get("level_stop_dollars")
    session_extreme = e.get("session_extreme")
    session_pct = e.get("session_extreme_pct")
    session_shares, session_dollars = e.get("session_stop_shares"), e.get("session_stop_dollars")

    toward = "below" if t.direction == "short" else "above"
    # CDH/CDL (current-day high/low) -- same 3-letter shape as PDH/PDL so the
    # two lines line up.
    session_label = "CDH" if t.direction == "short" else "CDL"

    def signed(v: float | None) -> str:
        return f"{v:+.2f}%" if v is not None else "n/a"

    def magnitude(v: float | None) -> str:
        return f"{abs(v):.2f}%" if v is not None else "n/a"

    def sized(shares: int | None, dollars: float | None) -> str:
        return f"{shares} sh (${dollars:,.0f})" if shares is not None else "no size (risk too wide)"

    lines = [
        f"[{t.bar_start:%H:%M:%S} ET] TRIGGER {t.symbol:<6} {t.setup:<4} {t.direction:<5} "
        f"grade {auto_grade if auto_grade is not None else 'n/a'}/4"
        f"   @ {t.trigger_price:.2f}   avwap {signed(avwap_pct)}   adr {magnitude(adr_pct)}"
        f"   rvol {rvol if rvol is not None else 'n/a'}",
        f"    {t.level_name:<3} {magnitude(level_pct)} {toward}, stop {t.level_value:.2f}"
        f"  ->  {sized(level_shares, level_dollars)}",
    ]
    if session_extreme is not None:
        lines.append(
            f"    {session_label:<3} {magnitude(session_pct)} {toward}, stop {session_extreme:.2f}"
            f"  ->  {sized(session_shares, session_dollars)}"
        )
    return "\n".join(lines)


# Terminal bell -- most terminals (Terminal.app, iTerm2) sound it without any
# extra dependency, but it's a per-terminal setting: check yours isn't set to
# silent or visual-only if you don't hear it.
_BELL = "\a"
_ANSI_BOLD = "\033[1m"
_ANSI_GREEN = "\033[92m"
_ANSI_RED = "\033[91m"
_ANSI_RESET = "\033[0m"

# Distinct from the setup-trigger palette above, so a trade alert (this
# trader's own open position) is visually distinguishable at a glance from a
# fresh setup trigger (a new candidate). Bold + plain black (30) renders as
# "bright black"/gray in most modern terminals rather than true black, so it
# stays visible on both light- and dark-background terminals; a literal
# black would risk vanishing on a dark background.
_ANSI_BLACK = "\033[30m"
_ANSI_BROWN = "\033[38;5;130m"


def _print_trigger_alert(t: Trigger) -> None:
    """Bell + bold color, not just a plain print -- a trigger scrolling by
    silently in a wall of other output is exactly what this is for."""
    color = _ANSI_GREEN if t.direction == "long" else _ANSI_RED
    print(f"{_BELL}{_ANSI_BOLD}{color}{_format_trigger_line(t)}{_ANSI_RESET}")


def _format_track_alert(alert: TrackAlert, trade: TrackedTrade) -> str:
    header = f"[{alert.bar_start:%H:%M:%S} ET] TRACK {alert.symbol:<6} {trade.direction:<5}"
    if alert.kind == "breakeven":
        return (
            f"{header} +1R reached @ {alert.close:.2f} (target {trade.target_1r:.2f})"
            f"  ->  move stop to breakeven ({trade.entry:.2f})"
        )
    assert trade.peak is not None
    return (
        f"{header} SELL -- gave back 0.5R from peak {trade.peak:.2f} to {alert.close:.2f}"
        f"  (entry {trade.entry:.2f}, stop {trade.stop:.2f}, R {trade.r:.2f})"
    )


def _print_track_alert(alert: TrackAlert, trade: TrackedTrade) -> None:
    color = _ANSI_BLACK if alert.kind == "breakeven" else _ANSI_BROWN
    print(f"{_BELL}{_ANSI_BOLD}{color}{_format_track_alert(alert, trade)}{_ANSI_RESET}")


async def resolve_open_trade(ib: IB, symbol: str) -> tuple[float, float, str] | None:
    """Look up entry price and protective stop for an open position in
    `symbol`, across every account this IBKR login can see. Returns
    (entry, stop, account), or None if it can't be resolved unambiguously --
    no position, more than one position, no single matching working stop
    order, or a stop that sits on the wrong side of entry for the position's
    direction (stale/unrelated order rather than this trade's actual stop).

    Entry comes from `ib.positions()` (kept live-synced by ib_async once
    connected, no extra request needed). The stop comes from a fresh
    `reqAllOpenOrders` -- unlike `ib.openOrders()`, which only sees orders
    placed via this API client, `reqAllOpenOrders` also sees orders entered
    by hand in TWS/Gateway, which is how this trader's stops are placed.
    """
    positions = [
        p for p in ib.positions() if p.contract.symbol == symbol and p.position != 0
    ]
    if len(positions) != 1:
        return None
    pos = positions[0]
    entry = pos.avgCost
    long = pos.position > 0
    exit_side = "SELL" if long else "BUY"

    open_trades = await ib.reqAllOpenOrdersAsync()
    stops = [
        t.order
        for t in open_trades
        if t.contract.symbol == symbol
        and t.order.account == pos.account
        and t.order.orderType == "STP"
        and t.order.action == exit_side
    ]
    if len(stops) != 1:
        return None
    stop = stops[0].auxPrice
    if (stop < entry) != long:
        return None
    return entry, stop, pos.account


class LiveDriver:
    def __init__(self, ib: IB, cache_dir: Path, bar_size: str = "1 min"):
        self.ib = ib
        self.bar_size = bar_size
        self.bar_seconds = parse_bar_seconds(bar_size)
        self.session = next_trading_session(datetime.now(ET))
        # Composition, not inheritance: fetch_bars/stream/run_replay are
        # replay-specific and don't apply here, but qualify/fetch_levels/
        # fetch_volume_curve/BarCache/the request semaphore all do.
        self._ref = ReplayDriver(ib, self.session, bar_size, cache_dir)

        self._pending: set[str] = set()  # reference data fetched, not yet live
        self._subs: dict[str, object] = {}  # symbol -> BarDataList
        self._last_bar_start: dict[str, datetime] = {}
        self.tracker = SellRuleTracker()

    def symbol_count(self) -> int:
        return len(self._pending) + len(self._subs)

    def status(self) -> dict[str, list[str]]:
        """Symbols currently tracked, split by pending (reference data
        fetched, not yet subscribed -- pre-open) vs live (backfilled and
        subscribed)."""
        return {"pending": sorted(self._pending), "live": sorted(self._subs)}

    # -- adding / removing ---------------------------------------------

    async def add_symbols(
        self,
        scanner: Scanner,
        symbols: list[str],
        run_id: str,
        store: TriggerStore,
    ) -> dict[str, str]:
        """Add one or more symbols. Returns {symbol: "pending" | "live" | "failed"}.

        "pending" means reference data (levels, volume curve) is fetched and
        cached, but it's before the open, so there's nothing to backfill and
        no subscription yet -- `run_open_transition` activates it at 09:30 ET.
        "live" means it's already backfilled and subscribed.
        """
        results: dict[str, str] = {}

        contracts = await self._ref.qualify(symbols)
        for sym in symbols:
            if sym not in contracts:
                results[sym] = "failed"
        ok = [s for s in symbols if s in contracts]
        if not ok:
            return results

        levels, curves = await asyncio.gather(
            self._ref.fetch_levels(ok), self._ref.fetch_volume_curve(ok)
        )

        before_open = datetime.now(ET).time() < RTH_OPEN
        to_activate = []
        for sym in ok:
            if sym not in levels:
                results[sym] = "failed"
                continue
            scanner.levels[sym] = levels[sym]
            if sym in curves:
                scanner.curves[sym] = curves[sym]
            else:
                logger.warning("No volume curve for %s; its RVOL will read None", sym)
            if before_open:
                self._pending.add(sym)
                results[sym] = "pending"
            else:
                to_activate.append(sym)

        if to_activate:
            await self._activate_all(scanner, contracts, to_activate, run_id, store)
            for s in to_activate:
                results[s] = "live"

        return results

    async def remove_symbols(self, symbols: list[str]) -> dict[str, str]:
        """Cancel a symbol's subscription (or drop it before it ever got
        one). Leaves scanner.levels/indicators/curves alone -- inert without
        new bars arriving, and a quick re-add doesn't need to refetch
        anything already cached.
        """
        results: dict[str, str] = {}
        for sym in symbols:
            if sym in self._pending:
                self._pending.discard(sym)
                results[sym] = "removed (was pending)"
            elif sym in self._subs:
                self.ib.cancelHistoricalData(self._subs.pop(sym))
                self._last_bar_start.pop(sym, None)
                results[sym] = "removed (was live)"
            else:
                results[sym] = "not found"
        return results

    # -- sell-rule tracking -------------------------------------------------

    async def track(
        self,
        scanner: Scanner,
        symbol: str,
        entry: float | None,
        stop: float | None,
        run_id: str,
        store: TriggerStore,
    ) -> str:
        """Start sell-rule tracking for a position in `symbol`. `entry` and
        `stop` are either both given (trusted as-is, no IBKR lookup -- the
        only path that works for an account this login can't see) or both
        None, in which case both are resolved from IBKR.

        Ensures `symbol` has a live bar subscription, same as `add_symbols`
        -- tracking needs the same 1-min closes a scanned symbol gets.
        """
        account = None
        if entry is None and stop is None:
            resolved = await resolve_open_trade(self.ib, symbol)
            if resolved is None:
                return (
                    f"Could not resolve a single open position + matching stop "
                    f"order for {symbol} from IBKR. Give both explicitly: "
                    f"'track {symbol} ENTRY STOP'."
                )
            entry, stop, account = resolved

        if symbol not in self._subs and symbol not in self._pending:
            results = await self.add_symbols(scanner, [symbol], run_id, store)
            if results.get(symbol) == "failed":
                return f"Could not subscribe {symbol} for bars; tracking not started."

        trade = self.tracker.track(symbol, entry, stop, account)
        where = f" [{account}]" if account else ""
        return (
            f"Tracking {symbol} {trade.direction}: entry {entry:.2f} stop {stop:.2f} "
            f"R={trade.r:.2f} (1R target {trade.target_1r:.2f}){where}"
        )

    # -- open transition --------------------------------------------------

    async def run_open_transition(
        self, scanner: Scanner, run_id: str, store: TriggerStore
    ) -> None:
        """Sleeps until this session's 09:30 ET open, then activates every
        symbol still pending (added the night before or pre-market) in one
        batch. Adds after this point go straight through `add_symbols`'s
        post-open branch instead.
        """
        open_dt = datetime.combine(self.session, RTH_OPEN, tzinfo=ET)
        delay = (open_dt - datetime.now(ET)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

        to_activate = list(self._pending)
        if not to_activate:
            return
        contracts = await self._ref.qualify(to_activate)
        self._pending.clear()
        await self._activate_all(scanner, contracts, to_activate, run_id, store)
        logger.info("Open transition: %d symbol(s) now live", len(to_activate))

    # -- activation: backfill + subscribe ----------------------------------

    async def _activate_all(
        self,
        scanner: Scanner,
        contracts: dict[str, object],
        symbols: list[str],
        run_id: str,
        store: TriggerStore,
    ) -> None:
        """Activate a batch of symbols one at a time, paced by
        ACTIVATION_PACE_SECONDS -- see that constant's comment for why this
        can't just be asyncio.gather'd like every other batch IB call here.
        """
        for sym in symbols:
            contract = contracts.get(sym)
            if contract is None:
                continue
            await self._activate(scanner, contract, sym, run_id, store)
            await asyncio.sleep(ACTIVATION_PACE_SECONDS)

    async def _activate(
        self, scanner: Scanner, contract, symbol: str, run_id: str, store: TriggerStore
    ) -> None:
        open_dt = datetime.combine(self.session, RTH_OPEN, tzinfo=ET)
        elapsed = max(0, int((datetime.now(ET) - open_dt).total_seconds()))

        if elapsed > 0:
            async with self._ref.sem:
                bars = await self.ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr=f"{elapsed} S",
                    barSizeSetting=self.bar_size,
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                    timeout=BACKFILL_TIMEOUT,
                )
            for b in bars or []:
                bar_event = self._to_bar_event(symbol, b)
                if bar_event is None:
                    continue
                scanner.backfill_bar(bar_event)
                self._last_bar_start[symbol] = bar_event.start

        await self._subscribe(scanner, contract, symbol, run_id, store)

    def _to_bar_event(self, symbol: str, ib_bar) -> BarEvent | None:
        start = to_et(ib_bar.date)
        if start is None or start.date() != self.session:
            return None
        return BarEvent(
            symbol=symbol,
            start=start,
            end=start + timedelta(seconds=self.bar_seconds),
            open=float(ib_bar.open),
            high=float(ib_bar.high),
            low=float(ib_bar.low),
            close=float(ib_bar.close),
            volume=float(ib_bar.volume),
        )

    async def _subscribe(
        self, scanner: Scanner, contract, symbol: str, run_id: str, store: TriggerStore
    ) -> None:
        # reqHistoricalData (no Async suffix) is a sync wrapper that runs its
        # own loop.run_until_complete() internally -- calling it from code
        # already running on an event loop (every caller here) hits asyncio's
        # "this event loop is already running" guard and crashes the process.
        # reqHistoricalDataAsync is the actual implementation underneath it
        # and supports keepUpToDate identically; awaiting it directly avoids
        # the nested-loop problem entirely.
        live_bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="2 D",
            barSizeSetting=self.bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            keepUpToDate=True,
        )
        self._subs[symbol] = live_bars

        def on_update(bars, has_new_bar) -> None:
            # keepUpToDate delivers every tick of the still-forming last bar;
            # hasNewBar means a new (forming) entry was just appended, which
            # means the previous entry -- bars[-2] -- just completed.
            try:
                if not has_new_bar or len(bars) < 2:
                    return
                bar_event = self._to_bar_event(symbol, bars[-2])
                if bar_event is None:
                    return
                # Skip anything at or before the last bar this symbol already
                # saw (via backfill, or a prior completed-bar callback) -- the
                # subscription's own 2-day lookback overlaps what backfill
                # just fed through, and re-processing it would double-count
                # volume and could re-evaluate a setup on a bar it already saw.
                last = self._last_bar_start.get(symbol)
                if last is not None and bar_event.start <= last:
                    return
                self._last_bar_start[symbol] = bar_event.start
                for trigger in scanner.on_bar(bar_event):
                    store.record(run_id, trigger, datetime.now(ET))
                    # Commit immediately, not just at process exit -- a live
                    # trigger sitting in an uncommitted transaction all day is
                    # invisible to any other reader (DataGrip) and gone entirely
                    # if this process dies before a clean quit. WAL mode (store.py)
                    # is exactly what makes a commit here safe to do this often
                    # without blocking a concurrent reader.
                    store.commit()
                    # Every trigger is still stored regardless of grade --
                    # only the terminal alert is gated, so a look back at
                    # the full session never has gaps to explain.
                    if (trigger.extras.get("auto_grade") or 0) >= NOTIFY_GRADE_THRESHOLD:
                        _print_trigger_alert(trigger)

                track_alert = self.tracker.on_close(symbol, bar_event.close, bar_event.start)
                if track_alert is not None:
                    _print_track_alert(track_alert, self.tracker.trades[symbol])
            except Exception:
                # This runs inside ib_async's event dispatch, off the normal
                # call stack -- an uncaught exception here is easy to lose
                # silently. Log it and keep this symbol's stream alive rather
                # than letting one bad bar take it down (or worse, take down
                # dispatch for other symbols sharing the same event loop).
                logger.exception("Error handling a live bar for %s", symbol)

        live_bars.updateEvent += on_update
