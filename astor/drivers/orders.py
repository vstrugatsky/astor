"""Order placement: `trail` moving stops to breakeven and exiting, and the
console's own order commands -- `enter`, `stop`, `cancel`, `exit`. The one
place that talks to IBKR orders.

Two things make this work against orders the trader enters by hand, both
verified against the live gateway on 2026-10-11:

  * The session must connect as client ID 0 and call reqAutoOpenOrders(True).
    IBKR only lets an API client modify or cancel orders it owns; client 0 is
    the one that manual orders get bound to (they show up with negative
    order IDs). A hand edit in IBKR Desktop afterwards keeps the binding.
  * A modify is sent on a *copy* of the order. ib_async writes IBKR's echo of
    the order back onto the tracked one, so its auxPrice only changes when
    IBKR confirms -- mutating it in place would "confirm" instantly.

Exit = cancel the stops, wait for IBKR to confirm the cancels, then send the
exit. The same in both accounts: in the IRA a working SELL stop plus a SELL
exit reads to IBKR as a possible short sale and is refused; in the regular
account both could fill in a fast move and leave an unintended short. The
position is unprotected only between the cancel confirming and the exit
arriving, well under a second when tested.

A position's protective stop may be several orders -- `stop SYM 1/2 P1 1/2 P2`
splits it into tranches -- so everything here works on the list of them.

The trail exit is IBKR's Adaptive algo on a market order: guaranteed to fill,
but it works the order inside the spread first rather than taking the far
side at once -- the point on wide-spread names. Adaptive is regular-hours
only, which is fine: trail acts on RTH 1-min closes only.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from ib_async import IB, Contract, Order, TagValue, Trade

from astor.core.order_entry import ADAPTIVE, EnterRequest
from astor.core.tracking import TrackedTrade

# How long IBKR gets to confirm a modify, a cancel or a new order before it
# counts as failed. Each took under a second against the gateway.
CONFIRM_TIMEOUT = 10.0

# How long an exit gets to fill before it is reported as still working.
# Not a deadline on the order -- it stays working either way.
FILL_REPORT_TIMEOUT = 120.0

# Normal works the order inside the spread for longer than Urgent does. A
# giveback exit is not seconds-urgent; the price on a wide spread is. The
# console's default, adn, is the same; adu is there for fast movers.
ADAPTIVE_PRIORITY = "Normal"


def _adaptive(order: Order, priority: str) -> None:
    order.orderType = "MKT"
    order.algoStrategy = "Adaptive"
    order.algoParams = [TagValue("adaptivePriority", priority)]

# The 15:59 exit has about a minute before the close: a fill matters more
# than the spread there.
EOD_ADAPTIVE_PRIORITY = "Urgent"

_WORKING = ("PreSubmitted", "Submitted")
_CANCELLED = ("Cancelled", "ApiCancelled")


@dataclass
class Outcome:
    """What happened, for the alert line and the store. `ok` False means a
    step failed and the trader needs to look at the position."""

    ok: bool
    text: str
    extras: dict = field(default_factory=dict)


def _tick(price: float) -> float:
    return 0.01 if price >= 1.0 else 0.0001


def breakeven_price(trade: TrackedTrade) -> float:
    """Entry rounded to a valid tick, away from a loss: up for a long, down
    for a short. avgCost carries commissions and many decimals; rounding the
    other way would put a "breakeven" stop a fraction below breakeven."""
    tick = _tick(trade.entry)
    steps = trade.entry / tick
    n = math.ceil(steps - 1e-6) if trade.direction == "long" else math.floor(steps + 1e-6)
    return round(n * tick, 4)


async def _wait_for(cond: Callable[[], bool], timeout: float = CONFIRM_TIMEOUT) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.1)
    return True


def _errors_since(trade: Trade, n: int) -> list[str]:
    """IBKR errors recorded on `trade` after its first `n` log entries."""
    return [f"{e.errorCode} {e.message}" for e in trade.log[n:] if e.errorCode]


def working_orders(open_trades: list[Trade], symbol: str, account: str) -> list[Trade]:
    return [
        t
        for t in open_trades
        if t.contract.symbol == symbol
        and t.order.account == account
        and t.orderStatus.status in _WORKING
    ]


def protective_stops(working: list[Trade], exit_side: str) -> list[Trade]:
    """The stops protecting a held position: working STPs on the exit side,
    not counting one attached to an entry that hasn't filled yet -- that
    one protects shares not yet held."""
    parents = {t.order.orderId for t in working if t.order.orderId}
    return [
        t
        for t in working
        if t.order.orderType == "STP"
        and t.order.action == exit_side
        and t.order.parentId not in parents
    ]


async def fresh_position(ib: IB, symbol: str, account: str) -> float:
    """The position as IBKR has it now -- a round trip, not ib_async's
    cached copy, which can trail a fill that just happened. IBKR answers in
    order, so any fill it reported first is in this answer."""
    positions = await asyncio.wait_for(ib.reqPositionsAsync(), CONFIRM_TIMEOUT)
    return sum(
        p.position for p in positions
        if p.account == account and p.contract.symbol == symbol
    )


def _fmt_prices(stops: list[Trade]) -> str:
    return ", ".join(f"{t.order.totalQuantity:g}@{t.order.auxPrice:.2f}" for t in stops)


def entry_orders(req: EnterRequest, account: str) -> tuple[Order, Order | None]:
    """The entry, DAY, and the stop loss attached to it, GTC. IBKR holds an
    attached stop until the entry fills and cancels it with the entry.

    An LMT entry works in extended hours too -- the way in before the open
    for a short, which IBKR Desktop doesn't offer this trader. The stop
    still triggers in regular hours only: pre-market prints are thin."""
    parent = Order(
        action=req.action, totalQuantity=req.qty, tif="DAY", account=account
    )
    if req.order_type in ADAPTIVE:
        _adaptive(parent, ADAPTIVE[req.order_type])
    else:
        parent.orderType = req.order_type
    if req.order_type == "LMT":
        parent.lmtPrice = req.price
        parent.outsideRth = True
    if req.order_type == "STP":
        parent.auxPrice = req.price
    if req.stop is None:
        return parent, None
    child = Order(
        action="SELL" if req.action == "BUY" else "BUY",
        totalQuantity=req.qty,
        orderType="STP",
        auxPrice=req.stop,
        tif="GTC",
        account=account,
    )
    return parent, child


def exit_order(
    action: str, qty: float, account: str, order_type: str = "ADN",
    price: float | None = None, priority: str | None = None,
) -> Order:
    """A DAY exit. An LMT exit is allowed outside regular hours -- the one
    exit that can be, since MKT and Adaptive only work in them. `priority`
    overrides an Adaptive type's own (trail's end-of-day exit)."""
    order = Order(action=action, totalQuantity=qty, tif="DAY", account=account)
    if order_type in ADAPTIVE:
        _adaptive(order, priority or ADAPTIVE[order_type])
    elif order_type == "LMT":
        order.orderType = "LMT"
        order.lmtPrice = price
        order.outsideRth = True
    else:
        order.orderType = "MKT"
    return order


def describe_order(o: Order) -> str:
    if o.algoStrategy == "Adaptive":
        kind = f"Adaptive market ({o.algoParams[0].value})" if o.algoParams else "Adaptive market"
    elif o.orderType == "LMT":
        kind = f"LMT {o.lmtPrice:.2f}"
    elif o.orderType == "STP":
        kind = f"STP {o.auxPrice:.2f}"
    else:
        kind = o.orderType
    return f"{o.action} {o.totalQuantity:g} {kind} {o.tif}"


class _OrderBase:
    def __init__(
        self,
        ib: IB,
        dry_run: bool,
        notify: Callable[[str, str], None],
        notify_fill: Callable[[str, float, float], None],
    ):
        self.ib = ib
        self.dry_run = dry_run
        # (symbol, text) -- for what happens after a call returns
        self.notify = notify
        # (symbol, signed shares, price) -- an exit's fill
        self.notify_fill = notify_fill
        self._tasks: set[asyncio.Task] = set()

    def _tag(self) -> str:
        return "[dry run] would " if self.dry_run else ""

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _modify(
        self, t: Trade, price: float | None = None, qty: float | None = None
    ) -> tuple[bool, str]:
        """Modify a working order's stop price and/or size; True once IBKR
        echoes both back."""
        n = len(t.log)
        m = copy.copy(t.order)
        if price is not None:
            m.auxPrice = price
        if qty is not None:
            m.totalQuantity = qty
        self.ib.placeOrder(t.contract, m)
        await _wait_for(
            lambda: (
                abs(t.order.auxPrice - m.auxPrice) < 1e-9
                and t.order.totalQuantity == m.totalQuantity
            )
            or bool(_errors_since(t, n))
        )
        errors = _errors_since(t, n)
        done = abs(t.order.auxPrice - m.auxPrice) < 1e-9 and t.order.totalQuantity == m.totalQuantity
        return done and not errors, "; ".join(errors)

    async def _cancel(self, trades: list[Trade]) -> tuple[list[Trade], list[Trade], list[Trade]]:
        """Cancel `trades` and wait for IBKR's answer on each. Returns
        (cancelled, filled first, unconfirmed)."""
        for t in trades:
            self.ib.cancelOrder(t.order)
        await _wait_for(
            lambda: all(t.orderStatus.status in _CANCELLED + ("Filled",) for t in trades)
        )
        cancelled = [t for t in trades if t.orderStatus.status in _CANCELLED]
        filled = [t for t in trades if t.orderStatus.status == "Filled"]
        stuck = [t for t in trades if t not in cancelled and t not in filled]
        return cancelled, filled, stuck

    async def _place_stop(self, contract: Contract, old: Order, qty: float, price: float) -> Trade:
        new = Order(
            action=old.action,
            totalQuantity=qty,
            orderType="STP",
            auxPrice=price,
            tif=old.tif or "GTC",
            outsideRth=old.outsideRth,
            account=old.account,
        )
        return self.ib.placeOrder(contract, new)

    async def _restore(self, stops: list[Trade]) -> bool:
        placed = [
            await self._place_stop(t.contract, t.order, t.order.totalQuantity, t.order.auxPrice)
            for t in stops
        ]
        return await _wait_for(lambda: all(t.orderStatus.status in _WORKING for t in placed))

    async def _flatten(
        self, symbol: str, stops: list[Trade], contract: Contract, order: Order, extras: dict
    ) -> Outcome:
        """Cancel `stops`, then send `order`. A refused exit puts the stops
        back; an unanswered one doesn't, since it may still be live."""
        what = describe_order(order)
        if self.dry_run:
            pre = f"cancel stop(s) {_fmt_prices(stops)}, then " if stops else ""
            return Outcome(True, f"{self._tag()}{pre}{what}", extras)

        # 1. cancel the stops and wait for IBKR to say so
        if stops:
            cancelled, filled, stuck = await self._cancel(stops)
            if filled:
                if len(filled) == len(stops):
                    return Outcome(True, "the stop filled while being cancelled -- out", extras)
                restored = await self._restore(cancelled)
                return Outcome(
                    False,
                    f"stop {_fmt_prices(filled)} filled while being cancelled; exit not sent, "
                    + ("the other stops restored -- CHECK THE POSITION" if restored
                       else "the other stops NOT restored -- POSITION IS UNPROTECTED"),
                    extras,
                )
            if stuck:
                return Outcome(
                    False,
                    f"stop cancel NOT confirmed ({_fmt_prices(stuck)}); exit not sent"
                    + (" -- some stops ARE cancelled, CHECK THE POSITION" if cancelled else ""),
                    extras,
                )

        # 2. the exit -- SMART whatever the stop was routed to: Adaptive is
        # SMART-only, so a stop directed at one exchange would otherwise get
        # its exit refused.
        contract = copy.copy(contract)
        contract.exchange = "SMART"
        exit_trade = self.ib.placeOrder(contract, order)
        await _wait_for(
            lambda: exit_trade.orderStatus.status in _WORKING + ("Filled",)
            or exit_trade.isDone()
        )
        lead = "stop cancelled, " if stops else ""
        if exit_trade.orderStatus.status in _WORKING + ("Filled",):
            self._spawn(self._report_fill(symbol, exit_trade))
            return Outcome(
                True,
                f"{lead}{what} sent",
                extras | {"exit_order_id": exit_trade.order.orderId},
            )

        # 3a. no answer at all: the exit may still be live, so putting the
        # stop back could double the sale. Say so and leave it to the trader.
        if not exit_trade.isDone():
            return Outcome(
                False,
                f"exit sent but NOT confirmed (status {exit_trade.orderStatus.status})"
                + ("; stop is cancelled" if stops else "") + " -- CHECK THE POSITION",
                extras,
            )

        # 3b. the exit was refused: put the stops back rather than leave the
        # position naked
        errors = _errors_since(exit_trade, 0)
        refused = f"exit REFUSED ({'; '.join(errors) or exit_trade.orderStatus.status})"
        if not stops:
            return Outcome(False, refused, extras)
        restored = await self._restore(stops)
        return Outcome(
            False,
            f"{refused}; "
            + (f"stop restored at {_fmt_prices(stops)}" if restored
               else "stop NOT restored -- POSITION IS UNPROTECTED"),
            extras,
        )

    async def _report_fill(self, symbol: str, t: Trade) -> None:
        filled = await _wait_for(lambda: t.orderStatus.status == "Filled", FILL_REPORT_TIMEOUT)
        s = t.orderStatus
        if filled:
            self.notify_fill(
                symbol, s.filled if t.order.action == "BUY" else -s.filled, s.avgFillPrice
            )
        else:
            self.notify(
                symbol,
                f"exit still {s.status} after {FILL_REPORT_TIMEOUT:.0f}s "
                f"({s.filled:g} of {t.order.totalQuantity:g} filled)",
            )


