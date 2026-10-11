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
import math
from datetime import datetime, time, timedelta
from pathlib import Path

from ib_async import IB, PnLSingle, Position

from astor.core.scanner import Scanner
from astor.core.store import TriggerStore
from astor.core.tracking import (
    SellRuleTracker,
    TrackAlert,
    TrackedTrade,
    TrailAction,
    TrailTracker,
)
from astor.core.types import BarEvent, CapitulationAlert, SetupAlert, TouchAlert, Trigger
from astor.drivers.orders import EOD_ADAPTIVE_PRIORITY, Outcome, TrailExecutor
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

# Notifications bypass auto_grade entirely (2026-09-02, "for now" -- not a
# permanent call) and use a flat time cutoff instead. grade_trigger
# (astor.core.grading) was tuned around U&R/O&F and doesn't fit Reversal:
# it rewards the 9:45-11:00 window, which silenced every real Reversal alert
# after 11:00; it rewards RVOL, which every Reversal trigger already clears
# by construction (RVOL is one of Reversal's own firing filters), so the
# reward is meaningless there; and it rewards price beyond session_open,
# which isn't a meaningful signal for a retracement setup. Revisit once
# grading is redesigned per-setup rather than shared across all of them.
# Reversal is the only setup left (ALL_SETUPS), so the cutoff is the whole
# rule -- the grade-threshold path went with U&R/O&F.
REVERSAL_NOTIFY_CUTOFF = time(15, 0)

# Triggers are recorded but not announced (2026-09-24, "until further
# notice"). The experiment underway is to act on the *alert* -- capitulation
# or AVWAP touch -- by resting a stop order at the price the trigger would
# fire, rather than waiting for the trigger bar to print and chasing it. A
# trigger notification arriving after that order is already working is noise
# at best and a second, worse entry at worst. Everything still lands in the
# store, so nothing is lost for review.
NOTIFY_TRIGGERS = False


def _should_notify(trigger: Trigger) -> bool:
    return NOTIFY_TRIGGERS and trigger.bar_start.time() < REVERSAL_NOTIFY_CUTOFF


def _format_trigger_line(t: Trigger) -> str:
    """Two lines, nothing more (2026-09-14 -- the fuller headline was
    overload mid-session): the leg quality the setup fired on, in exactly
    the shape the earlier CAPITULATION heads-up used so the two read as one
    story about the same leg, then the stop and the size it allows.

    Deliberately dropped: grade/avwap/adr/rvol, and the CDH/CDL
    session-extreme stop framing. They're still on the Trigger (and in the
    store) for after-hours review; this is the in-session view.
    """
    e = t.extras
    level_pct = e.get("reclaim_pct")  # already a clean "how far the good way" magnitude
    level_shares, level_dollars = e.get("level_stop_shares"), e.get("level_stop_dollars")
    toward = "below" if t.direction == "short" else "above"

    def magnitude(v: float | None) -> str:
        return f"{abs(v):.2f}%" if v is not None else "n/a"

    def sized(shares: int | None, dollars: float | None) -> str:
        return f"{shares} sh (${dollars:,.0f})" if shares is not None else "no size (risk too wide)"

    return (
        f"[{t.bar_start:%H:%M:%S} ET] TRIGGER {_symbol(t.symbol)} "
        f"{_setup_token(t.setup, t.direction)} {t.direction:<5} "
        f"@ {t.trigger_price:.2f}"
        + (f"   R:R {_bold(f'{t.extras["rr"]:.1f}')}" if t.extras.get("rr") else "")
        + f"   {_format_metrics(t)}\n"
        f"    {t.level_name:<3} {magnitude(level_pct)} {toward}, stop {t.level_value:.2f}"
        f"  ->  {sized(level_shares, level_dollars)}"
    )


# Terminal bell -- most terminals (Terminal.app, iTerm2) sound it without any
# extra dependency, but it's a per-terminal setting: check yours isn't set to
# silent or visual-only if you don't hear it.
_BELL = "\a"
_ANSI_BOLD = "\033[1m"
_ANSI_RESET = "\033[0m"

