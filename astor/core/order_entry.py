"""Parsing and checking for the live console's order commands -- `enter`,
`stop`, `cancel`, `exit` -- with no IBKR in it, so every rule here is
testable on hand-built input. Placing the orders is astor.drivers.orders.

    enter SYM [ACCOUNT] l|long|s|short QTY [adn|adu|mkt|lmt PRICE|stp PRICE] [stop PRICE]
    buy|sell SYM [ACCOUNT] QTY ...   (enter, the direction in the verb)
    stop SYM [ACCOUNT] PRICE
    stop SYM [ACCOUNT] PART PRICE PART PRICE ...   (PART: 1/2, or a share count)
    cancel SYM [ACCOUNT]
    exit SYM [ACCOUNT] [adn | adu | mkt | lmt PRICE]

adn/adu are IBKR's Adaptive algo on a market order, Normal or Urgent
priority; adn is the default for both commands.

ACCOUNT is `ira`, `reg`, or an IBKR account ID, any case. `enter` without one
goes to the IRA for a long and the regular account for a short.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

# Which managed account each alias means, by the last digit of its ID. The
# IRA can't hold a short: an entry that would open one there is refused.
ACCOUNT_SUFFIX = {"ira": "6", "reg": "9"}

DIRECTIONS = {"l": "long", "long": "long", "s": "short", "short": "short"}
# Adaptive's two priorities, as order types of their own: Normal works the
# order inside the spread longer, Urgent gets it done -- for fast movers.
ADAPTIVE = {"ADN": "Normal", "ADU": "Urgent"}
# 'alg', the old name for adn, still types the same order.
ENTRY_TYPES = {"adn": "ADN", "adu": "ADU", "alg": "ADN", "mkt": "MKT", "lmt": "LMT", "stp": "STP"}
EXIT_TYPES = {"adn": "ADN", "adu": "ADU", "alg": "ADN", "mkt": "MKT", "lmt": "LMT"}
PRICED = ("LMT", "STP")


class CommandError(ValueError):
    """A command that can't be acted on, with the reason to print."""


def tick(price: float) -> float:
    return 0.01 if price >= 1.0 else 0.0001


def round_to_tick(price: float) -> float:
    t = tick(price)
    return round(round(price / t) * t, 4)


def _price(tok: str, what: str) -> float:
    try:
        p = float(tok)
    except ValueError:
        raise CommandError(f"{what} '{tok}' is not a price") from None
    if not math.isfinite(p) or p <= 0:
        raise CommandError(f"{what} must be a positive price, got '{tok}'")
    return round_to_tick(p)


def _symbol(tok: str) -> str:
    return tok.upper().replace(".", " ")


def is_account(tok: str) -> bool:
    """`ira`, `reg`, or an IBKR account ID -- letters then digits (U1234566).
    Nothing else the commands take looks like that."""
    t = tok.lower()
    return t in ACCOUNT_SUFFIX or (
        t[0].isalpha() and any(c.isdigit() for c in t) and t.isalnum()
    )


def account_token(tok: str) -> str:
    return tok.lower() if tok.lower() in ACCOUNT_SUFFIX else tok.upper()


def _split_account(tokens: list[str]) -> tuple[str | None, list[str]]:
    if tokens and is_account(tokens[0]):
        return account_token(tokens[0]), tokens[1:]
    return None, tokens


@dataclass(frozen=True)
class EnterRequest:
    symbol: str
    account: str | None  # as typed: 'ira', 'reg', an ID, or None
    direction: str  # 'long' | 'short'
    qty: int
    order_type: str  # ADN | ADU | MKT | LMT | STP
    price: float | None
    stop: float | None
    # No type typed: ADN in regular hours; outside them live makes it an
    # LMT at the ask (long) or bid (short).
    type_defaulted: bool = False

    @property
    def action(self) -> str:
        return "BUY" if self.direction == "long" else "SELL"


ENTER_USAGE = (
    "enter SYM [ACCOUNT] l|s QTY [adn|adu|mkt|lmt PRICE|stp PRICE] [stop PRICE]"
    "  e.g. 'enter SPCX short 20 stp 170.10 stop 172.01'; the type defaults to adn"
)
BUY_SELL_USAGE = (
    "buy|sell SYM [ACCOUNT] QTY [adn|adu|mkt|lmt PRICE|stp PRICE] [stop PRICE]"
    "  e.g. 'buy NVDA 100 stop 35.99' -- Adaptive Normal unless a type is given"
)


