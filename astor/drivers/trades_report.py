"""The `trades` report: today's round trips from IBKR's fills, with the
original stop from astor's own records -- for the short side, which
IBKR Desktop no longer shows this trader. Read-only."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date
from zoneinfo import ZoneInfo

from ib_async import IB, ExecutionFilter

from astor.core.order_entry import ACCOUNT_SUFFIX, is_ira
from astor.core.store import TriggerStore
from astor.core.trade_log import Fill, Trade, attach_stops, round_trips

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
# Commission reports trail their fills by a few seconds.
COMMISSION_WAIT = 8.0


def _alias(account: str) -> str:
    return "ira" if is_ira(account) else "reg" if account.endswith(ACCOUNT_SUFFIX["reg"]) else account


async def todays_trades(ib: IB, store: TriggerStore, session: date) -> list[Trade]:
    await ib.reqExecutionsAsync(ExecutionFilter())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + COMMISSION_WAIT
    while loop.time() < deadline:
        if all(f.commissionReport.currency for f in ib.fills()):
            break
        await asyncio.sleep(0.25)
    fills = [
        Fill(
            f.execution.time.astimezone(ET), f.contract.symbol, f.execution.acctNumber,
            f.execution.shares if f.execution.side == "BOT" else -f.execution.shares,
            f.execution.price, f.commissionReport.commission or 0.0,
        )
        for f in ib.fills()
        if f.contract.secType == "STK" and f.execution.time.astimezone(ET).date() == session
    ]
    # Where each position stood before today's first fill: now, less today.
    now = {(p.contract.symbol, p.account): p.position for p in await ib.reqPositionsAsync()}
    net: dict[tuple[str, str], float] = {}
    for f in fills:
        net[f.symbol, f.account] = net.get((f.symbol, f.account), 0.0) + f.shares
    start = {k: now.get(k, 0.0) - n for k, n in net.items()}
    trades = round_trips(fills, start)
    attach_stops(trades, store.stop_records(session))
    return trades


def format_trades(trades: list[Trade], color: Callable[[str, float], str] = lambda s, v: s) -> list[str]:
    """The table, one trade a row, then the day's total. `color` paints a
    P&L cell by its sign."""
    if not trades:
        return ["  No trades today."]
    head = f"  {'SYM':<6} {'ACCT':<4} {'SIDE':<5} {'SHARES':>6}  {'IN':>8} {'ENTRY':>9}  {'OUT':>8} {'EXIT':>9}  {'STOP':>8}  {'P&L':>9} {'R':>6}"
    lines = [head]
    total = 0.0
    for t in trades:
        entry = f"{t.entry:.4f}".rstrip("0").rstrip(".") if t.entry is not None else "--"
        exit_ = f"{t.exit:.4f}".rstrip("0").rstrip(".") if t.exit is not None else "--"
        tin = f"{t.opened_at:%H:%M:%S}" if t.opened_at else "earlier"
        tout = f"{t.closed_at:%H:%M:%S}" if t.closed_at else "open"
        stop = f"{t.stop:.2f}" if t.stop is not None else "--"
        if t.pnl is not None:
            total += t.pnl
            pnl = color(f"{t.pnl:>+9,.2f}", t.pnl)
            r = color(f"{t.pnl_r:>+6.2f}", t.pnl) if t.pnl_r is not None else f"{'--':>6}"
        else:
            pnl, r = f"{'--':>9}", f"{'--':>6}"
        if t.is_open and t.exited:
            tout = f"part {t.exited:g}"
        lines.append(
            f"  {t.symbol:<6} {_alias(t.account):<4} {t.direction:<5} {t.shares:>6g}  {tin:>8} {entry:>9}"
            f"  {tout:>8} {exit_:>9}  {stop:>8}  {pnl} {r}"
        )
    lines.append(f"  {'':<52}{'net':>8}  {color(f'{total:>+9,.2f}', total)}")
    lines.append("  P&L is net of commissions, on the shares closed today; STOP is the original one astor recorded.")
    return lines