# This trader runs a white-background terminal, so every alert prints black
# and only one token per line carries color -- the thing that says what the
# alert *is* (REV-L/REV-S, CAPITULATION, the track event). The bright
# variants (90-97) wash out on white; these are the standard, darker ones.
_ANSI_BLACK = "\033[30m"
_ANSI_GREEN = "\033[32m"
_ANSI_RED = "\033[31m"
_ANSI_BROWN = "\033[38;5;130m"
_ANSI_PURPLE = "\033[35m"


def _bold(text: str) -> str:
    """Bold, still black -- for the few numbers an order is placed from."""
    return f"{_ANSI_BOLD}{text}{_ANSI_RESET}{_ANSI_BLACK}"


def _symbol(sym: str, width: int = 6) -> str:
    """Every alert's ticker, bold. Black like the rest of the line -- the
    color is reserved for what *kind* of alert it is -- but heavy enough to
    be the thing the eye lands on when several alerts scroll past."""
    pad = " " * max(0, width - len(sym))
    return f"{_ANSI_BOLD}{sym}{_ANSI_RESET}{_ANSI_BLACK}{pad}"


def _accent(text: str, color: str, width: int = 0) -> str:
    """Color one token inside an otherwise-black line. Returns to black
    rather than a full reset so the rest of the line keeps its explicit
    color; padding is emitted outside the color span so no trailing blanks
    get painted.
    """
    pad = " " * max(0, width - len(text))
    return f"{color}{_ANSI_BOLD}{text}{_ANSI_RESET}{_ANSI_BLACK}{pad}"


def _setup_token(setup: str, direction: str) -> str:
    """The one colored token on a trigger line: green long (REV-L), red
    short (REV-S). Reversal is the only setup that still fires (see
    ALL_SETUPS), so direction alone decides the color."""
    return _accent(setup, _ANSI_GREEN if direction == "long" else _ANSI_RED, width=4)


def _format_leg_metrics(
    leg_size_adr: float | None,
    move_rvol: float | None,
    ema9_extension_adr: float | None,
    accel_price: float | None,
    accel_vol: float | None,
) -> str:
    """The leg-quality numbers, in one shared format so a CAPITULATION
    heads-up and the TRIGGER that may follow it show the identical set --
    directly comparable line to line. Individual values can still be None
    (an unmeasurable leg renders n/a for that one)."""

    def num(v: float | None) -> str:
        return f"{v:.2f}" if v is not None else "n/a"

    leg = f"{leg_size_adr:.2f}x ADR" if leg_size_adr is not None else "n/a"
    return (
        f"leg {leg}   move_rvol {move_rvol if move_rvol is not None else 'n/a'}"
        f"   ema9_ext {num(ema9_extension_adr)} ADR"
        f"   accel_price {num(accel_price)}   accel_vol {num(accel_vol)}"
    )


def _format_avt_metrics(e: dict) -> str:
    """AVT's own quality numbers -- what the pullback did, not what a leg
    did. Same role the leg metrics play for a Reversal trigger."""

    def num(v: float | None, nd: int = 3) -> str:
        return f"{v:.{nd}f}" if v is not None else "n/a"

    return (
        f"range {num(e.get('day_range_adr'), 2)}x ADR   rvol {num(e.get('rvol'), 2)}"
        f"   touch {num(e.get('touch_adr'))}   depth {num(e.get('depth_adr'))}"
        f"   recovery {num(e.get('recovery_adr'))}"
        f"   +{e.get('bars_since_touch', '?')}b"
    )


def _format_metrics(t: Trigger) -> str:
    """The quality numbers for whichever setup fired. Dispatched on the
    setup's own name rather than sniffing which extras happen to be present:
    a Reversal trigger missing a metric should print n/a for it, not quietly
    render as some other setup's line.
    """
    if t.setup.startswith("AVT-"):
        return _format_avt_metrics(t.extras)
    e = t.extras
    return _format_leg_metrics(
        e.get("leg_size_adr"), e.get("move_rvol"), e.get("ema9_extension_adr"),
        e.get("accel_price"), e.get("accel_vol"),
    )


def _print_alert(text: str) -> None:
    """Bell + explicit black, not just a plain print -- an alert scrolling by
    silently in a wall of other output is exactly what this is for."""
    print(f"{_BELL}{_ANSI_BLACK}{text}{_ANSI_RESET}")