class TrailExecutor(_OrderBase):
    async def _find_stops(self, trade: TrackedTrade) -> tuple[list[Trade], str]:
        """The working stops protecting this position -- one, or several
        tranches -- re-read from IBKR every time: the trader may have moved
        them since the last look."""
        exit_side = "SELL" if trade.direction == "long" else "BUY"
        working = working_orders(
            await self.ib.reqAllOpenOrdersAsync(), trade.symbol, trade.account or ""
        )
        stops = protective_stops(working, exit_side)
        if not stops:
            return [], f"found no working {exit_side} stop"
        if not self.dry_run and any(t.order.orderId == 0 for t in stops):
            return [], "a stop is not bound to this session (connect as client ID 0)"
        return stops, ""

    async def _position_size(self, trade: TrackedTrade) -> float:
        return await fresh_position(self.ib, trade.symbol, trade.account or "")

    async def move_stop_to_breakeven(self, trade: TrackedTrade) -> Outcome:
        """Every stop tranche still short of breakeven goes to breakeven."""
        stops, why = await self._find_stops(trade)
        if not stops:
            return Outcome(False, f"stop NOT moved: {why}")
        be = breakeven_price(trade)
        before = [t.order.auxPrice for t in stops]
        extras = {"stop_before": before[0] if len(before) == 1 else before, "breakeven": be}
        # Never loosen: a stop the trader already raised past entry stays.
        loose = [
            t for t in stops
            if (t.order.auxPrice < be if trade.direction == "long" else t.order.auxPrice > be)
        ]
        if not loose:
            return Outcome(
                True,
                f"{'stop' if len(stops) == 1 else 'stops'} already at or past breakeven "
                f"({_fmt_prices(stops)}) -- not moved",
                extras,
            )
        moving = ", ".join(f"{t.order.auxPrice:.2f}" for t in loose)
        if self.dry_run:
            return Outcome(True, f"{self._tag()}move stop {moving} -> {be:.2f}", extras)

        failed = []
        for t in loose:
            current = t.order.auxPrice
            ok, errors = await self._modify(t, price=be)
            if not ok:
                failed.append(f"{current:.2f} (still {t.order.auxPrice:.2f}{': ' + errors if errors else ''})")
        if not failed:
            return Outcome(True, f"stop moved {moving} -> {be:.2f}", extras)
        return Outcome(False, f"stop move to {be:.2f} NOT confirmed for {'; '.join(failed)}", extras)

    async def exit(self, trade: TrackedTrade, priority: str = ADAPTIVE_PRIORITY) -> Outcome:
        # Stops, then the position, fresh (see exit_shares): a tranche that
        # filled a moment ago would otherwise be sold again. One that fills
        # after this is caught as it is cancelled -- no exit goes out.
        stops, why = await self._find_stops(trade)
        qty = abs(await self._position_size(trade))
        if qty == 0:
            return Outcome(True, "no position left -- the stop likely filled first")
        if not stops:
            return Outcome(False, f"NOT exited: {why}")
        action = "SELL" if trade.direction == "long" else "BUY"
        extras = {
            "qty": qty,
            "stop_cancelled": stops[0].order.auxPrice if len(stops) == 1
            else [t.order.auxPrice for t in stops],
            "priority": priority,
        }
        order = exit_order(action, qty, trade.account or "", "ADN", priority=priority)
        return await self._flatten(trade.symbol, stops, stops[0].contract, order, extras)


    async def exit_shares(
        self, trade: TrackedTrade, n: int, priority: str = ADAPTIVE_PRIORITY
    ) -> Outcome:
        """Sell `n` of the position -- one rule's share of it -- and keep the
        rest protected. The stop is cut first, by exactly the shares going:
        left whole, it would later sell more than is held and flip the
        position. The cut comes off the stop furthest from the price, the
        tranche least likely to fire. All of it, or nothing left beyond
        `n`: the whole exit."""
        long = trade.direction == "long"
        action = "SELL" if long else "BUY"
        before: dict[int, float] = {}  # stop's order id -> its size before any cut
        done: list[tuple[Trade, float]] = []  # cuts made: (stop, its new size; 0 = cancel)
        cut_text = ""

        # Cut, then look again with fresh reads, until the stops cover no
        # more than what the sale leaves. A tranche filling between the
        # reads and the cut would otherwise leave them covering shares no
        # longer held -- the stop could later sell more than held and flip
        # the position. Once it holds, a later stop fill keeps it holding:
        # it takes as many shares off the stops as off the position.
        #
        # Stops first, then the position: IBKR answers in order, so a fill
        # between the two reads shows in the position but may still count
        # in the stops -- which overstates the stops and only cuts more.
        # The other way round it would overstate the position: the danger.
        for _ in range(3):
            stops, why = await self._find_stops(trade)
            held = abs(await self._position_size(trade))
            if held == 0:
                restored = await self._uncut(done, before)
                return Outcome(True, "no position left -- the stop likely filled first"
                               + ("" if restored else "; stops NOT restored"))
            if n >= held:
                return await self.exit(trade, priority)
            if n <= 0:
                return Outcome(True, "no shares in this rule's part -- nothing sold")
            if not stops:
                restored = await self._uncut(done, before)
                return Outcome(False, f"NOT sold: {why}"
                               + ("" if restored else " -- stops NOT restored, CHECK THE POSITION"))
            excess = sum(t.order.totalQuantity for t in stops) - (held - n)
            if excess <= 0:
                break
            # Furthest from the price first: the lowest stop under a long.
            cuts: list[tuple[Trade, float]] = []
            for t in sorted(stops, key=lambda t: t.order.auxPrice, reverse=not long):
                if excess <= 0:
                    break
                take = min(excess, t.order.totalQuantity)
                cuts.append((t, t.order.totalQuantity - take))
                excess -= take
            for t, _q in cuts:
                before.setdefault(t.order.orderId, t.order.totalQuantity)
            cut_text = ", ".join(
                f"{t.order.totalQuantity:g}@{t.order.auxPrice:.2f} -> {'cancel' if q == 0 else f'{q:g}'}"
                for t, q in cuts
            )
            if self.dry_run:
                exit = exit_order(action, n, trade.account or "", "ADN", priority=priority)
                return Outcome(True, f"{self._tag()}cut stop {cut_text}, then {describe_order(exit)}",
                               {"qty": n, "held": held})
            for t, q in cuts:
                if q == 0:
                    cancelled, filled, stuck = await self._cancel([t])
                    ok = bool(cancelled)
                    err = "filled first" if filled else "cancel not confirmed" if stuck else ""
                else:
                    ok, err = await self._modify(t, qty=q)
                if not ok:
                    restored = await self._uncut(done, before)
                    return Outcome(
                        False,
                        f"stop cut NOT confirmed ({err or 'no answer'}); nothing sold"
                        + ("" if restored else " -- stops NOT restored, CHECK THE POSITION"),
                    )
                done.append((t, q))
        else:
            restored = await self._uncut(done, before)
            return Outcome(
                False,
                "position and stops kept changing; nothing sold"
                + ("" if restored else " -- stops NOT restored, CHECK THE POSITION"),
            )

        extras = {"qty": n, "held": held, "stops_before": _fmt_prices(stops), "priority": priority}
        exit = exit_order(action, n, trade.account or "", "ADN", priority=priority)
        what = describe_order(exit)
        if self.dry_run:
            return Outcome(True, f"{self._tag()}{what}", extras)

        # The sale
        contract = copy.copy(stops[0].contract)
        contract.exchange = "SMART"
        sale = self.ib.placeOrder(contract, exit)
        await _wait_for(
            lambda: sale.orderStatus.status in _WORKING + ("Filled",) or sale.isDone()
        )
        if sale.orderStatus.status in _WORKING + ("Filled",):
            self._spawn(self._report_fill(trade.symbol, sale))
            return Outcome(
                True,
                f"stop cut {cut_text}, {what} sent" if done else f"{what} sent",
                extras | {"exit_order_id": sale.order.orderId},
            )
        if not sale.isDone():
            return Outcome(
                False,
                f"sale sent but NOT confirmed (status {sale.orderStatus.status}); "
                "stop already cut -- CHECK THE POSITION",
                extras,
            )
        # Refused: the stops go back to covering the whole position
        errors = _errors_since(sale, 0)
        restored = await self._uncut(done, before)
        return Outcome(
            False,
            f"sale REFUSED ({'; '.join(errors) or sale.orderStatus.status}); "
            + ("stops restored" if restored else "stops NOT restored -- CHECK THE POSITION"),
            extras,
        )

    async def _uncut(self, done: list[tuple[Trade, float]], before: dict[int, float]) -> bool:
        """Put the stops `exit_shares` cut back to their old sizes."""
        ok = True
        for t, q in done:
            size = before[t.order.orderId]
            if q == 0:
                placed = await self._place_stop(t.contract, t.order, size, t.order.auxPrice)
                ok &= await _wait_for(lambda p=placed: p.orderStatus.status in _WORKING)
            else:
                ok &= (await self._modify(t, qty=size))[0]
        return ok


