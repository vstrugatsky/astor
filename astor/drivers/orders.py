"""Order placement for `trail`: moving a stop to breakeven, and exiting.

Two things make this work against stops the trader enters by hand, both
verified against the live gateway on 2026-10-11:

  * The session must connect as client ID 0 and call reqAutoOpenOrders(True).
    IBKR only lets an API client modify or cancel orders it owns; client 0 is
    the one that manual orders get bound to (they show up with negative
    order IDs). A hand edit in IBKR Desktop afterwards keeps the binding.
  * A modify is sent on a *copy* of the order. ib_async writes IBKR's echo of
    the order back onto the tracked one, so its auxPrice only changes when
    IBKR confirms -- mutating it in place would "confirm" instantly.

Exit = cancel the stop, wait for IBKR to confirm the cancel, then send the
exit. The same in both accounts: in the IRA a working SELL stop plus a SELL
exit reads to IBKR as a possible short sale and is refused; in the regular
account both could fill in a fast move and leave an unintended short. The
position is unprotected only between the cancel confirming and the exit
arriving, well under a second when tested.

The exit is IBKR's Adaptive algo on a market order: guaranteed to fill, but
it works the order inside the spread first rather than taking the far side
at once -- the point on wide-spread names. Adaptive is regular-hours only,
which is fine: trail acts on RTH 1-min closes only.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from ib_async import IB, Order, TagValue, Trade

from astor.core.tracking import TrackedTrade

# How long IBKR gets to confirm a modify, a cancel or a new order before it
# counts as failed. Each took under a second against the gateway.
CONFIRM_TIMEOUT = 10.0

# How long an Adaptive exit gets to fill before trail says it is still
# working. Not a deadline on the order -- it stays working either way.
FILL_REPORT_TIMEOUT = 120.0

# Normal works the order inside the spread for longer than Urgent does. A
# giveback exit is not seconds-urgent; the price on a wide spread is.
ADAPTIVE_PRIORITY = "Normal"

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


class TrailExecutor:
    def __init__(self, ib: IB, dry_run: bool, notify: Callable[[str, str], None]):
        self.ib = ib
        self.dry_run = dry_run
        # (symbol, text) -- for what happens after `exit` returns: the fill
        self.notify = notify
        self._tasks: set[asyncio.Task] = set()

    def _tag(self) -> str:
        return "[dry run] would " if self.dry_run else ""

    async def _find_stop(self, trade: TrackedTrade) -> tuple[Trade | None, str]:
        """The one working stop protecting this position, re-read from IBKR
        every time -- the trader may have moved it by hand since the last
        look. Same matching as `resolve_open_trade`."""
        exit_side = "SELL" if trade.direction == "long" else "BUY"
        stops = [
            t
            for t in await self.ib.reqAllOpenOrdersAsync()
            if t.contract.symbol == trade.symbol
            and t.order.account == trade.account
            and t.order.orderType == "STP"
            and t.order.action == exit_side
            and t.orderStatus.status in _WORKING
        ]
        if len(stops) != 1:
            return None, f"found {len(stops)} working {exit_side} stops, expected 1"
        if not self.dry_run and stops[0].order.orderId == 0:
            return None, "the stop is not bound to this session (connect as client ID 0)"
        return stops[0], ""

    def _position_size(self, trade: TrackedTrade) -> float:
        return sum(
            p.position
            for p in self.ib.positions(trade.account or "")
            if p.contract.symbol == trade.symbol
        )

    async def move_stop_to_breakeven(self, trade: TrackedTrade) -> Outcome:
        stop, why = await self._find_stop(trade)
        if stop is None:
            return Outcome(False, f"stop NOT moved: {why}")
        current = stop.order.auxPrice
        be = breakeven_price(trade)
        extras = {"stop_before": current, "breakeven": be}
        # Never loosen: a stop the trader already raised past entry stays.
        already = current >= be if trade.direction == "long" else current <= be
        if already:
            return Outcome(True, f"stop already at {current:.2f}, left alone", extras)
        if self.dry_run:
            return Outcome(True, f"{self._tag()}move stop {current:.2f} -> {be:.2f}", extras)

        n = len(stop.log)
        modified = copy.copy(stop.order)
        modified.auxPrice = be
        self.ib.placeOrder(stop.contract, modified)
        ok = await _wait_for(
            lambda: abs(stop.order.auxPrice - be) < 1e-9 or bool(_errors_since(stop, n))
        )
        errors = _errors_since(stop, n)
        if ok and not errors:
            return Outcome(True, f"stop moved {current:.2f} -> {be:.2f}", extras)
        return Outcome(
            False,
            f"stop move to {be:.2f} NOT confirmed (still {stop.order.auxPrice:.2f})"
            + (f": {'; '.join(errors)}" if errors else ""),
            extras,
        )

    async def exit(self, trade: TrackedTrade) -> Outcome:
        qty = abs(self._position_size(trade))
        if qty == 0:
            return Outcome(True, "no position left -- the stop likely filled first")
        stop, why = await self._find_stop(trade)
        if stop is None:
            return Outcome(False, f"NOT exited: {why}")
        action = "SELL" if trade.direction == "long" else "BUY"
        extras = {"qty": qty, "stop_cancelled": stop.order.auxPrice, "priority": ADAPTIVE_PRIORITY}
        if self.dry_run:
            return Outcome(
                True,
                f"{self._tag()}cancel stop {stop.order.auxPrice:.2f}, then "
                f"{action} {qty:g} Adaptive market ({ADAPTIVE_PRIORITY})",
                extras,
            )

        # 1. cancel the stop and wait for IBKR to say so
        self.ib.cancelOrder(stop.order)
        await _wait_for(lambda: stop.orderStatus.status in _CANCELLED + ("Filled",))
        status = stop.orderStatus.status
        if status == "Filled":
            return Outcome(True, "the stop filled while being cancelled -- out", extras)
        if status not in _CANCELLED:
            return Outcome(False, f"stop cancel NOT confirmed (status {status}); exit not sent", extras)

        # 2. the exit
        order = Order(
            action=action,
            totalQuantity=qty,
            orderType="MKT",
            tif="DAY",
            account=trade.account or "",
            algoStrategy="Adaptive",
            algoParams=[TagValue("adaptivePriority", ADAPTIVE_PRIORITY)],
        )
        exit_trade = self.ib.placeOrder(stop.contract, order)
        await _wait_for(
            lambda: exit_trade.orderStatus.status in _WORKING + ("Filled",)
            or exit_trade.isDone()
        )
        if exit_trade.orderStatus.status in _WORKING + ("Filled",):
            task = asyncio.create_task(self._report_fill(trade.symbol, exit_trade))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return Outcome(
                True,
                f"stop cancelled, {action} {qty:g} Adaptive market sent",
                extras | {"exit_order_id": exit_trade.order.orderId},
            )

        # 3a. no answer at all: the exit may still be live, so putting the
        # stop back could double the sale. Say so and leave it to the trader.
        if not exit_trade.isDone():
            return Outcome(
                False,
                f"exit sent but NOT confirmed (status {exit_trade.orderStatus.status}); "
                "stop is cancelled -- CHECK THE POSITION",
                extras,
            )

        # 3b. the exit was refused: put the stop back rather than leave the
        # position naked
        errors = _errors_since(exit_trade, 0)
        restored = await self._restore_stop(stop)
        return Outcome(
            False,
            f"exit REFUSED ({'; '.join(errors) or exit_trade.orderStatus.status}); "
            + (f"stop restored at {stop.order.auxPrice:.2f}" if restored
               else "stop NOT restored -- POSITION IS UNPROTECTED"),
            extras,
        )

    async def _restore_stop(self, old: Trade) -> bool:
        o = old.order
        new = Order(
            action=o.action,
            totalQuantity=o.totalQuantity,
            orderType="STP",
            auxPrice=o.auxPrice,
            tif=o.tif,
            outsideRth=o.outsideRth,
            account=o.account,
        )
        t = self.ib.placeOrder(old.contract, new)
        return await _wait_for(lambda: t.orderStatus.status in _WORKING)

    async def _report_fill(self, symbol: str, t: Trade) -> None:
        filled = await _wait_for(lambda: t.orderStatus.status == "Filled", FILL_REPORT_TIMEOUT)
        s = t.orderStatus
        if filled:
            self.notify(symbol, f"exit filled: {s.filled:g} @ {s.avgFillPrice:.4f}")
        else:
            self.notify(
                symbol,
                f"exit still {s.status} after {FILL_REPORT_TIMEOUT:.0f}s "
                f"({s.filled:g} of {t.order.totalQuantity:g} filled)",
            )