def _print_trigger_alert(t: Trigger) -> None:
    _print_alert(_format_trigger_line(t))


def _format_track_alert(alert: TrackAlert, trade: TrackedTrade) -> str:
    header = (
        f"[{alert.bar_start:%H:%M:%S} ET] {_accent('TRACKING', _ANSI_BROWN)} "
        f"{_symbol(alert.symbol)} {trade.direction:<5}"
    )
    if alert.kind == "breakeven":
        return (
            f"{header} {_accent('+1R reached', _ANSI_BROWN)} @ {alert.close:.2f}"
            f" (target {trade.target_1r:.2f})"
            f"  ->  move stop to breakeven ({trade.entry:.2f})"
        )
    assert trade.peak is not None
    return (
        f"{header} SELL -- {_accent('gave back 0.5R', _ANSI_BROWN)}"
        f" from peak {trade.peak:.2f} to {alert.close:.2f}"
        f"  (entry {trade.entry:.2f}, stop {trade.stop:.2f}, R {trade.r:.2f})"
    )


def _print_track_alert(alert: TrackAlert, trade: TrackedTrade) -> None:
    _print_alert(_format_track_alert(alert, trade))


def _trail_header(symbol: str, when: datetime | None = None) -> str:
    stamp = f"[{when:%H:%M:%S} ET] " if when else f"[{datetime.now(ET):%H:%M:%S} ET] "
    return f"{stamp}{_accent('TRAIL', _ANSI_BROWN)} {_symbol(symbol)}"


def _format_trail_alert(action: TrailAction, trade: TrackedTrade, outcome: Outcome) -> str:
    """What the rule saw, then what was done at IBKR. A failed step says
    so in red: it is the one line the trader has to act on."""
    if action.kind == "breakeven":
        why = f"+1R @ {action.close:.2f} (target {trade.target_1r:.2f})"
    elif action.kind == "eod":
        why = f"end-of-day: last close {action.close:.2f} is {trade.gain_r(action.close):+.2f}R, under 2R"
    else:
        assert trade.peak is not None
        why = (
            f"gave back {action.giveback / trade.r:.2f}R from {trade.peak:.2f} "
            f"to {action.close:.2f} (limit {action.limit / trade.r:.2f}R)"
        )
    done = outcome.text if outcome.ok else _accent(outcome.text, _ANSI_RED)
    return f"{_trail_header(action.symbol, action.bar_start)} {trade.direction:<5} {why}  ->  {done}"


def _print_trail_notice(symbol: str, text: str) -> None:
    _print_alert(f"{_trail_header(symbol)} {text}")


# How much worse a fill has to be before the position shrinks by a whole
# share. Walked in order until one of them costs at least one share, so a
# $3 stock with a 15c stop reports in cents and a $700 one reports in
# dollars, without either being told which it is.
_OVERSHOOT_STEPS = (0.01, 0.02, 0.05, 0.10, 0.25, 0.50, 1.00, 2.50, 5.00)


def _overshoot_cost(
    target: float, stop: float, max_loss: float, direction: str
) -> tuple[float, int] | None:
    """(price step, shares lost) -- the first step size that costs a whole
    share if the fill comes in past the target.

    A resting stop order fills at the market, not at the price it was
    resting at, so the size worked out from the target is only right if the
    fill is. This says how quickly it stops being right: risk per share
    widens as the entry moves away from the stop, so the affordable size
    falls. None when the sizing is meaningless anyway (no risk, no budget).
    """
    def shares_at(entry: float) -> int:
        risk = abs(entry - stop)
        return int(max_loss / risk) if risk > 0 else 0

    base = shares_at(target)
    if base <= 0:
        return None
    worse = 1.0 if direction == "long" else -1.0
    for step in _OVERSHOOT_STEPS:
        lost = base - shares_at(target + worse * step)
        if lost >= 1:
            return step, lost
    return None