def parse_enter(rest: str, direction: str | None = None) -> EnterRequest:
    """`enter SYM [ACCOUNT] l|s QTY ...`, or with `direction` given --
    `buy`/`sell`, the same order with the direction in the verb -- without
    the l|s. The order type is optional: none means Adaptive Normal."""
    usage = ENTER_USAGE if direction is None else BUY_SELL_USAGE
    tokens = rest.split()
    if not tokens:
        raise CommandError(usage)
    symbol = _symbol(tokens[0])
    account, tokens = _split_account(tokens[1:])
    if direction is None:
        if len(tokens) < 2:
            raise CommandError(usage)
        direction = DIRECTIONS.get(tokens[0].lower())
        if direction is None:
            raise CommandError(f"direction '{tokens[0]}' is not l/long/s/short")
        tokens = tokens[1:]
    if not tokens:
        raise CommandError(usage)
    try:
        qty = int(tokens[0])
    except ValueError:
        raise CommandError(f"share count '{tokens[0]}' is not a whole number") from None
    if qty <= 0:
        raise CommandError("share count must be positive")
    tokens = tokens[1:]
    order_type = ENTRY_TYPES.get(tokens[0].lower()) if tokens else None
    if order_type is None:
        # No type given: Adaptive Normal. Anything else here is a mistyped type.
        if tokens and tokens[0].lower() != "stop":
            raise CommandError(f"'{tokens[0]}' is not an order type (adn/adu/mkt/lmt/stp) or 'stop PRICE'")
        order_type, defaulted = "ADN", True
    else:
        tokens = tokens[1:]
        defaulted = False

    price = None
    if order_type in PRICED:
        if not tokens or tokens[0].lower() == "stop":
            raise CommandError(f"{order_type} needs a price: '... {order_type.lower()} PRICE'")
        price = _price(tokens[0], f"{order_type} price")
        tokens = tokens[1:]

    stop = None
    if tokens:
        if tokens[0].lower() != "stop" or len(tokens) != 2:
            raise CommandError(
                f"unexpected '{' '.join(tokens)}'"
                + (" -- MKT/ADN/ADU take no price" if order_type not in PRICED else "")
                + "; the stop loss is 'stop PRICE'"
            )
        stop = _price(tokens[1], "stop")
    return EnterRequest(symbol, account, direction, qty, order_type, price, stop, defaulted)


@dataclass(frozen=True)
class StopRequest:
    symbol: str
    account: str | None
    # (part, price): part is a Fraction of the position or an int share count
    tranches: list[tuple[Fraction | int, float]]
    # 'stop SYM be': every stop short of breakeven goes to it
    breakeven: bool = False


STOP_USAGE = "stop SYM [ACCOUNT] PRICE, stop SYM [ACCOUNT] 1/2 PRICE 1/2 PRICE, or stop SYM [ACCOUNT] be"


def _part(tok: str) -> Fraction | int:
    try:
        if "/" in tok:
            f = Fraction(tok)
            if not 0 < f <= 1:
                raise CommandError(f"part '{tok}' must be between 0 and 1")
            return f
        n = int(tok)
    except (ValueError, ZeroDivisionError):
        raise CommandError(f"part '{tok}' is not a fraction (1/2) or a share count") from None
    if n <= 0:
        raise CommandError(f"part '{tok}' must be positive")
    return n


def parse_stop(rest: str) -> StopRequest:
    tokens = rest.split()
    if not tokens:
        raise CommandError(STOP_USAGE)
    symbol = _symbol(tokens[0])
    account, tokens = _split_account(tokens[1:])
    if len(tokens) == 1 and tokens[0].lower() == "be":
        return StopRequest(symbol, account, [], breakeven=True)
    if len(tokens) == 1:
        return StopRequest(symbol, account, [(Fraction(1), _price(tokens[0], "stop"))])
    if not tokens or len(tokens) % 2:
        raise CommandError(STOP_USAGE)
    tranches = [
        (_part(tokens[i]), _price(tokens[i + 1], "stop")) for i in range(0, len(tokens), 2)
    ]
    return StopRequest(symbol, account, tranches)


def tranche_sizes(parts: list[Fraction | int], position: int) -> list[int]:
    """Share counts for each part of a `position`-share holding. Fractions
    are floored and the shares that leaves over go to the last fractional
    part, so 1/2 + 1/2 of 21 is 10 + 11. The parts must cover the position
    exactly: a stop for less leaves shares unprotected, for more could open
    a position the other way."""
    sizes = [p if isinstance(p, int) else math.floor(p * position) for p in parts]
    fractional = [i for i, p in enumerate(parts) if not isinstance(p, int)]
    # flooring loses under a share per fractional part; more than that is
    # parts that don't add up, not rounding
    leftover = position - sum(sizes)
    if fractional and 0 < leftover < len(fractional):
        sizes[fractional[-1]] += leftover
    if sum(sizes) != position:
        raise CommandError(
            f"the parts come to {sum(sizes)} shares, the position is {position}"
        )
    if any(s <= 0 for s in sizes):
        raise CommandError(f"a part rounds to no shares ({' + '.join(map(str, sizes))})")
    return sizes


