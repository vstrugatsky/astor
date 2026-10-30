"""Today's trades, rebuilt from fills: the `trades` report.

A trade is one round trip in one symbol and account -- from the fill that
takes the position off zero to the one that brings it back. Adds and
partial exits stay inside the trade; entry and exit are share-weighted
averages. A fill that goes through zero (long 100, sell 300) closes the
trade and opens the next with what's left. Pure: the fills come in, the
trades go out; reading them from IBKR is astor.drivers.trades_report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Fill:
    time: datetime
    symbol: str
    account: str
    shares: float  # signed: + bought, - sold
    price: float
    commission: float = 0.0
    order_id: int = 0  # IBKR's permId: an order's partial fills share it


@dataclass(frozen=True)
class Exit:
    """One exit order: its fills taken together."""

    time: datetime  # the last fill's
    shares: float
    price: float  # share-weighted
    commission: float
    order_id: int = 0


def _merge_exits(pieces: list[Exit]) -> list[Exit]:
    """Consecutive pieces of one exit order taken together."""
    out: list[Exit] = []
    for e in pieces:
        last = out[-1] if out else None
        if last is not None and e.order_id and e.order_id == last.order_id:
            n = last.shares + e.shares
            out[-1] = Exit(e.time, n, (last.price * last.shares + e.price * e.shares) / n,
                           last.commission + e.commission, e.order_id)
        else:
            out.append(e)
    return out


@dataclass
class Lot:
    """One entry order's shares, and the exits matched to them, first in
    first out: a stop-out on half comes off the first entry, and a later
    sale of everything takes the rest of it, then the add."""

    time: datetime | None  # None: held from an earlier day
    shares: float
    price: float | None    # None: carried, its entry isn't in today's fills
    commission: float = 0.0
    order_id: int = 0
    remaining: float = 0.0
    _exits: list[Exit] = field(default_factory=list)
    stop: float | None = None
    limit: float | None = None
    rvol: float | None = None
    adr1_pct: float | None = None

    @property
    def exits(self) -> list[Exit]:
        return _merge_exits(self._exits)

    def pnl(self, e: Exit, direction: str) -> float | None:
        """An exit's dollars against this lot: the move from its entry,
        less the exit's commission and this lot's own, by shares."""
        if self.price is None:
            return None
        sign = 1 if direction == "long" else -1
        return sign * (e.price - self.price) * e.shares - e.commission - self.commission * e.shares / self.shares

    def r(self, e: Exit, direction: str) -> float | None:
        pnl = self.pnl(e, direction)
        if pnl is None or self.stop is None or self.price is None or self.stop == self.price:
            return None
        return pnl / (abs(self.price - self.stop) * e.shares)


@dataclass
class Trade:
    symbol: str
    account: str
    direction: str  # 'long' | 'short'
    # held from before today: entry and its time are not in today's fills
    carried: float = 0.0
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    shares: float = 0.0  # the most held at once
    _in: list[tuple[float, float]] = field(default_factory=list)   # (shares, price) opening
    _out: list[tuple[float, float]] = field(default_factory=list)  # (shares, price) closing
    commission: float = 0.0
    in_commission: float = 0.0  # the entry fills' share of it
    _exits: list[Exit] = field(default_factory=list)  # one per fill, merged by `exits`
    lots: list[Lot] = field(default_factory=list)  # one per entry order, oldest first
    stop: float | None = None  # the original stop, when astor knows it
    limit: float | None = None  # the entry's limit (a stop-limit's cap), when astor sent it
    rvol: float | None = None  # at entry: volume through the last bar before it, against its curve
    adr1_pct: float | None = None  # at entry: today's range so far, % of the entry price

    @property
    def exits(self) -> list[Exit]:
        """Each exit order once: consecutive fills of one order merged."""
        return _merge_exits(self._exits)

    def exit_pnl(self, e: Exit) -> float | None:
        """One exit's dollars: its shares' move from the entry, less its
        own commission and its shares' part of the entry's."""
        if self.entry is None or self.carried:
            return None
        bought = sum(q for q, _ in self._in)
        sign = 1 if self.direction == "long" else -1
        return sign * (e.price - self.entry) * e.shares - e.commission - self.in_commission * e.shares / bought

    def exit_r(self, e: Exit) -> float | None:
        pnl = self.exit_pnl(e)
        if pnl is None or self.stop is None or self.entry is None or self.stop == self.entry:
            return None
        return pnl / (abs(self.entry - self.stop) * e.shares)

    @property
    def entry(self) -> float | None:
        n = sum(q for q, _ in self._in)
        return sum(q * p for q, p in self._in) / n if n else None

    @property
    def exit(self) -> float | None:
        n = sum(q for q, _ in self._out)
        return sum(q * p for q, p in self._out) / n if n else None

    @property
    def exited(self) -> float:
        return sum(q for q, _ in self._out)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def pnl(self) -> float | None:
        """Dollars on the shares closed today, net of today's commissions;
        None when the entry isn't in today's fills."""
        if self.entry is None or self.exit is None or self.carried:
            return None
        sign = 1 if self.direction == "long" else -1
        return sign * (self.exit - self.entry) * self.exited - self.commission

    @property
    def pnl_r(self) -> float | None:
        if self.pnl is None or self.stop is None or self.entry is None or self.stop == self.entry:
            return None
        return self.pnl / (abs(self.entry - self.stop) * self.exited)


def round_trips(fills: list[Fill], start: dict[tuple[str, str], float] | None = None) -> list[Trade]:
    """`fills` in any order; `start` is each (symbol, account)'s position
    before the first of them -- nonzero for one held from an earlier day.
    Trades come back in the order they opened."""
    start = start or {}
    trades: list[Trade] = []
    by_key: dict[tuple[str, str], list[Fill]] = {}
    for f in sorted(fills, key=lambda f: f.time):
        by_key.setdefault((f.symbol, f.account), []).append(f)

    for key, fs in by_key.items():
        pos = start.get(key, 0.0)
        current: Trade | None = None
        if pos:
            current = Trade(key[0], key[1], "long" if pos > 0 else "short", carried=abs(pos), shares=abs(pos))
            current.lots.append(Lot(None, abs(pos), None, remaining=abs(pos)))
            trades.append(current)
        for f in fs:
            qty = f.shares
            share_of_comm = f.commission / abs(f.shares) if f.shares else 0.0
            while qty:
                if current is None or current.closed_at is not None:
                    current = Trade(key[0], key[1], "long" if qty > 0 else "short", opened_at=f.time)
                    trades.append(current)
                opening = (pos >= 0) == (qty > 0) or pos == 0
                if opening:
                    take = qty
                    current._in.append((abs(take), f.price))
                    current.in_commission += share_of_comm * abs(take)
                    _add_to_lot(current, f, abs(take), share_of_comm * abs(take))
                else:
                    take = qty if abs(qty) <= abs(pos) else -pos  # up to flat, the rest opens the next
                    current._out.append((abs(take), f.price))
                    current._exits.append(Exit(f.time, abs(take), f.price, share_of_comm * abs(take), f.order_id))
                    _take_from_lots(current, f, abs(take), share_of_comm)
                current.commission += share_of_comm * abs(take)
                pos += take
                qty -= take
                current.shares = max(current.shares, abs(pos))
                if pos == 0:
                    current.closed_at = f.time
    # Carried trades first (no open time today), then by when they opened.
    trades.sort(key=lambda t: (t.opened_at is not None, t.opened_at.timestamp() if t.opened_at else 0.0))
    return trades


def _add_to_lot(t: Trade, f: Fill, shares: float, commission: float) -> None:
    """An entry fill: onto the last lot if it is the same order's, else a
    new lot."""
    last = t.lots[-1] if t.lots else None
    if last is not None and f.order_id and last.order_id == f.order_id and last.price is not None:
        n = last.shares + shares
        last.price = (last.price * last.shares + f.price * shares) / n
        last.shares, last.remaining = n, last.remaining + shares
        last.commission += commission
    else:
        t.lots.append(Lot(f.time, shares, f.price, commission, f.order_id, remaining=shares))


def _take_from_lots(t: Trade, f: Fill, shares: float, commission_per_share: float) -> None:
    """An exit fill, matched to the open lots oldest first."""
    for lot in t.lots:
        if shares <= 1e-9:
            break
        piece = min(lot.remaining, shares)
        if piece <= 1e-9:
            continue
        lot._exits.append(Exit(f.time, piece, f.price, commission_per_share * piece, f.order_id))
        lot.remaining -= piece
        shares -= piece


# How long before its first fill an entry may have been sent and still be
# this trade's: a resting stop or limit entry waits a while to fill.
ENTRY_LEAD = timedelta(minutes=30)


def attach_stops(trades: list[Trade], records: list[dict]) -> None:
    """Each trade's original stop, from what astor wrote down (store's
    stop_records): the stop its entry was sent with -- the latest entry in
    the symbol and account shortly before the first fill -- or else the
    stop the first track/trail action on it measured R from."""
    for t in trades:
        mine = [r for r in records if r["symbol"] == t.symbol and r["account"] == t.account]
        end = t.closed_at
        during = [
            r for r in mine
            if r["source"] == "track/trail"
            and (t.opened_at is None or r["time"] >= t.opened_at)
            and (end is None or r["time"] <= end + timedelta(minutes=1))
        ]
        fallback = during[0]["stop"] if during else None
        # Each lot: the stop its own entry was sent with, else the trade's
        for lot in t.lots:
            enters = [] if lot.time is None else [
                r for r in mine
                if r["source"] == "enter" and lot.time - ENTRY_LEAD <= r["time"] <= lot.time + timedelta(minutes=1)
            ]
            if enters:
                lot.stop, lot.limit = enters[-1]["stop"], enters[-1].get("limit")
            else:
                lot.stop = fallback
        first = t.lots[0] if t.lots else None
        t.stop = first.stop if first is not None and first.stop is not None else fallback
        t.limit = first.limit if first is not None else None


def entry_rvol(t: Trade, bars: list, curve) -> float | None:
    """RVOL when `t` was entered: today's volume through the last bar that
    had closed by the entry fill, against the average at that bar -- the
    same reading the setups take on each bar's close. None before the
    first bar closes, or without bars or a curve."""
    from astor.core.indicators import compute_rvol

    if t.opened_at is None or not bars or curve is None:
        return None
    done = [b for b in bars if b.end <= t.opened_at]
    if not done:
        return None
    return compute_rvol(sum(b.volume for b in done), done[-1].start, curve)


def entry_range_pct(t: Trade, bars: list) -> float | None:
    """ADR(1) when `t` was entered: today's range over the bars closed by
    the entry fill, as a percent of the entry price."""
    if t.opened_at is None or t.entry is None or not bars:
        return None
    done = [b for b in bars if b.end <= t.opened_at]
    if not done:
        return None
    return (max(b.high for b in done) - min(b.low for b in done)) / t.entry * 100


def lot_rvol(lot: Lot, bars: list, curve) -> float | None:
    from astor.core.indicators import compute_rvol

    if lot.time is None or not bars or curve is None:
        return None
    done = [b for b in bars if b.end <= lot.time]
    return compute_rvol(sum(b.volume for b in done), done[-1].start, curve) if done else None


def lot_range_pct(lot: Lot, bars: list) -> float | None:
    if lot.time is None or lot.price is None or not bars:
        return None
    done = [b for b in bars if b.end <= lot.time]
    if not done:
        return None
    return (max(b.high for b in done) - min(b.low for b in done)) / lot.price * 100