def _format_order_alert(label: str, a: SetupAlert, held: bool = False) -> str:
    """One line, sized to be acted on rather than studied.

    Everything that describes *why* the alert fired -- the leg, the touch,
    the volume -- is in the store and deliberately not here (2026-09-24).
    What is here is what a resting Buy/Sell Stop order needs: the price the
    trigger would fire at, the stop that trade carries, the size that pair
    allows, and how fast that size decays if the fill overshoots.
    """
    color = _ANSI_GREEN if a.direction == "long" else _ANSI_RED
    if a.shares is None or a.target is None:
        sized = "no size (risk too wide)"
    else:
        sized = f"sh: {_bold(str(a.shares))} (${a.dollars:,.0f})"
    tail = ""
    if a.target is not None and a.shares:
        cost = _overshoot_cost(a.target, a.stop, a.max_loss_dollars, a.direction)
        if cost is not None:
            step, lost = cost
            beyond = "above" if a.direction == "long" else "below"
            unit = "share" if lost == 1 else "shares"
            tail = f"; {_bold(f'-{lost}')} {unit} / {_bold(f'{step:.2f}')} {beyond} target"
    target = _bold(f"{a.target:.2f}") if a.target is not None else "n/a"
    # A resting stop order only works when the market has not already gone
    # through the level. It happens: the touch can print while price is
    # still beyond the recovery line from an earlier push, so the trigger is
    # held back by the crossing rule rather than by price. Saying so beats
    # sending an order ticket that cannot be placed -- 5 of 38 alerts on
    # 2026-09-21 were this.
    if a.target is not None:
        passed = a.price >= a.target if a.direction == "long" else a.price <= a.target
        if passed:
            target = f"{target} {_bold('(already through)')}"
    # R:R leads, before any of the prices: it is the one number that says
    # whether this setup is worth the attention the rest of the line asks
    # for.
    rr = f"R:R {_bold(f'{a.rr:.1f}')}; " if a.rr is not None else ""
    marker = f"{_accent('(position)', _ANSI_PURPLE)} " if held else ""
    return (
        f"[{a.bar_start:%H:%M:%S} ET] {_accent(label, color)} {_bold(a.symbol)} "
        f"{marker}{a.direction}; {rr}target: {target} "
        f"stop: {_bold(f'{a.stop:.2f}')} {sized}{tail}"
    )


def _print_setup_alert(a: SetupAlert, held: bool = False) -> None:
    """One entry point for every setup-produced alert; the only thing that
    differs by kind is what it is called."""
    _print_alert(
        _format_order_alert(
            "AVWAP TOUCH" if isinstance(a, TouchAlert) else "CAPITULATION", a, held
        )
    )


