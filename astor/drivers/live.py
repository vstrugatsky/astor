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
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ib_async import IB

from astor.core.scanner import Scanner
from astor.core.store import TriggerStore
from astor.core.types import BarEvent, Trigger
from astor.drivers.replay import ET, ReplayDriver, parse_bar_seconds, to_et

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
    avwap, rvol = t.extras.get("avwap"), t.extras.get("rvol")
    avwap_s = f"{avwap:.2f}" if avwap is not None else "-"
    rvol_s = f"{rvol:.2f}" if rvol is not None else "-"
    return (
        f"[{t.bar_start:%H:%M:%S} ET] TRIGGER {t.symbol:<6} {t.setup:<4} {t.direction:<5} "
        f"@ {t.trigger_price:.2f}  avwap={avwap_s}  rvol={rvol_s}"
    )


def _target_session(now_et: datetime) -> date:
    """The trading day this process is scanning for.

    Before today's close, that's today (covers "started mid-session" and
    every pre-open case). At/after today's close, it's the next weekday --
    the night-before-prep case. This does not know about market holidays: a
    holiday immediately after a live run would roll to a closed day. Low
    blast radius if it happens (a cache-key/label mismatch, not a wrong PDH/
    PDL -- the "last N sessions" lookups already skip closed days on their
    own since IBKR simply has no bars for them), and not worth a market
    calendar for a first pass.
    """
    d = now_et.date() if now_et.time() < time(16, 0) else now_et.date() + timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d += timedelta(days=1)
    return d


class LiveDriver:
    def __init__(self, ib: IB, cache_dir: Path, bar_size: str = "1 min"):
        self.ib = ib
        self.bar_size = bar_size
        self.bar_seconds = parse_bar_seconds(bar_size)
        self.session = _target_session(datetime.now(ET))
        # Composition, not inheritance: fetch_bars/stream/run_replay are
        # replay-specific and don't apply here, but qualify/fetch_levels/
        # fetch_volume_curve/BarCache/the request semaphore all do.
        self._ref = ReplayDriver(ib, self.session, bar_size, cache_dir)

        self._pending: set[str] = set()  # reference data fetched, not yet live
        self._subs: dict[str, object] = {}  # symbol -> BarDataList
        self._last_bar_start: dict[str, datetime] = {}

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
                    print(_format_trigger_line(trigger))
            except Exception:
                # This runs inside ib_async's event dispatch, off the normal
                # call stack -- an uncaught exception here is easy to lose
                # silently. Log it and keep this symbol's stream alive rather
                # than letting one bad bar take it down (or worse, take down
                # dispatch for other symbols sharing the same event loop).
                logger.exception("Error handling a live bar for %s", symbol)

        live_bars.updateEvent += on_update
