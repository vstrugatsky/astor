"""What a trade costs beyond its stop distance, so a risk budget can pay
for it: IBKR's commission, and slippage.

IBKR Pro, Fixed pricing -- read off the trader's own fills (2026-10-13:
30 SKHY bought for $1.00, the $1 minimum): $0.005 a share, at least $1 an
order, at most 1% of the order's value. Charged on the way in and again on
the way out, so a trade's risk is its stop distance plus both.

Regulatory fees on sales (SEC, FINRA TAF, CAT) are left out: cents on
the sizes this trades, against the dollars the per-share rate costs on a
low-priced stock bought by the thousand.

Slippage is passed in as dollars a share (`slip`), since it comes from a
live quote: half the bid/ask spread at the stop, which becomes a market
order when hit. The trader's fills (2026-10-14) back that: stops slipped
0 to 1/2 of the half spread, while Adaptive entries filled at or inside
the midpoint -- so an Adaptive or limit entry carries no slip, and a
stop or market entry carries another half spread.
"""

from __future__ import annotations

PER_SHARE = 0.005
MINIMUM = 1.00
MAX_PCT = 0.01


def commission(shares: float, price: float) -> float:
    """One order's commission: `shares` (either sign) at `price`."""
    shares = abs(shares)
    if shares == 0:
        return 0.0
    return min(max(PER_SHARE * shares, MINIMUM), MAX_PCT * shares * price)


def round_trip(shares: float, entry: float, exit: float) -> float:
    """In at `entry` and out at `exit`."""
    return commission(shares, entry) + commission(shares, exit)


def trade_risk(shares: float, entry: float, stop: float, slip: float = 0.0) -> float:
    """Dollars lost if the stop is hit: the distance, slippage on top of
    it (`slip` a share), and both commissions."""
    return abs(shares) * (abs(entry - stop) + slip) + round_trip(shares, entry, stop)


def shares_for_risk(entry: float, stop: float, max_loss: float, slip: float = 0.0) -> int:
    """The most shares whose `trade_risk` stays within `max_loss`; 0 when
    none do (or there is no distance between entry and stop)."""
    distance = abs(entry - stop)
    if distance <= 0 or max_loss <= 0:
        return 0
    # Commission never costs more than PER_SHARE a share each way beyond
    # its minimum, so this lands at or one or two shares off the answer.
    n = int(max_loss / (distance + slip + 2 * PER_SHARE))
    while n > 0 and trade_risk(n, entry, stop, slip) > max_loss:
        n -= 1
    while trade_risk(n + 1, entry, stop, slip) <= max_loss:
        n += 1
    return n
