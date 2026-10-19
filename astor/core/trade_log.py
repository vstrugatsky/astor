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
    stop: float | None = None  # the original stop, when astor knows it

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
                else:
                    take = qty if abs(qty) <= abs(pos) else -pos  # up to flat, the rest opens the next
                    current._out.append((abs(take), f.price))
                current.commission += share_of_comm * abs(take)
                pos += take
                qty -= take
                current.shares = max(current.shares, abs(pos))
                if pos == 0:
                    current.closed_at = f.time
    # Carried trades first (no open time today), then by when they opened.
    trades.sort(key=lambda t: (t.opened_at is not None, t.opened_at.timestamp() if t.opened_at else 0.0))
    return trades


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
        if t.opened_at is not None:
            enters = [
                r for r in mine
                if r["source"] == "enter" and t.opened_at - ENTRY_LEAD <= r["time"] <= t.opened_at + timedelta(minutes=1)
            ]
            if enters:
                t.stop = enters[-1]["stop"]
                continue
        during = [
            r for r in mine
            if r["source"] == "track/trail"
            and (t.opened_at is None or r["time"] >= t.opened_at)
            and (end is None or r["time"] <= end + timedelta(minutes=1))
        ]
        if during:
            t.stop = during[0]["stop"]