@dataclass(frozen=True)
class SymbolRequest:
    """`cancel` and `exit`: a symbol, an account, and for `exit` the order."""

    symbol: str
    account: str | None
    order_type: str = "ADN"
    price: float | None = None
    # `exit` only: part of the position -- a share count or a fraction --
    # or None for all of it
    qty: Fraction | int | None = None


def parse_cancel(rest: str) -> SymbolRequest:
    tokens = rest.split()
    if not tokens:
        raise CommandError("cancel SYM [ACCOUNT]")
    account, extra = _split_account(tokens[1:])
    if extra:
        raise CommandError(f"unexpected '{' '.join(extra)}'; usage: cancel SYM [ACCOUNT]")
    return SymbolRequest(_symbol(tokens[0]), account)


EXIT_USAGE = "exit SYM [ACCOUNT] [SHARES | 1/3] [adn | adu | mkt | lmt PRICE]  (default: all of it, adn)"


def parse_exit(rest: str) -> SymbolRequest:
    tokens = rest.split()
    if not tokens:
        raise CommandError(EXIT_USAGE)
    symbol = _symbol(tokens[0])
    account, tokens = _split_account(tokens[1:])
    qty = None
    if tokens and (tokens[0][0].isdigit()):
        qty = _part(tokens[0])
        tokens = tokens[1:]
    if not tokens:
        return SymbolRequest(symbol, account, qty=qty)
    order_type = EXIT_TYPES.get(tokens[0].lower())
    if order_type is None:
        raise CommandError(f"exit order '{tokens[0]}' is not adn/adu/mkt/lmt; {EXIT_USAGE}")
    if order_type == "LMT":
        if len(tokens) != 2:
            raise CommandError("an LMT exit needs a price: 'exit SYM [SHARES] lmt PRICE'")
        return SymbolRequest(symbol, account, "LMT", _price(tokens[1], "LMT price"), qty)
    if len(tokens) != 1:
        raise CommandError(f"{order_type} takes no price; {EXIT_USAGE}")
    return SymbolRequest(symbol, account, order_type, qty=qty)


def resolve_account(token: str | None, managed: list[str]) -> str | None:
    """The managed account `token` names, None for no token. Raises when
    it names none of them, or an alias matches more than one."""
    if token is None:
        return None
    if token in ACCOUNT_SUFFIX:
        hits = [a for a in managed if a.endswith(ACCOUNT_SUFFIX[token])]
        if len(hits) != 1:
            raise CommandError(
                f"'{token}' should be the one account ending in {ACCOUNT_SUFFIX[token]}; "
                f"this login has {', '.join(managed) or 'none'}"
            )
        return hits[0]
    hits = [a for a in managed if a.upper() == token.upper()]
    if not hits:
        raise CommandError(f"{token} is not an account on this login ({', '.join(managed)})")
    return hits[0]


def is_ira(account: str) -> bool:
    return account.endswith(ACCOUNT_SUFFIX["ira"])


def entry_problems(req: EnterRequest, market: float | None, held: float) -> list[str]:
    """Why `req` should not be sent, given the current price (None when it
    couldn't be read -- the checks that need it are skipped) and the shares
    already held in the target account (negative for a short)."""
    problems = []
    long = req.direction == "long"
    if held and (held > 0) != long:
        problems.append(
            f"already {'long' if held > 0 else 'short'} {abs(held):g} in this account"
            f" -- 'exit {req.symbol}' first"
        )
    if req.order_type == "STP" and market is not None:
        if long and req.price <= market:
            problems.append(
                f"a buy stop at {req.price:.2f} is not above the market ({market:.2f})"
                " -- it would trigger at once"
            )
        if not long and req.price >= market:
            problems.append(
                f"a sell stop at {req.price:.2f} is not below the market ({market:.2f})"
                " -- it would trigger at once"
            )
    ref = req.price if req.order_type in PRICED else market
    if req.stop is not None and ref is not None:
        if long and req.stop >= ref:
            problems.append(f"stop {req.stop:.2f} is not below the entry ({ref:.2f})")
        if not long and req.stop <= ref:
            problems.append(f"stop {req.stop:.2f} is not above the entry ({ref:.2f})")
    return problems


def stop_problems(prices: list[float], long: bool, market: float | None) -> list[str]:
    """A protective stop on the wrong side of the market fires at once."""
    if market is None:
        return []
    bad = [p for p in prices if (p >= market if long else p <= market)]
    side = "below" if long else "above"
    return [f"stop {p:.2f} is not {side} the market ({market:.2f})" for p in bad]