class OrderDesk(_OrderBase):
    """The console's `enter`, `stop`, `cancel` and `exit`. The caller has
    already resolved the account, the position and the orders involved;
    this sends and confirms."""

    async def enter(
        self, contract: Contract, parent: Order, child: Order | None
    ) -> tuple[Outcome, Trade | None, Trade | None]:
        what = describe_order(parent) + (
            f", stop {describe_order(child)}" if child else ""
        )
        if self.dry_run:
            return Outcome(True, f"{self._tag()}send {what}"), None, None
        if child is not None:
            # Held back until the child is sent, so the entry can't fill
            # without its stop attached.
            parent.orderId = self.ib.client.getReqId()
            parent.transmit = False
            child.parentId = parent.orderId
            child.transmit = True
        pt = self.ib.placeOrder(contract, parent)
        ct = self.ib.placeOrder(contract, child) if child is not None else None
        sent = [pt] + ([ct] if ct else [])

        def settled() -> bool:
            return all(t.orderStatus.status in _WORKING + ("Filled",) or t.isDone() for t in sent)

        await _wait_for(settled)
        extras = {"entry_order_id": pt.order.orderId}
        if ct is not None:
            extras["stop_order_id"] = ct.order.orderId
        errors = [e for t in sent for e in _errors_since(t, 0)]
        live = [t.orderStatus.status in _WORKING + ("Filled",) for t in sent]
        if all(live):
            note = f" ({'; '.join(errors)})" if errors else ""
            return Outcome(True, f"sent {what}{note}", extras), pt, ct
        if any(t.isDone() for t in sent):
            # a refused child takes the parent with it at IBKR; cancel the
            # parent too in case it didn't
            if not pt.isDone():
                self.ib.cancelOrder(pt.order)
            return Outcome(
                False,
                f"REFUSED: {'; '.join(errors) or ', '.join(t.orderStatus.status for t in sent)}",
                extras,
            ), None, None
        return Outcome(
            False,
            f"sent but NOT confirmed ({', '.join(t.orderStatus.status for t in sent)})"
            " -- CHECK ORDERS",
            extras,
        ), pt, ct

    async def set_stops(
        self, contract: Contract, account: str, exit_side: str,
        existing: list[Trade], tranches: list[tuple[int, float]],
    ) -> Outcome:
        """Make the working stops exactly `tranches` (size, price): existing
        orders are modified where they can be, surplus ones cancelled, the
        rest created GTC. Shrinking goes first, so the stops never add up to
        more shares than are held -- the IRA reads that as a short sale."""
        plan_existing = existing[: len(tranches)]
        surplus = existing[len(tranches):]
        to_create = tranches[len(existing):]
        mods = sorted(
            zip(plan_existing, tranches[: len(plan_existing)], strict=True),
            key=lambda pair: pair[1][0] - pair[0].order.totalQuantity,
        )
        target = ", ".join(f"{q:g}@{p:.2f}" for q, p in tranches)
        before = _fmt_prices(existing) or "none"
        extras = {"before": before, "after": target}
        if self.dry_run:
            return Outcome(True, f"{self._tag()}set stops {before} -> {target}", extras)

        problems = []
        if surplus:
            _, filled, stuck = await self._cancel(surplus)
            if filled or stuck:
                problems.append(f"cancel of {_fmt_prices(filled + stuck)} not confirmed")
        for t, (qty, price) in mods:
            if t.order.totalQuantity == qty and abs(t.order.auxPrice - price) < 1e-9:
                continue
            ok, errors = await self._modify(t, price=price, qty=qty)
            if not ok:
                problems.append(
                    f"modify to {qty:g}@{price:.2f} NOT confirmed"
                    f" (still {t.order.totalQuantity:g}@{t.order.auxPrice:.2f}"
                    + (f": {errors}" if errors else "") + ")"
                )
        template = existing[0].order if existing else Order(
            action=exit_side, tif="GTC", account=account
        )
        created = [await self._place_stop(contract, template, q, p) for q, p in to_create]
        if created:
            await _wait_for(lambda: all(t.orderStatus.status in _WORKING or t.isDone() for t in created))
            for t in created:
                if t.orderStatus.status not in _WORKING:
                    problems.append(
                        f"new stop {t.order.totalQuantity:g}@{t.order.auxPrice:.2f} NOT working"
                        f" ({'; '.join(_errors_since(t, 0)) or t.orderStatus.status})"
                    )
        if problems:
            return Outcome(False, f"stops {before} -> {target}: {'; '.join(problems)} -- CHECK ORDERS", extras)
        return Outcome(True, f"stops {before} -> {target}", extras)

    async def cancel(self, trades: list[Trade]) -> Outcome:
        listed = "; ".join(describe_order(t.order) for t in trades)
        if self.dry_run:
            return Outcome(True, f"{self._tag()}cancel {listed}")
        cancelled, filled, stuck = await self._cancel(trades)
        parts = []
        if cancelled:
            parts.append(f"cancelled {'; '.join(describe_order(t.order) for t in cancelled)}")
        if filled:
            parts.append(f"FILLED before the cancel: {'; '.join(describe_order(t.order) for t in filled)}")
        if stuck:
            parts.append(f"cancel NOT confirmed: {'; '.join(describe_order(t.order) for t in stuck)}")
        return Outcome(not (filled or stuck), "; ".join(parts))

    async def exit(
        self, symbol: str, contract: Contract, stops: list[Trade], order: Order
    ) -> Outcome:
        extras = {
            "qty": order.totalQuantity,
            "stops_cancelled": [t.order.auxPrice for t in stops],
            "order": describe_order(order),
        }
        return await self._flatten(symbol, stops, contract, order, extras)