async def resolve_open_trade(
    ib: IB, symbol: str, account: str | None = None
) -> tuple[float, float, str] | None:
    """Look up entry price and protective stop for an open position in
    `symbol`, across every account this IBKR login can see, or only in
    `account` if given. Returns (entry, stop, account), or None if it can't
    be resolved unambiguously -- no position, more than one position (the
    same symbol held in two accounts: pass `account`), no single matching working stop
    order, or a stop sitting the wrong side of the *market* for the
    position's direction (a stale or unrelated order rather than this
    trade's protective stop).

    That last check used to compare the stop against entry, which rejected
    every winning position: a stop raised above entry -- exactly what the
    TRACK alerts tell this trader to do at +1R -- read as "wrong side" and
    could not be tracked without typing both prices by hand. On 2026-10-04
    all four open positions failed this way, SPCX in at 109.34 with its stop
    trailed to 142.46 among them. Entry says nothing about whether a stop is
    protective; the market does, because a sell stop above the market would
    already have fired.

    Entry comes from `ib.positions()` (kept live-synced by ib_async once
    connected, no extra request needed). The stop comes from a fresh
    `reqAllOpenOrders` -- unlike `ib.openOrders()`, which only sees orders
    placed via this API client, `reqAllOpenOrders` also sees orders entered
    by hand in TWS/Gateway, which is how this trader's stops are placed.
    """
    positions = [
        p
        for p in held_positions(ib, symbol)
        if account is None or p.account == account
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
    mkt = await _position_mark(ib, pos)
    if mkt and ((stop > mkt) if long else (stop < mkt)):
        return None
    return entry, stop, pos.account


def held_positions(ib: IB, symbol: str) -> list[Position]:
    """Every open position in `symbol`, one per account holding it."""
    return [p for p in ib.positions() if p.contract.symbol == symbol and p.position != 0]


async def _position_mark(ib: IB, pos: Position) -> float | None:
    """IB's own mark for `pos` -- the market price its portfolio valuation
    uses -- or None if it doesn't arrive, which skips the side check rather
    than failing closed.

    This used to read `ib.portfolio()`, which is empty on a login with more
    than one account (see `position_pnl`). A ticker snapshot doesn't serve
    either: outside RTH it has no last price.
    """
    pnl = await position_pnl(ib, pos)
    return pnl.value / pos.position if pnl is not None else None


async def position_pnl(ib: IB, pos: Position, timeout: float = 3.0) -> PnLSingle | None:
    """IB's valuation of one position -- its own position count, market
    value, unrealized and daily P&L -- or None if none arrives within
    `timeout`. One-shot: the subscription is cancelled before returning.

    This stands in for the portfolio feed (`ib.portfolio()`), which is empty
    on a login with more than one account: ib_async only subscribes to it
    itself when there is exactly one, and subscribing explicitly never
    resolves and never delivers against this gateway, with or without an
    account (verified 2026-10-10). PnLSingle works per account.
    """
    pnl = ib.reqPnLSingle(pos.account, "", pos.contract.conId)
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not math.isfinite(pnl.value):
            if asyncio.get_running_loop().time() > deadline:
                return None
            await asyncio.sleep(0.1)
        return pnl
    finally:
        ib.cancelPnLSingle(pos.account, "", pos.contract.conId)


def _minute_before(session, close: time) -> time:
    return (datetime.combine(session, close) - timedelta(minutes=1)).time()


class LiveDriver:
    def __init__(
        self, ib: IB, cache_dir: Path, bar_size: str = "1 min", dry_run_trail: bool = False
    ):
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
        self.trailer = TrailTracker()
        self.executor = TrailExecutor(ib, dry_run_trail, _print_trail_notice)
        self._trail_tasks: set[asyncio.Task] = set()

    @property
    def positions(self) -> set[str]:
        """Symbols currently held, read straight from IBKR each time rather
        than cached: positions change through the session, and a cached set
        would start marking alerts for something already sold. ib_async
        keeps this live-synced once connected, so reading it is free.

        Used only to mark alerts -- one on something already held is a
        different read (add, exit, or sit) from one on a fresh name.
        """
        return {p.contract.symbol for p in self.ib.positions() if p.position}

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

    async def _open_trade(
        self,
        scanner: Scanner,
        symbol: str,
        entry: float | None,
        stop: float | None,
        run_id: str,
        store: TriggerStore,
        account: str | None,
        verb: str,
    ) -> tuple[float, float, str | None] | str:
        """Entry, stop and account for `track`/`trail`, with `symbol`
        subscribed for bars -- or a message saying why not."""
        if entry is None and stop is None:
            resolved = await resolve_open_trade(self.ib, symbol, account)
            if resolved is None:
                holders = sorted(p.account for p in held_positions(self.ib, symbol))
                if account is None and len(holders) > 1:
                    return (
                        f"{symbol} is held in more than one account "
                        f"({', '.join(holders)}). Name one: '{verb} {symbol} ACCOUNT'."
                    )
                return (
                    f"Could not resolve a single open position + matching stop "
                    f"order for {symbol} from IBKR. Give both explicitly: "
                    f"'{verb} {symbol} ENTRY STOP'."
                )
            entry, stop, account = resolved

        if symbol not in self._subs and symbol not in self._pending:
            results = await self.add_symbols(scanner, [symbol], run_id, store)
            if results.get(symbol) == "failed":
                return f"Could not subscribe {symbol} for bars; {verb} not started."

        if entry == stop:
            return (
                f"{symbol}: stop equals entry, so R is zero -- the stop is probably "
                f"at breakeven already. Give the original risk: '{verb} {symbol} ENTRY STOP'."
            )
        return entry, stop, account

    async def track(
        self,
        scanner: Scanner,
        symbol: str,
        entry: float | None,
        stop: float | None,
        run_id: str,
        store: TriggerStore,
        account: str | None = None,
    ) -> str:
        """Start sell-rule tracking for a position in `symbol`. `entry` and
        `stop` are either both given (trusted as-is, no IBKR lookup -- the
        only path that works for an account this login can't see) or both
        None, in which case both are resolved from IBKR -- from `account`
        only, if given, which is required when more than one account holds
        `symbol`.

        Ensures `symbol` has a live bar subscription, same as `add_symbols`
        -- tracking needs the same 1-min closes a scanned symbol gets.
        """
        opened = await self._open_trade(scanner, symbol, entry, stop, run_id, store, account, "track")
        if isinstance(opened, str):
            return opened
        entry, stop, account = opened
        self.trailer.untrack(symbol)

        trade = self.tracker.track(symbol, entry, stop, account)
        where = f" [{account}]" if account else ""
        # Styled like the TRACKING alerts this turns on, so the confirmation
        # and the alerts that follow it read as one thing.
        return (
            f"{_ANSI_BLACK}{_accent('TRACKING', _ANSI_BROWN)} {_bold(symbol)} "
            f"{trade.direction}: entry {entry:.2f} stop {stop:.2f} "
            f"R={trade.r:.2f} (1R target {trade.target_1r:.2f}){where}{_ANSI_RESET}"
        )

    async def trail(
        self,
        scanner: Scanner,
        symbol: str,
        entry: float | None,
        stop: float | None,
        run_id: str,
        store: TriggerStore,
        account: str | None = None,
    ) -> str:
        """Hand a position to `trail`: the trail rules (`TrailTracker`) on
        its 1-min closes, acted on at IBKR by `TrailExecutor` -- the stop
        moved to breakeven at +1R, the exit sent on the giveback. Replaces
        any `track` of the same symbol; one rule set per position.

        Same lookup as `track`, but it always needs the account: orders go
        to one. With ENTRY STOP given it comes from whichever single account
        holds `symbol`.
        """
        opened = await self._open_trade(scanner, symbol, entry, stop, run_id, store, account, "trail")
        if isinstance(opened, str):
            return opened
        entry, stop, account = opened
        if account is None:
            holders = sorted(p.account for p in held_positions(self.ib, symbol))
            if len(holders) != 1:
                return (
                    f"{symbol} is held in {len(holders)} accounts ({', '.join(holders) or 'none'}); "
                    f"name one: 'trail {symbol} ACCOUNT' or 'trail {symbol} ENTRY STOP ACCOUNT'."
                )
            account = holders[0]
        # Direction is inferred from stop vs. entry, and orders go the way it
        # says -- so it has to match what is actually held. It won't when the
        # stop has already been raised past entry (looks like a short).
        held = sum(p.position for p in held_positions(self.ib, symbol) if p.account == account)
        if held == 0:
            return f"No {symbol} position in {account}; trail not started."
        if (held > 0) != (stop < entry):
            side = "long" if held > 0 else "short"
            return (
                f"{symbol} is {side} in {account}, but stop {stop:.2f} is past entry "
                f"{entry:.2f} -- it has been moved into profit. Give the original "
                f"risk: 'trail {symbol} ENTRY STOP'."
            )

        self.tracker.untrack(symbol)
        close = await self._session_close(symbol)
        eod_exit = _minute_before(self.session, close) if close else None
        trade = self.trailer.track(symbol, entry, stop, account, eod_exit)
        mode = " (dry run -- no orders)" if self.executor.dry_run else ""
        eod = f"exits at {trade.eod_exit:%H:%M} ET if under 2R"
        if close is None:
            eod += _accent(" (IBKR schedule unavailable -- assuming a 16:00 close)", _ANSI_RED)
        return (
            f"{_ANSI_BLACK}{_accent('TRAIL', _ANSI_BROWN)} {_bold(symbol)} "
            f"{trade.direction}: entry {entry:.2f} stop {stop:.2f} "
            f"R={trade.r:.2f} (1R target {trade.target_1r:.2f}) [{account}]; "
            f"{eod}{mode}{_ANSI_RESET}"
        )

    async def _session_close(self, symbol: str) -> time | None:
        """Today's regular-hours close for `symbol`, from IBKR's own
        schedule (ContractDetails.liquidHours) -- 13:00 on an early-close
        day. None if it can't be read, or the session isn't on it.

        Needs "Expose entire trading schedule to API" in the gateway's API
        settings. The schedule runs about six days ahead (2026-10-11).
        """
        try:
            contract = (await self._ref.qualify([symbol])).get(symbol)
            if contract is None:
                return None
            details = await asyncio.wait_for(self.ib.reqContractDetailsAsync(contract), 10)
            for s in details[0].liquidSessions() if details else []:
                if s.start.astimezone(ET).date() == self.session:
                    return s.end.astimezone(ET).time()
        except Exception:
            logger.exception("Could not read the trading schedule for %s", symbol)
        return None

    def untrack(self, symbol: str) -> bool:
        """Stop `track` and `trail` alike -- the position goes back to the
        trader's hands. Orders already at IBKR stay as they are."""
        return self.tracker.untrack(symbol) | self.trailer.untrack(symbol)

    async def _act_on_trail(self, action: TrailAction, run_id: str, store: TriggerStore) -> None:
        trade = self.trailer.trades.get(action.symbol)
        if trade is None:
            return
        try:
            if action.kind == "breakeven":
                outcome = await self.executor.move_stop_to_breakeven(trade)
            elif action.kind == "eod":
                outcome = await self.executor.exit(trade, EOD_ADAPTIVE_PRIORITY)
            else:
                outcome = await self.executor.exit(trade)
        except Exception as e:  # an exception here would otherwise vanish in a task
            logger.exception("trail %s for %s failed", action.kind, action.symbol)
            outcome = Outcome(False, f"{action.kind} FAILED: {e!r} -- CHECK THE POSITION")
        store.record_alert(
            run_id,
            f"trail_{action.kind}",
            action.symbol,
            trade.direction,
            action.bar_start,
            action.close,
            datetime.now(ET),
            {
                "entry": trade.entry,
                "stop": trade.stop,
                "peak": trade.peak,
                "r": trade.r,
                "giveback": action.giveback,
                "limit": action.limit,
                "account": trade.account,
                "dry_run": self.executor.dry_run,
                "ok": outcome.ok,
                "result": outcome.text,
            }
            | outcome.extras,
        )
        store.commit()
        _print_alert(_format_trail_alert(action, trade, outcome))

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
                    # Every trigger is still stored regardless of grade or
                    # setup -- only the terminal alert is gated, so a look
                    # back at the full session never has gaps to explain.
                    if _should_notify(trigger):
                        _print_trigger_alert(trigger)

                for alert in scanner.pop_alerts():
                    store.record_alert(
                        run_id,
                        alert.kind,
                        alert.symbol,
                        alert.direction,
                        alert.bar_start,
                        alert.price,
                        datetime.now(ET),
                        alert.store_extras(),
                        bar_end=alert.bar_end,
                    )
                    store.commit()
                    _print_setup_alert(alert, alert.symbol in self.positions)

                track_alert = self.tracker.on_close(symbol, bar_event.close, bar_event.start)
                if track_alert is not None:
                    trade = self.tracker.trades[symbol]
                    store.record_alert(
                        run_id,
                        track_alert.kind,
                        symbol,
                        trade.direction,
                        track_alert.bar_start,
                        track_alert.close,
                        datetime.now(ET),
                        {
                            "entry": trade.entry,
                            "stop": trade.stop,
                            "peak": trade.peak,
                            "r": trade.r,
                            "target_1r": trade.target_1r,
                            "account": trade.account,
                        },
                    )
                    store.commit()
                    _print_track_alert(track_alert, trade)

                trail_action = self.trailer.on_close(
                    symbol, bar_event.close, bar_event.start, bar_event.end
                )
                if trail_action is not None:
                    # Orders are awaited, and this callback is not async.
                    # The rule has already moved on (an exit marks the trade
                    # done), so a second close can't send a second order.
                    task = asyncio.create_task(self._act_on_trail(trail_action, run_id, store))
                    self._trail_tasks.add(task)
                    task.add_done_callback(self._trail_tasks.discard)
            except Exception:
                # This runs inside ib_async's event dispatch, off the normal
                # call stack -- an uncaught exception here is easy to lose
                # silently. Log it and keep this symbol's stream alive rather
                # than letting one bad bar take it down (or worse, take down
                # dispatch for other symbols sharing the same event loop).
                logger.exception("Error handling a live bar for %s", symbol)

        live_bars.updateEvent += on_update
