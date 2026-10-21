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
import dataclasses
import logging
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from ib_async import (
    IB,
    Contract,
    ExecutionFilter,
    Fill,
    Order,
    PnLSingle,
    Position,
    Ticker,
    Trade,
)

from astor.core.commissions import shares_for_risk, trade_risk
from astor.core.exit_plan import ExitPlan, allocate
from astor.core.order_entry import (
    ACCOUNT_SUFFIX,
    ADAPTIVE,
    CommandError,
    EnterRequest,
    StopRequest,
    SymbolRequest,
    entry_problems,
    is_ira,
    resolve_account,
    stop_problems,
    tranche_sizes,
)
from astor.core.scanner import Scanner
from astor.core.setups import pace_label
from astor.core.store import TriggerStore
from astor.core.tracking import (
    SellRuleTracker,
    TrackAlert,
    TrackedTrade,
    TrailAction,
    TrailTracker,
    giveback_limit,
)
from astor.core.types import (
    BarEvent,
    CapitulationAlert,
    SetupAlert,
    TouchAlert,
    Trigger,
)
from astor.drivers.orders import (
    EOD_ADAPTIVE_PRIORITY,
    OrderDesk,
    Outcome,
    TrailExecutor,
    describe_order,
    entry_orders,
    exit_order,
    fresh_position,
    protective_stops,
    unbound_problem,
    working_orders,
)
from astor.drivers.replay import (
    ET,
    ReplayDriver,
    next_trading_session,
    parse_bar_seconds,
    to_et,
)
from astor.drivers.shortable import ShortableList

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


def _print_alert(text: str, bell: bool = True) -> None:
    """Bell + explicit black, not just a plain print -- an alert scrolling by
    silently in a wall of other output is exactly what this is for. Only
    setup alerts ever go without the bell (`mute`)."""
    print(f"{_BELL if bell else ''}{_ANSI_BLACK}{text}{_ANSI_RESET}")


def _print_trigger_alert(t: Trigger, bell: bool = True) -> None:
    _print_alert(_format_trigger_line(t), bell)


def _account_tag(account: str | None) -> str:
    """ira/reg rather than the ID: it is there to tell apart two trades of
    one symbol, and those can only be in these two accounts."""
    if account is None:
        return ""
    return f"  [{'ira' if is_ira(account) else 'reg' if account.endswith(ACCOUNT_SUFFIX['reg']) else account}]"


# The avwap rule's yardstick: today's range, capped at 10% of price -- the
# same ADR(1) the AVT setup measures its touch in (setups' max_yard_pct).
AVWAP_YARD_CAP_PCT = 10.0


def _avwap_inputs(ind) -> tuple[float | None, float | None]:
    """(AVWAP, ADR(1) yardstick) for the avwap rule, from a symbol's
    session indicators; None for either that isn't there yet."""
    if ind is None or ind.session_high is None or ind.session_low is None:
        return None, None
    day_range = ind.session_high - ind.session_low
    if day_range <= 0:
        return ind.avwap, None
    return ind.avwap, min(day_range, AVWAP_YARD_CAP_PCT / 100.0 * ind.session_low)


def _leg_part(trade: TrackedTrade, rule: str | None) -> str:
    """'35% (35 sh)' for a rule's share; '' on a one-rule plan."""
    leg = trade.plan.leg(rule) if rule else None  # type: ignore[arg-type]
    if leg is None or leg.pct >= 100:
        return ""
    n = trade.leg_shares.get(leg.rule)
    return f"{leg.pct:g}%" + (f" ({n} sh)" if n is not None else "")


def _plan_text(trade: TrackedTrade) -> str:
    """The plan as the confirmation states it: R coefficient, each rule's
    share with its shares, and the close."""
    plan = trade.plan
    legs = ", ".join(
        f"{g.label()}" + (f" ({trade.leg_shares[g.rule]} sh)" if g.rule in trade.leg_shares else "")
        for g in plan.legs
    )
    parts = ([f"R x{plan.r_mult:g}"] if plan.r_mult != 1.0 else []) + [legs]
    return "; ".join(parts)


def _format_track_alert(alert: TrackAlert, trade: TrackedTrade) -> str:
    header = (
        f"[{alert.bar_start:%H:%M:%S} ET] {_accent('TRACKING', _ANSI_BROWN)} "
        f"{_symbol(alert.symbol)} {trade.direction:<5}"
    )
    if alert.kind == "breakeven":
        return (
            f"{header} {_accent('+1R reached', _ANSI_BROWN)} @ {alert.close:.2f}"
            f" (target {trade.target_1r:.2f})"
            f"  ->  move stop to breakeven ({trade.entry:.2f}){_account_tag(trade.account)}"
        )
    # A short is covered, not sold.
    verb = "SELL" if trade.direction == "long" else "COVER"
    if alert.kind == "eod":
        hold = trade.plan.close_hold_r
        return (
            f"{header} {verb} what's left -- {_accent('the close', _ANSI_BROWN)}"
            f" @ {alert.close:.2f} ({trade.gain_r(alert.close):+.2f}R"
            + (f", under {hold:g}R" if hold is not None else "")
            + f"){_account_tag(trade.account)}"
        )
    part = _leg_part(trade, alert.rule) if not alert.whole else ""
    what = f"{verb} {part}" if part else (
        f"{verb} the rest" if alert.whole and not trade.plan.is_default else verb
    )
    if alert.rule == "avwap":
        why = f"{_accent('lost AVWAP', _ANSI_BROWN)}: close {alert.close:.2f} is {alert.giveback:.2f} past it (limit {alert.limit:.2f})"
    else:
        assert trade.peak is not None
        why = (
            f"{_accent(f'gave back {alert.giveback / trade.r:.2f}R', _ANSI_BROWN)}"
            f" from {trade.peak:.2f} to {alert.close:.2f} (limit {alert.limit / trade.r:.2f}R)"
        )
    return (
        f"{header} {what} -- {why}"
        f"  (entry {trade.entry:.2f}, stop {trade.stop:.2f}, R {trade.r:.2f}){_account_tag(trade.account)}"
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
        hold = trade.plan.close_hold_r
        why = f"end-of-day: last close {action.close:.2f} is {trade.gain_r(action.close):+.2f}R" + (
            f", under {hold:g}R" if hold is not None else ""
        )
    elif action.rule == "avwap":
        why = (
            f"lost AVWAP: close {action.close:.2f} is {action.giveback:.2f} past it "
            f"(limit {action.limit:.2f})"
        )
    else:
        assert trade.peak is not None
        why = (
            f"gave back {action.giveback / trade.r:.2f}R from {trade.peak:.2f} "
            f"to {action.close:.2f} (limit {action.limit / trade.r:.2f}R)"
        )
    if action.kind == "exit" and not action.whole and (part := _leg_part(trade, action.rule)):
        why = f"{part}: {why}"
    elif action.kind == "exit" and not trade.plan.is_default:
        why = f"the rest: {why}"
    done = outcome.text if outcome.ok else _accent(outcome.text, _ANSI_RED)
    return (
        f"{_trail_header(action.symbol, action.bar_start)} {trade.direction:<5} {why}  ->  {done}"
        f"{_account_tag(trade.account)}"
    )


def _trail_start_state(trade: TrackedTrade, closes: list[float]) -> str:
    """Where a just-started trail stands: what it counts the giveback
    from, and whether the next close exits already."""
    if trade.plan.leg("gb") is None:
        return "no giveback rule in this plan"
    if trade.opened_at is None:
        return "entry time unknown (no fill today that opened it) -- giveback counts from the next close"
    if not closes:
        return f"opened {trade.opened_at:%H:%M} ET -- giveback counts from entry"
    assert trade.peak is not None
    last = closes[-1]
    given, limit = abs(trade.peak - last) / trade.r, giveback_limit(trade) / trade.r
    state = (
        f"best close since {trade.opened_at:%H:%M} ET {trade.peak:.2f} "
        f"({trade.gain_r(trade.peak):+.2f}R); last close {last:.2f} gave back "
        f"{given:.2f}R of {limit:.2f}R allowed"
    )
    if round(given, 6) >= round(limit, 6):
        state += " -- " + _accent("exits on the next close unless it recovers", _ANSI_RED)
    return state


def _eod_text(trade: TrackedTrade, close: time | None, verb: str) -> str:
    """What happens at the close, as the track/trail confirmation says it."""
    hold = trade.plan.close_hold_r
    text = (
        f"{verb} what's left at {trade.eod_exit:%H:%M} ET" if hold is None
        else f"{verb} at {trade.eod_exit:%H:%M} ET if under {hold:g}R"
    )
    if close is None:
        text += _accent(" (IBKR schedule unavailable -- assuming a 16:00 close)", _ANSI_RED)
    return text


def _start_lines(trade: TrackedTrade, closes: list[float]) -> str:
    """The lines under a track/trail confirmation: where the giveback
    stands, and whether avwap is armed."""
    lines = f"\n  {_trail_start_state(trade, closes)}"
    if trade.open_leg("avwap") is not None:
        lines += f"\n  avwap: {'armed' if trade.avwap_armed else 'waits for a close on the trade side of AVWAP'}"
    return lines


def _print_trail_notice(symbol: str, text: str) -> None:
    _print_alert(f"{_trail_header(symbol)} {text}")


def _order_line(symbol: str, outcome: Outcome) -> str:
    stamp = f"[{datetime.now(ET):%H:%M:%S} ET]"
    done = outcome.text if outcome.ok else _accent(outcome.text, _ANSI_RED)
    return f"{stamp} {_accent('ORDER', _ANSI_BROWN)} {_symbol(symbol)} {done}"


def _print_order_notice(symbol: str, text: str) -> None:
    _print_alert(_order_line(symbol, Outcome(True, text)))


def _r_text(entry: float, r: float) -> str:
    """'R=0.25 (0.8%)': R in dollars a share, and as a percent of entry."""
    return f"R={r:.2f} ({r / entry * 100:.1f}%)"


def _print_fill_notice(symbol: str, shares: float, price: float, then: str) -> None:
    """A fill, entry or exit: shares signed by side (negative sold), then
    what it was or what astor did with it."""
    _print_alert(
        f"[{datetime.now(ET):%H:%M:%S} ET] {_accent('ORDER FILLED', _ANSI_BROWN)} {_symbol(symbol)} "
        f"{_bold(f'{shares:g}')} @ {_bold(f'{price:.4f}')}; {then}"
    )


@dataclass
class EntryTicket:
    """An `enter` checked and built, waiting on the trader's go-ahead."""

    req: EnterRequest
    account: str
    contract: Contract
    parent: Order
    child: Order | None
    summary: str


# How much worse a fill has to be before the position shrinks by a whole
# share. Walked in order until one of them costs at least one share, so a
# $3 stock with a 15c stop reports in cents and a $700 one reports in
# dollars, without either being told which it is.
_OVERSHOOT_STEPS = (0.01, 0.02, 0.05, 0.10, 0.25, 0.50, 1.00, 2.50, 5.00)


def _overshoot_cost(
    target: float, stop: float, max_loss: float, direction: str, slip: float = 0.0
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
        return shares_for_risk(entry, stop, max_loss, slip)

    base = shares_at(target)
    if base <= 0:
        return None
    worse = 1.0 if direction == "long" else -1.0
    for step in _OVERSHOOT_STEPS:
        lost = base - shares_at(target + worse * step)
        if lost >= 1:
            return step, lost
    return None


def _format_order_alert(
    label: str, a: SetupAlert, held: bool = False, slip: float | None = None,
    borrow: str | None = None,
) -> str:
    """One line, sized to be acted on rather than studied.

    Everything that describes *why* the alert fired -- the leg, the touch,
    the volume -- is in the store and deliberately not here (2026-09-24).
    What is here is what a resting Buy/Sell Stop order needs: the price the
    trigger would fire at, the stop that trade carries, the size that pair
    allows, and how fast that size decays if the fill overshoots.
    """
    color = _ANSI_GREEN if a.direction == "long" else _ANSI_RED
    # `slip` is half the live spread, paid at the stop (astor.core.
    # commissions); the setup sized from bars alone, which have no quote.
    # None: no quote came, so the setup's own size stands, and says so.
    shares, dollars = a.shares, a.dollars
    if slip is not None and a.target is not None:
        shares = shares_for_risk(a.target, a.stop, a.max_loss_dollars, slip) or None
        dollars = shares * a.target if shares else None
    if shares is None or a.target is None:
        sized = "no size (risk too wide)"
    else:
        sized = f"sh: {_bold(str(shares))} (${dollars:,.0f})"
        if slip is None:
            sized += " (no quote, no slippage)"
    tail = ""
    if a.target is not None and shares:
        cost = _overshoot_cost(a.target, a.stop, a.max_loss_dollars, a.direction, slip or 0.0)
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
    # One number leads, before any of the prices: the one that says
    # whether this setup is worth the attention the rest of the line asks
    # for. For AVWAP TOUCH that is R:R. Not for CAPITULATION, where it is
    # 9.0 by construction: entry 10% off the leg's extreme, stop at the
    # extreme, exit back at the session's other end -- where the leg
    # started -- so 90% of the leg over 10% of it. What does vary is how
    # violent the leg was: its pace, stored as a multiple of an average
    # day's (one ADR over the session's 390 minutes) -- "13x"
    # is a leg built thirteen times faster than the stock's usual day.
    if isinstance(a, CapitulationAlert):
        rr = f"pace {_bold(pace_label(a.pace_adr))}; " if a.pace_adr is not None else ""
    else:
        rr = f"R:R {_bold(f'{a.rr:.1f}')}; " if a.rr is not None else ""
    marker = f"{_accent('(position)', _ANSI_PURPLE)} " if held else ""
    # A short IBKR can't lend for would be rejected: say so up front.
    if borrow:
        marker += f"{_accent(f'({borrow})', _ANSI_PURPLE)} "
    return (
        f"[{a.bar_start:%H:%M:%S} ET] {_accent(label, color)} {_bold(a.symbol)} "
        f"{marker}{a.direction}; {rr}target: {target} "
        f"stop: {_bold(f'{a.stop:.2f}')} {sized}{tail}"
    )


def _print_setup_alert(
    a: SetupAlert, held: bool = False, bell: bool = True, slip: float | None = None,
    borrow: str | None = None,
) -> None:
    """One entry point for every setup-produced alert; the only thing that
    differs by kind is what it is called."""
    _print_alert(
        _format_order_alert(
            "AVWAP TOUCH" if isinstance(a, TouchAlert) else "CAPITULATION", a, held, slip, borrow
        ),
        bell,
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


def opening_fill_time(held: float, fills: list[Fill]) -> datetime | None:
    """The time of the fill that opened a position now `held` shares
    (negative short), walking back from the latest fill until the size
    reaches zero -- or crosses it, a single fill that flipped the position.
    None if `fills` never get it there."""
    remaining = held
    for f in sorted(fills, key=lambda f: f.time, reverse=True):
        remaining -= f.execution.shares if f.execution.side == "BOT" else -f.execution.shares
        if round(remaining, 6) == 0 or (remaining > 0) != (held > 0):
            return f.time.astimezone(ET)
    return None


def _price_of(t: Ticker | None) -> float | None:
    """Last if it's inside the quote, else the midpoint; None if neither."""
    for p in (t.marketPrice(), t.last, t.midpoint()) if t is not None else ():
        if p is not None and math.isfinite(p) and p > 0:
            return p
    return None


def _half_spread_of(t: Ticker | None) -> float | None:
    """Half the bid/ask spread, once both sides are in."""
    if t is None:
        return None
    bid, ask = t.bid, t.ask
    if bid is not None and ask is not None and math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask:
        return (ask - bid) / 2
    return None


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
        self.executor = TrailExecutor(
            ib, dry_run_trail, _print_trail_notice,
            lambda sym, shares, price: _print_fill_notice(sym, shares, price, "trail exit"),
        )
        # The console's enter/stop/cancel/exit. Same dry-run switch as trail:
        # a session that shouldn't place trail's orders shouldn't place these.
        self.desk = OrderDesk(
            ib, dry_run_trail, _print_order_notice,
            lambda sym, shares, price: _print_fill_notice(sym, shares, price, "exit"),
        )
        self._entry_tasks: set[asyncio.Task] = set()
        self._trail_tasks: set[asyncio.Task] = set()
        self._alert_tasks: set[asyncio.Task] = set()
        self._spreads: dict[str, asyncio.Task] = {}  # symbol -> quote in flight
        # What IBKR can lend, for shorts -- see start_background
        self.shortable = ShortableList()
        self._background: set[asyncio.Task] = set()
        # `mute`: setup alerts print without the bell. track, trail and
        # order notices always ring -- they are about money already in.
        self.muted = False
        ib.positionEvent += self._on_position

    def start_background(self) -> None:
        """Work the session keeps doing on its own: for now, refreshing the
        shortable list every 15 minutes. Needs the running event loop."""
        task = asyncio.create_task(self.shortable.run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _borrow_problem(self, symbol: str, qty: int | None = None) -> str | None:
        """Why a short in `symbol` would be refused for lack of shares to
        borrow, if the list says so; None when it doesn't (or isn't read)."""
        n = self.shortable.available(symbol)
        if n is None or n >= (qty or 1):
            return None
        as_of = f" (IBKR list {self.shortable.as_of:%H:%M} ET)" if self.shortable.as_of else ""
        return (f"only {n:,} to borrow" if n else "not shortable -- none to borrow") + as_of

    def _on_position(self, pos: Position) -> None:
        """A position gone flat ends its trade -- stopped out, sold in TWS,
        or exited by trail. Without this the trade lingers: `track` keeps
        alerting on it, and a re-entry reads as an add to it (CCXI
        2026-10-14, stopped out and re-entered)."""
        if pos.position != 0:
            return
        try:
            sym = pos.contract.symbol
            # A trade with no account stands for whichever account holds
            # the symbol, so it ends only when none does.
            accounts = [pos.account] + ([None] if not held_positions(self.ib, sym) else [])
            gone = [
                t for book in (self.tracker, self.trailer) for a in accounts
                if (t := book.trades.pop((sym, a), None)) is not None
            ]
            # One trail or `exit` already closed is quiet; anything else is news.
            if any(t.state != "done" for t in gone):
                _print_order_notice(
                    sym, f"position closed {_account_tag(pos.account).strip()} -- no longer tracked/trailed"
                )
        except Exception:
            # ib_async event dispatch: log rather than lose it
            logger.exception("handling a position update failed")

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

    def resolve_account(self, token: str | None) -> str | None:
        """The account an ira/reg/ID token names; None for no token."""
        return resolve_account(token, self.ib.managedAccounts())

    def _known_trade(self, symbol: str, account: str | None) -> TrackedTrade | None:
        """The trade astor already has on `symbol` in `account` -- trailed
        or tracked, the latter including every fill of an `enter` -- or the
        single one on `symbol` when `account` is None. Its entry and stop
        are the original ones: neither rule set ever changes them."""
        for book in (self.trailer, self.tracker):
            trades = [
                t for t in book.for_symbol(symbol)
                if account is None or t.account in (account, None)
            ]
            if len(trades) == 1:
                return trades[0]
        return None

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
    ) -> tuple[float, float, str | None, TrackedTrade | None] | str:
        """Entry, stop and account for `track`/`trail`, with `symbol`
        subscribed for bars -- or a message saying why not. Also the trade
        astor already had on the position, if any.

        Without ENTRY STOP, a position astor already tracks keeps its own
        entry and original stop; anything else is looked up at IBKR, which
        only works while the one stop there is still the original.
        """
        try:
            account = self.resolve_account(account)
        except CommandError as e:
            return str(e)
        holders = sorted(p.account for p in held_positions(self.ib, symbol))
        if account is None and len(holders) > 1:
            return (
                f"{symbol} is held in more than one account ({', '.join(holders)}). "
                f"Name one: '{verb} {symbol} [ENTRY STOP] ira|reg'."
            )
        if account is None and holders:
            account = holders[0]

        known = self._known_trade(symbol, account)
        if entry is None and stop is None:
            if known is not None:
                entry, stop = known.entry, known.stop
                account = account or known.account
            else:
                resolved = await resolve_open_trade(self.ib, symbol, account)
                if resolved is None:
                    return (
                        f"astor isn't tracking {symbol}, and IBKR doesn't show a single "
                        f"working stop for it to take R from (none, or split into "
                        f"tranches). Give entry and the original stop: "
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
        return entry, stop, account, known

    async def track(
        self,
        scanner: Scanner,
        symbol: str,
        entry: float | None,
        stop: float | None,
        run_id: str,
        store: TriggerStore,
        account: str | None = None,
        opened_at: datetime | None = None,
        plan: ExitPlan | None = None,
    ) -> str:
        """Start sell-rule tracking for a position in `symbol`. `entry` and
        `stop` are either both given (trusted as-is, no IBKR lookup -- the
        only path that works for an account this login can't see) or both
        None, in which case both come from the trade astor already has on
        it, or else from IBKR -- from `account` only, if given, which is
        required when more than one account holds `symbol`.

        Ensures `symbol` has a live bar subscription, same as `add_symbols`
        -- tracking needs the same 1-min closes a scanned symbol gets.
        """
        opened = await self._open_trade(scanner, symbol, entry, stop, run_id, store, account, "track")
        if isinstance(opened, str):
            return opened
        entry, stop, account, known = opened
        if opened_at is None and known is not None:
            opened_at = known.opened_at
        self.trailer.untrack(symbol, account)

        # trail's rules, so trail's start: the best close since entry, the
        # avwap rule armed if a close since was on the trade's side, the
        # cutoff from the symbol's own close.
        plan = plan or ExitPlan()
        if opened_at is None and account is not None:
            opened_at = await self._opened_at(symbol, account)
        closes = self._closes_since(symbol, opened_at)
        best = (max if stop < entry else min)(closes) if closes else None
        close = await self._session_close(symbol)
        eod_exit = _minute_before(self.session, close) if close else None
        held = int(abs(self._held(symbol, account))) if account else 0
        trade = self.tracker.track(
            symbol, entry, stop, account, opened_at, plan, eod_exit, best,
            allocate(plan.legs, held) if held else None,
        )
        self._seed_avwap(scanner, trade, closes)
        where = f" [{account}]" if account else ""
        how = "" if plan.is_default else f"; {_plan_text(trade)}"
        # Styled like the TRACKING alerts this turns on, so the confirmation
        # and the alerts that follow it read as one thing.
        return (
            f"{_ANSI_BLACK}{_accent('TRACKING', _ANSI_BROWN)} {_bold(symbol)} "
            f"{trade.direction}: entry {entry:.2f} stop {stop:.2f} "
            f"{_r_text(entry, trade.r)} (1R target {trade.target_1r:.2f}){where}{how}; "
            f"{_eod_text(trade, close, 'says to sell' if trade.direction == 'long' else 'says to cover')}{_ANSI_RESET}"
            f"{_start_lines(trade, closes)}"
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
        plan: ExitPlan | None = None,
    ) -> str:
        """Hand a position to `trail`: the trail rules (`TrailTracker`) on
        its 1-min closes, acted on at IBKR by `TrailExecutor` -- the stop
        moved to breakeven at +1R, the exit sent on the giveback. Replaces
        any `track` of the same position; one rule set per position.

        Same lookup as `track`, but it always needs the account: orders go
        to one. With ENTRY STOP given it comes from whichever single account
        holds `symbol`.

        A trade already under way starts from its best close since entry,
        not from the close after this command: a giveback that happened
        before trail was started counts, and exits on the next close.
        """
        opened = await self._open_trade(scanner, symbol, entry, stop, run_id, store, account, "trail")
        if isinstance(opened, str):
            return opened
        entry, stop, account, known = opened
        if account is None:
            return f"No {symbol} position in any account; trail not started."
        # Direction is inferred from stop vs. entry, and orders go the way it
        # says -- so it has to match what is actually held. It won't when the
        # stop has already been raised past entry (looks like a short).
        held = self._held(symbol, account)
        if held == 0:
            return f"No {symbol} position in {account}; trail not started."
        if (held > 0) != (stop < entry):
            side = "long" if held > 0 else "short"
            return (
                f"{symbol} is {side} in {account}, but stop {stop:.2f} is past entry "
                f"{entry:.2f} -- it has been moved into profit. Give the original "
                f"risk: 'trail {symbol} ENTRY STOP'."
            )

        opened_at = known.opened_at if known is not None else None
        opened_at = opened_at or await self._opened_at(symbol, account)
        closes = self._closes_since(symbol, opened_at)
        best = (max if stop < entry else min)(closes) if closes else None

        self.tracker.untrack(symbol, account)
        close = await self._session_close(symbol)
        eod_exit = _minute_before(self.session, close) if close else None
        plan = plan or ExitPlan()
        trade = self.trailer.track(
            symbol, entry, stop, account, eod_exit, opened_at, best,
            plan, allocate(plan.legs, int(abs(held))),
        )
        self._seed_avwap(scanner, trade, closes)
        mode = " (dry run -- no orders)" if self.executor.dry_run else ""
        return (
            f"{_ANSI_BLACK}{_accent('TRAIL', _ANSI_BROWN)} {_bold(symbol)} "
            f"{trade.direction}: entry {entry:.2f} stop {stop:.2f} "
            f"{_r_text(entry, trade.r)} (1R target {trade.target_1r:.2f}) [{account}]; "
            + ("" if plan.is_default else f"{_plan_text(trade)}; ")
            + f"{_eod_text(trade, close, 'exits')}{mode}{_ANSI_RESET}"
            + _start_lines(trade, closes)
        )

    def _seed_avwap(self, scanner: Scanner, trade: TrackedTrade, closes: list[float]) -> None:
        """avwap: armed already if a close since entry was on the trade's
        side of AVWAP -- today's AVWAP, the one the rule will watch."""
        avwap, _ = _avwap_inputs(scanner.indicators.get(trade.symbol))
        if avwap is not None and closes:
            long = trade.direction == "long"
            trade.avwap_armed = any(c > avwap if long else c < avwap for c in closes)

    async def _opened_at(self, symbol: str, account: str) -> datetime | None:
        """When today's fills opened the position now held in `account`:
        walking back from its current size to the fill that took it off
        zero. None when today's fills don't account for all of it -- held
        from an earlier day, or filled where this login can't see."""
        held = self._held(symbol, account)
        try:
            fills = await asyncio.wait_for(
                self.ib.reqExecutionsAsync(ExecutionFilter(acctCode=account, symbol=symbol)), 10
            )
        except Exception:
            logger.exception("Could not read today's %s fills", symbol)
            return None
        return opening_fill_time(
            held,
            [f for f in fills if f.contract.symbol == symbol and f.execution.acctNumber == account],
        )

    def _closes_since(self, symbol: str, since: datetime | None) -> list[float]:
        """Today's completed 1-min closes ending after `since`, from the
        bar subscription's own history. Empty when `since` is unknown."""
        bars = self._subs.get(symbol)
        if since is None or not bars:
            return []
        closes = []
        for b in list(bars)[:-1]:  # the last bar is still forming
            ev = self._to_bar_event(symbol, b)
            if ev is not None and ev.end > since:
                closes.append(ev.close)
        return closes

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

    def untrack(self, symbol: str, account: str | None = None) -> list[TrackedTrade]:
        """Stop `track` and `trail` alike, in `account` or in every account
        -- the position goes back to the trader's hands. Orders already at
        IBKR stay as they are. Returns the trades dropped."""
        return self.tracker.untrack(symbol, account) + self.trailer.untrack(symbol, account)

    def untrail(self, symbol: str, account: str | None = None) -> list[TrackedTrade]:
        """Stop `trail` only, in `account` or in every account: no more
        orders from it. A `track` is left alone, and so are the orders
        already at IBKR. Returns the trades dropped."""
        return self.trailer.untrack(symbol, account)

    async def _act_on_trail_in_order(
        self, actions: list[TrailAction], run_id: str, store: TriggerStore
    ) -> None:
        """One close's actions, one after another: a partial sale and a
        breakeven move both change the stops, and must not overlap."""
        for action in actions:
            await self._act_on_trail(action, run_id, store)

    async def _act_on_trail(self, action: TrailAction, run_id: str, store: TriggerStore) -> None:
        trade = self.trailer.trades.get((action.symbol, action.account))
        if trade is None:
            return
        try:
            if action.kind == "breakeven":
                outcome = await self.executor.move_stop_to_breakeven(trade)
            elif action.kind == "eod":
                outcome = await self.executor.exit(trade, EOD_ADAPTIVE_PRIORITY)
            elif action.whole or action.rule is None:
                outcome = await self.executor.exit(trade)
            else:
                outcome = await self.executor.exit_shares(trade, trade.leg_shares.get(action.rule, 0))
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
                "rule": action.rule,
                "whole": action.whole,
                "plan": trade.plan.describe(),
                "account": trade.account,
                "dry_run": self.executor.dry_run,
                "ok": outcome.ok,
                "result": outcome.text,
            }
            | outcome.extras,
        )
        store.commit()
        _print_alert(_format_trail_alert(action, trade, outcome))

    # -- order commands: enter / stop / cancel / exit -----------------------

    async def _contract(self, symbol: str) -> Contract | None:
        return (await self._ref.qualify([symbol])).get(symbol)

    async def _snapshot(self, contract: Contract) -> Ticker | None:
        try:
            [t] = await asyncio.wait_for(self.ib.reqTickersAsync(contract), 5)
            return t
        except Exception:
            logger.exception("No market snapshot for %s", contract.symbol)
            return None

    async def _quote(self, contract: Contract, timeout: float = 2.0) -> Ticker | None:
        """Bid and ask outside regular hours, where a full snapshot can
        time out: a snapshot request read as soon as both sides are in,
        then a streaming one (the overnight session streams; regular
        hours, and likely pre-market, are snapshot-only on this login --
        see _half_spread). The ticker, or None if neither brings a quote."""
        loop = asyncio.get_running_loop()

        async def both_sides(ticker: Ticker) -> bool:
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                if _half_spread_of(ticker) is not None:
                    return True
                await asyncio.sleep(0.05)
            return False

        try:
            ticker = self.ib.reqMktData(contract, "", True, False)
            if await both_sides(ticker):
                return ticker
            ticker = self.ib.reqMktData(contract)
            try:
                return ticker if await both_sides(ticker) else None
            finally:
                self.ib.cancelMktData(contract)
        except Exception:
            logger.exception("No quote for %s", contract.symbol)
            return None

    async def _market_price(self, contract: Contract) -> float | None:
        """The current price for the side checks: last if it's inside the
        quote, else the midpoint. None when neither arrives -- the checks
        that need it are then skipped rather than blocking the order."""
        return _price_of(await self._snapshot(contract))

    async def _half_spread(self, contract: Contract, timeout: float = 1.5) -> float | None:
        """Half the bid/ask spread right now, from a snapshot request --
        read as soon as bid and ask are in, not when the snapshot ends
        (it waits on every tick type). Not streaming: the API's US stock
        quotes in regular hours are snapshot-only on this login, and a
        streaming request is refused with error 354 (2026-10-15). None if
        no two-sided quote arrives in `timeout`. Two alerts on one symbol
        share the one request."""
        sym = contract.symbol
        if sym not in self._spreads:
            task = asyncio.create_task(self._snapshot_half_spread(contract, timeout))
            self._spreads[sym] = task
            task.add_done_callback(lambda _: self._spreads.pop(sym, None))
        return await asyncio.shield(self._spreads[sym])

    async def _snapshot_half_spread(self, contract: Contract, timeout: float) -> float | None:
        # A snapshot ends itself; cancelling one that already has is an
        # error at IBKR, so it is never cancelled here.
        ticker = self.ib.reqMktData(contract, "", True, False)
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                if (half := _half_spread_of(ticker)) is not None:
                    return half
                await asyncio.sleep(0.05)
            return None
        except Exception:
            logger.exception("No quote for %s", contract.symbol)
            return None

    async def _print_setup_alert_sized(self, alert: SetupAlert, held: bool, bell: bool) -> None:
        """The alert, its size cut for slippage at the stop. Waits at most
        1.5s for the quote; the alert prints either way."""
        slip = None
        try:
            bars = self._subs.get(alert.symbol)
            contract = getattr(bars, "contract", None)
            if contract is not None:
                slip = await self._half_spread(contract)
        finally:
            borrow = self._borrow_problem(alert.symbol) if alert.direction == "short" else None
            _print_setup_alert(alert, held, bell, slip, borrow)

    async def _in_rth(self, symbol: str) -> bool:
        now = datetime.now(ET)
        if now.date() != self.session:
            return False
        close = await self._session_close(symbol) or time(16, 0)
        return RTH_OPEN <= now.time() < close

    def _held(self, symbol: str, account: str) -> float:
        return sum(p.position for p in held_positions(self.ib, symbol) if p.account == account)

    def _account_for(self, token: str | None, symbol: str, open_trades: list[Trade]) -> str:
        """The account a stop/cancel/exit is about: the one named, else the
        one account holding `symbol` or with orders working in it."""
        managed = self.ib.managedAccounts()
        if token is not None:
            return resolve_account(token, managed)
        involved = {p.account for p in held_positions(self.ib, symbol)} | {
            a for a in managed if working_orders(open_trades, symbol, a)
        }
        if len(involved) != 1:
            raise CommandError(
                f"{symbol}: nothing held or working in any account" if not involved
                else f"{symbol} is in {', '.join(sorted(involved))} -- name one (ira/reg/ID)"
            )
        return involved.pop()

    def _unbound(self, trades: list[Trade]) -> str | None:
        return None if self.desk.dry_run else unbound_problem(trades)

    def _record_order(
        self, store: TriggerStore, run_id: str, kind: str, symbol: str,
        direction: str, price: float, outcome: Outcome, extras: dict,
    ) -> None:
        now = datetime.now(ET)
        store.record_alert(
            run_id, kind, symbol, direction, now, price, now,
            extras | {"dry_run": self.desk.dry_run, "ok": outcome.ok, "result": outcome.text}
            | outcome.extras,
        )
        store.commit()

    async def prepare_entry(self, req: EnterRequest) -> EntryTicket | str:
        """Check an `enter` and build its orders, without sending them.
        Returns the ticket to confirm, or why it can't be sent."""
        managed = self.ib.managedAccounts()
        try:
            account = resolve_account(
                req.account or ("ira" if req.direction == "long" else "reg"), managed
            )
        except CommandError as e:
            return str(e)
        if req.direction == "short" and is_ira(account):
            return f"{account} is the IRA -- no shorts there. Use 'reg'."
        contract = await self._contract(req.symbol)
        if contract is None:
            return f"IBKR doesn't know {req.symbol}."
        in_rth = await self._in_rth(req.symbol)
        if req.order_type in ADAPTIVE and not in_rth and not req.type_defaulted:
            return "Adaptive is regular-hours only; outside them use lmt (works now) or stp (from the open)."
        quote = await (self._snapshot(contract) if in_rth else self._quote(contract))
        market, half = _price_of(quote), _half_spread_of(quote)
        notes = []
        # A stop entry the market is already through would trigger at once
        # and fill at the market anyway: send it as the market order it is
        # -- Adaptive, or outside regular hours the limit below -- and say
        # so, rather than refuse it and make the trader retype.
        if req.order_type == "STP" and market is not None and req.price is not None:
            long = req.direction == "long"
            if (market >= req.price) if long else (market <= req.price):
                side = "buy" if long else "sell"
                notes.append(_bold(
                    f"market {market:.2f} is already through the {side} stop {req.price:.2f}"
                    f" -- sent as {'Adaptive' if in_rth else 'a limit'} instead"
                ))
                req = dataclasses.replace(req, order_type="ADN", price=None, type_defaulted=True)
        # No type typed, outside regular hours: a limit at the far side of
        # the quote -- the price that fills now, without chasing it.
        if req.type_defaulted and not in_rth:
            long = req.direction == "long"
            far = (quote.ask if long else quote.bid) if quote is not None else None
            if far is None or not math.isfinite(far) or far <= 0:
                return f"No {'ask' if long else 'bid'} for {req.symbol} -- give the price: '... lmt PRICE'."
            req = dataclasses.replace(req, order_type="LMT", price=round(far, 4))
            notes.append(f"limit at the {'ask' if long else 'bid'}")
        held = self._held(req.symbol, account)
        problems = entry_problems(req, market, held)
        if problems:
            return "NOT sent: " + "; ".join(problems)

        parent, child = entry_orders(req, account)
        if market is None:
            notes.append("no market price -- side checks skipped")
        if half is None:
            notes.append("no quote -- risk without slippage")
        if held:
            notes.append(f"adds to {abs(held):g} held")
        # Not refused here: the list is up to 15 minutes old, and IBKR
        # has the last word -- it rejects the short if it still can't lend.
        if req.direction == "short" and (borrow := self._borrow_problem(req.symbol, req.qty)):
            notes.append(_bold(borrow))
        # 'sell' reads as closing a long, but it opens a short -- say so
        # whenever the other side is held, in either account.
        sign = 1 if req.direction == "long" else -1
        for p in held_positions(self.ib, req.symbol):
            if p.position * sign < 0:
                side = "long" if p.position > 0 else "short"
                notes.append(_bold(
                    f"you are {side} {abs(p.position):g} in {_account_tag(p.account).strip(' []')}"
                    f" -- this opens a {req.direction}; 'exit {req.symbol}' closes"
                ))
        if not in_rth:
            notes.append({
                "MKT": "queued for the open",
                "LMT": "works now, pre/after-market; the stop from 9:30",
            }.get(req.order_type, "works from the open"))
        # Pared to what is decided at the prompt: side, size, how, the stop,
        # what is at risk. SELL/BUY repeats long/short, DAY and GTC never
        # vary (entries DAY, stops GTC).
        # Risk counts the commission in and out, and slippage: half the
        # spread at the stop, and again at the entry when it goes in as a
        # market order (STP, MKT) or Adaptive Urgent, which takes the spread
        # to get done. Adaptive Normal fills at or inside the mid, and a
        # limit can't fill past its price (astor.core.commissions).
        slip = (half or 0.0) * (2 if req.order_type in ("STP", "MKT", "ADU") else 1)
        ref = req.price or market
        size = f", {_bold(f'~${req.qty * ref:,.0f}')}" if ref else ""
        stop = (
            f", stop {child.auxPrice:.2f} {_r_text(ref, abs(ref - child.auxPrice))}"
            f" (risk {_bold(f'${trade_risk(req.qty, ref, child.auxPrice, slip):,.0f}')})"
            if child is not None and ref else
            f", stop {child.auxPrice:.2f}" if child is not None else f", {_accent(_bold('NO stop'), _ANSI_RED)}"
        )
        kind = {
            "ADN": "Adaptive",
            "ADU": _bold("Adaptive Urgent"),
            "LMT": f"LMT {parent.lmtPrice:.2f}",
            "STP": f"STP {parent.auxPrice:.2f}",
        }.get(req.order_type, req.order_type)
        mkt = f"  [market {market:.2f}]" if market is not None else ""
        summary = (
            f"{_bold(req.symbol)} {req.direction} {_bold(f'{req.qty:g}')} {kind}{stop}{size}"
            f"  [{_account_tag(account).strip(' []')}]{mkt}" + (f"  ({'; '.join(notes)})" if notes else "")
        )
        return EntryTicket(req, account, contract, parent, child, summary)

    async def send_entry(
        self, ticket: EntryTicket, scanner: Scanner, run_id: str, store: TriggerStore
    ) -> str:
        req = ticket.req
        outcome, pt, ct = await self.desk.enter(ticket.contract, ticket.parent, ticket.child)
        self._record_order(
            store, run_id, "order_enter", req.symbol, req.direction, req.price or 0.0, outcome,
            {"account": ticket.account, "qty": req.qty, "order_type": req.order_type,
             "price": req.price, "stop": req.stop},
        )
        if pt is not None:
            task = asyncio.create_task(self._watch_entry(ticket, pt, ct, scanner, run_id, store))
            self._entry_tasks.add(task)
            task.add_done_callback(self._entry_tasks.discard)
        return _order_line(req.symbol, outcome)

    async def _watch_entry(
        self, ticket: EntryTicket, pt: Trade, ct: Trade | None,
        scanner: Scanner, run_id: str, store: TriggerStore,
    ) -> None:
        """Wait out the entry; on a fill with a stop, start `track` on it --
        entry at the fill, R to wherever the stop is by then. Never `trail`:
        that stays the trader's call."""
        sym = ticket.req.symbol
        try:
            while not pt.isDone():
                await asyncio.sleep(0.5)
            s = pt.orderStatus
            if s.status != "Filled":
                part = f" after {s.filled:g} filled -- not tracked" if s.filled else ""
                _print_order_notice(sym, f"entry {s.status.lower()}{part}")
                return
            fill = s.avgFillPrice
            self._record_order(
                store, run_id, "order_fill", sym, ticket.req.direction, fill, Outcome(True, "filled"),
                {"account": ticket.account, "qty": s.filled},
            )
            shares = s.filled if ticket.req.direction == "long" else -s.filled

            def filled(then: str) -> None:
                _print_fill_notice(sym, shares, fill, then)

            stop = ct.order.auxPrice if ct is not None and ct.orderStatus.status in ("PreSubmitted", "Submitted") else None
            if stop is None:
                filled("no working stop -- not tracked")
                return
            key = (sym, ticket.account)
            if key in self.tracker.trades or key in self.trailer.trades:
                # Nothing held beyond this fill (or IBKR's count not caught
                # up yet): the old trade is over -- stopped out, then
                # re-entered -- even if its flat position was never seen.
                if self._held(sym, ticket.account) not in (0, shares):
                    filled(f"adds to the trade tracked/trailed {_account_tag(ticket.account).strip()} -- left as is")
                    return
                self.tracker.trades.pop(key, None)
                self.trailer.trades.pop(key, None)
            long = ticket.req.direction == "long"
            if (stop >= fill) if long else (stop <= fill):
                filled(f"stop {stop:.2f} is past the fill -- not tracked")
                return
            opened_at = pt.fills[0].time.astimezone(ET) if pt.fills else datetime.now(ET)
            msg = await self.track(scanner, sym, fill, stop, run_id, store, ticket.account, opened_at)
            filled(msg)
        except Exception as e:
            logger.exception("watching the %s entry failed", sym)
            _print_order_notice(sym, f"lost track of the entry ({e!r}) -- CHECK ORDERS")

    async def set_stop(self, req: StopRequest, run_id: str, store: TriggerStore) -> str:
        """Make the position's protective stops exactly what `req` says --
        one price, or tranches. Before the entry fills, its attached stop
        can be moved, but not split."""
        sym = req.symbol
        open_trades = await self.ib.reqAllOpenOrdersAsync()
        try:
            account = self._account_for(req.account, sym, open_trades)
        except CommandError as e:
            return str(e)
        working = working_orders(open_trades, sym, account)
        held = self._held(sym, account)
        prices = [p for _, p in req.tranches]
        if held:
            long = held > 0
            exit_side = "SELL" if long else "BUY"
            stops = protective_stops(working, exit_side)
            try:
                sizes = tranche_sizes([p for p, _ in req.tranches], int(abs(held)))
            except CommandError as e:
                return f"{sym}: {e}"
            tranches = list(zip(sizes, prices, strict=True))
        else:
            ids = {t.order.orderId for t in working if t.order.orderId}
            stops = [t for t in working if t.order.parentId in ids and t.order.orderType == "STP"]
            if not stops:
                return f"No {sym} position, and no pending entry with a stop, in {account}."
            if len(req.tranches) != 1 or len(stops) != 1:
                return f"{sym} isn't filled yet: its stop can be moved, but split only once held."
            exit_side = stops[0].order.action
            long = exit_side == "SELL"
            tranches = [(stops[0].order.totalQuantity, prices[0])]
        if why := self._unbound(stops):
            return why
        contract = stops[0].contract if stops else await self._contract(sym)
        if contract is None:
            return f"IBKR doesn't know {sym}."
        problems = stop_problems(prices, long, await self._market_price(contract))
        if problems:
            return "NOT changed: " + "; ".join(problems)
        outcome = await self.desk.set_stops(contract, account, exit_side, stops, tranches)
        self._record_order(
            store, run_id, "order_stop", sym, "long" if long else "short", prices[0], outcome,
            {"account": account, "tranches": tranches},
        )
        return _order_line(sym, outcome)

    async def cancel(self, req: SymbolRequest, run_id: str, store: TriggerStore) -> str:
        """Cancel `symbol`'s working entries -- everything but the stops
        protecting what's held. An entry's attached stop goes with it."""
        sym = req.symbol
        open_trades = await self.ib.reqAllOpenOrdersAsync()
        try:
            account = self._account_for(req.account, sym, open_trades)
        except CommandError as e:
            return str(e)
        working = working_orders(open_trades, sym, account)
        held = self._held(sym, account)
        keep = protective_stops(working, "SELL" if held > 0 else "BUY") if held else []
        targets = [t for t in working if t not in keep]
        ids = {t.order.orderId for t in targets if t.order.orderId}
        targets = [t for t in targets if t.order.parentId not in ids]
        if not targets:
            return f"Nothing to cancel for {sym} in {account}" + (
                f" -- only the stop on {abs(held):g} held ('exit {sym}' to close it)." if keep else "."
            )
        if why := self._unbound(targets):
            return why
        outcome = await self.desk.cancel(targets)
        self._record_order(
            store, run_id, "order_cancel", sym, "", 0.0, outcome,
            {"account": account, "orders": [describe_order(t.order) for t in targets]},
        )
        return _order_line(sym, outcome)

    async def exit_position(self, req: SymbolRequest, run_id: str, store: TriggerStore) -> str:
        """Close the whole position: cancel its stops, then send the exit.
        Ends any track/trail on the symbol once the exit is in."""
        sym = req.symbol
        open_trades = await self.ib.reqAllOpenOrdersAsync()
        try:
            account = self._account_for(req.account, sym, open_trades)
        except CommandError as e:
            return str(e)
        # Fresh, after the stops were read: a stop that filled before this
        # read is gone from both; one that fills later is caught as it is
        # cancelled, and no exit goes out.
        held = await fresh_position(self.ib, sym, account)
        if not held:
            return f"No {sym} position in {account}."
        if req.order_type != "LMT" and not await self._in_rth(sym):
            return f"Outside regular hours only an LMT exit works: 'exit {sym} lmt PRICE'."
        long = held > 0
        working = working_orders(open_trades, sym, account)
        stops = protective_stops(working, "SELL" if long else "BUY")
        if why := self._unbound(stops):
            return why
        contract = stops[0].contract if stops else await self._contract(sym)
        if contract is None:
            return f"IBKR doesn't know {sym}."
        order = exit_order(
            "SELL" if long else "BUY", abs(held), account, req.order_type, req.price
        )
        outcome = await self.desk.exit(sym, contract, stops, order)
        if outcome.ok and not self.desk.dry_run and self.untrack(sym, account):
            outcome.text += "; tracking ended"
        others = [t for t in working if t not in stops]
        if others:
            outcome.text += f"; {len(others)} other order(s) still working -- 'cancel {sym}'"
        self._record_order(
            store, run_id, "order_exit", sym, "long" if long else "short", req.price or 0.0,
            outcome, {"account": account},
        )
        return _order_line(sym, outcome)

    async def position_lines(self) -> list[str]:
        """The console's `positions`: every open position in every account
        this login sees -- the one place both accounts show side by side,
        since IBKR Desktop can only reach one of them. Per position: shares,
        entry (IBKR's average cost), IB's own mark, gain/loss, the stops
        protecting it, and whether track or trail has it."""
        held = sorted(
            (p for p in self.ib.positions() if p.position),
            key=lambda p: (p.contract.symbol, p.account),
        )
        if not held:
            return ["No open positions."]
        pnls, open_trades = await asyncio.gather(
            asyncio.gather(*(position_pnl(self.ib, p) for p in held)),
            self.ib.reqAllOpenOrdersAsync(),
        )
        lines = [
            f"  {'SYM':<6} {'ACCT':<4} {'SHARES':>7} {'ENTRY':>9} {'MARK':>9} "
            f"{'G/L %':>7} {'G/L $':>9}  STOPS"
        ]
        for p, pnl in zip(held, pnls, strict=True):
            sym, long = p.contract.symbol, p.position > 0
            mark = pnl.value / p.position if pnl is not None and pnl.position else None
            if mark is not None:
                pct = (mark - p.avgCost) / p.avgCost * (1 if long else -1) * 100
                color = _ANSI_GREEN if pct >= 0 else _ANSI_RED
                gl = _accent(f"{pct:+.2f}%".rjust(7), color)
                dollars = f"{pnl.unrealizedPnL:>+9,.0f}"
            else:
                gl, dollars = f"{'n/a':>7}", f"{'':>9}"
            stops = protective_stops(
                working_orders(open_trades, sym, p.account), "SELL" if long else "BUY"
            )
            covered = sum(t.order.totalQuantity for t in stops)
            stop_text = ", ".join(
                f"{t.order.totalQuantity:g}@{t.order.auxPrice:.2f}" for t in stops
            ) or _accent("NONE", _ANSI_RED)
            if stops and covered < abs(p.position):
                stop_text += _accent(f" ({abs(p.position) - covered:g} unprotected)", _ANSI_RED)
            rules = [
                name for name, book in (("trail", self.trailer), ("track", self.tracker))
                if (sym, p.account) in book.trades
            ]
            lines.append(
                f"  {_symbol(sym)} {_account_tag(p.account).strip(' []') or p.account:<4} "
                f"{p.position:>+7g} {p.avgCost:>9.2f} "
                f"{f'{mark:.2f}' if mark is not None else 'n/a':>9} {gl} {dollars}  {stop_text}"
                + (f"  [{', '.join(rules)}]" if rules else "")
            )
        return lines

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
                        _print_trigger_alert(trigger, not self.muted)

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
                    # The quote for slippage is awaited; this callback isn't async.
                    task = asyncio.create_task(self._print_setup_alert_sized(
                        alert, alert.symbol in self.positions, not self.muted
                    ))
                    self._alert_tasks.add(task)
                    task.add_done_callback(self._alert_tasks.discard)

                avwap, yard = _avwap_inputs(scanner.indicators.get(symbol))
                for track_alert in self.tracker.on_close(
                    symbol, bar_event.close, bar_event.start, bar_event.end, avwap, yard
                ):
                    trade = self.tracker.trades[symbol, track_alert.account]
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
                            "rule": track_alert.rule,
                            "plan": trade.plan.describe(),
                        },
                    )
                    store.commit()
                    _print_track_alert(track_alert, trade)

                actions = self.trailer.on_close(
                    symbol, bar_event.close, bar_event.start, bar_event.end, avwap, yard
                )
                if actions:
                    # Orders are awaited, and this callback is not async.
                    # The rules have already moved on (a rule that fired is
                    # marked done), so a second close can't send a second
                    # order for it.
                    task = asyncio.create_task(self._act_on_trail_in_order(actions, run_id, store))
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
