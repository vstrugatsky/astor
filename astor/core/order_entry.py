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
EXIT_TYPES = {
    "adn": "ADN", "adu": "ADU", "alg": "ADN", "mkt": "MKT",
    # the same three words as an entry's price cap
    "lmt": "LMT", "lim": "LMT", "limit": "LMT",
}
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
    qty: int  # 0: astor works it out from the stop and the max loss
    order_type: str  # ADN | ADU | MKT | LMT | STP | STP LMT
    # STP and STP LMT: the trigger. LMT: the limit -- a cap on the price,
    # worked by Adaptive in regular hours.
    price: float | None
    stop: float | str | None  # 'lod'/'hod' until live looks it up
    # No type typed: ADN in regular hours; outside them live makes it an
    # LMT at the ask (long) or bid (short).
    type_defaulted: bool = False
    # 'buy SYM orb [N]': an opening-range break over the first N minutes --
    # entry and stop are left for live to work out from the range.
    orb_minutes: int | None = None
    # STP LMT: the cap on the fill once the stop triggers
    cap: float | None = None
    # an Adaptive limit at Urgent priority
    urgent: bool = False
    # 'loss 250': the max loss this entry is sized to, in place of live's
    max_loss: float | None = None

    @property
    def action(self) -> str:
        return "BUY" if self.direction == "long" else "SELL"


ENTER_USAGE = (
    "enter SYM [ACCOUNT] l|s [QTY] ... -- the same options as buy/sell"
    "  e.g. 'enter SPCX short 20 stp 170.10 stop 172.01'"
)
BUY_SELL_USAGE = (
    "buy|sell SYM [ACCOUNT] [QTY] [stop P] [when|stp P] [limit|lim|lmt P] [urgent|urg] [mkt]"
    " [loss|maxloss $], or buy|sell SYM [ACCOUNT] orb [MINUTES] [lim P] [loss $]"
    "  e.g. 'buy NVDA stop 35.99' -- astor sizes it; Adaptive Normal unless told otherwise"
)
ORB_MINUTES = 1

# Keyword -> what it sets. Synonyms side by side: 'when' reads apart from
# the protective 'stop' better than 'stp' does.
_TRIGGER = ("when", "stp")
_CAP = ("limit", "lim", "lmt")
_URGENT = ("urgent", "urg", "adu")
_LOSS = ("loss", "maxloss")


def parse_enter(rest: str, direction: str | None = None) -> EnterRequest:
    """`buy|sell SYM [ACCOUNT] [QTY] OPTIONS`, or `enter SYM [ACCOUNT]
    l|s ...` with the direction spelled out. With no share count astor
    sizes the entry from the protective stop and the max loss.

    OPTIONS, in any order: `stop P` (the protective stop), `when|stp P`
    (a stop entry triggering at P), `limit|lim|lmt P` (a cap on the
    entry price), `urgent|urg` (Adaptive Urgent), `mkt` (a plain market
    order), `adn` (Adaptive Normal, the default), `loss|maxloss $`, and
    `orb [MINUTES]`. A trigger and a cap together make a stop-limit."""
    usage = ENTER_USAGE if direction is None else BUY_SELL_USAGE
    tokens = rest.split()
    if not tokens:
        raise CommandError(usage)
    symbol = _symbol(tokens[0])
    account, tokens = _split_account(tokens[1:])
    if direction is None:
        if not tokens:
            raise CommandError(usage)
        direction = DIRECTIONS.get(tokens[0].lower())
        if direction is None:
            raise CommandError(f"direction '{tokens[0]}' is not l/long/s/short")
        tokens = tokens[1:]
    if not tokens:
        raise CommandError(usage)

    qty: int | None = None
    # A share of the position is a partial exit's, not an entry's: 'sell'
    # opens a short, it doesn't sell part of a long (2026-10-26).
    if parse_share(tokens[0]) is not None:
        example = " ".join([f"exit {symbol}", *tokens])
        raise CommandError(
            f"'{tokens[0]}' is part of a position -- to sell part of one: '{example}'. "
            f"buy and sell open a new position, and take a share count"
        )
    if tokens[0][0].isdigit():
        try:
            qty = int(tokens[0])
        except ValueError:
            raise CommandError(f"share count '{tokens[0]}' is not a whole number") from None
        if qty <= 0:
            raise CommandError("share count must be positive")
        tokens = tokens[1:]

    seen: dict[str, object] = {}

    def once(key: str, value: object, tok: str) -> None:
        if key in seen:
            raise CommandError(f"'{tok}' given twice")
        seen[key] = value

    i = 0
    while i < len(tokens):
        tok = tokens[i].lower()
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok in ("stop",) + _TRIGGER + _CAP + _LOSS:
            if nxt is None:
                raise CommandError(f"'{tok}' needs a value: '{tok} PRICE'")
            if tok in _LOSS:
                once("loss", _dollars(nxt), tok)
            elif tok == "stop":
                once("stop", _stop_price(nxt, allow_be=False), tok)
            else:
                key = "trigger" if tok in _TRIGGER else "cap"
                once(key, _price(nxt, {"trigger": "stop entry", "cap": "limit"}[key]), tok)
            i += 2
            continue
        if tok in _URGENT:
            once("urgent", True, tok)
        elif tok in ("adn", "alg"):
            once("adn", True, tok)
        elif tok == "mkt":
            once("mkt", True, tok)
        elif tok == "orb":
            minutes = ORB_MINUTES
            if nxt is not None and nxt.isdigit():
                minutes = int(nxt)
                if not 1 <= minutes <= 60:
                    raise CommandError("the opening range is 1 to 60 minutes")
                i += 1
            once("orb", minutes, tok)
        else:
            raise CommandError(f"'{tokens[i]}' isn't one of the options; {usage}")
        i += 1

    stop, trigger, cap = seen.get("stop"), seen.get("trigger"), seen.get("cap")
    urgent, mkt, orb = bool(seen.get("urgent")), bool(seen.get("mkt")), seen.get("orb")
    loss = seen.get("loss")
    if qty is not None and loss is not None:
        raise CommandError("a share count and a loss both set the size -- give one")
    if orb is not None and (qty is not None or stop is not None or trigger is not None):
        raise CommandError("orb sets the size, entry and stop itself -- only lim and loss go with it")
    if mkt and (cap is not None or trigger is not None or urgent):
        raise CommandError("mkt is a plain market order -- no limit, stop entry or urgency")
    if urgent and trigger is not None:
        raise CommandError("a stop entry fills at the market once triggered -- urgent is for Adaptive orders")
    if urgent and orb is not None:
        raise CommandError("an ORB entry is a stop entry -- urgent is for Adaptive orders")
    if qty is None and orb is None and stop is None:
        raise CommandError("give the protective stop ('stop PRICE') for astor to size from, or a share count")

    if orb is not None or trigger is not None:
        order_type = "STP LMT" if cap is not None else "STP"
        price, cap_ = (trigger, cap) if cap is not None else (trigger, None)
    elif cap is not None:
        order_type, price, cap_ = "LMT", cap, None
    elif mkt:
        order_type, price, cap_ = "MKT", None, None
    else:
        order_type, price, cap_ = ("ADU" if urgent else "ADN"), None, None
    defaulted = not (trigger is not None or cap is not None or mkt or urgent or seen.get("adn") or orb is not None)
    return EnterRequest(
        symbol, account, direction, qty or 0, order_type, price, stop, defaulted,
        orb_minutes=orb, cap=cap_, urgent=urgent, max_loss=loss,
    )


def _dollars(tok: str) -> float:
    try:
        v = float(tok.lstrip("$").replace(",", ""))
    except ValueError:
        raise CommandError(f"loss '{tok}' is not a dollar amount") from None
    if v <= 0:
        raise CommandError("the loss must be above $0")
    return v


@dataclass(frozen=True)
class StopRequest:
    symbol: str
    account: str | None
    # (part, price): part is a Fraction of the position or an int share
    # count; price a number or 'be'/'lod'/'hod', which live resolves
    tranches: list[tuple[Fraction | int, StopPrice]]
    # 'stop SYM be': every stop short of breakeven goes to it
    breakeven: bool = False


STOP_USAGE = (
    "stop SYM [ACCOUNT] PRICE|be|lod|hod, or stop SYM [ACCOUNT] PART PRICE PART PRICE ..."
    "  (PART: 30%, 0.3, 3/10 or a share count; PRICE: a price, be, lod or hod)"
)


def parse_share(tok: str) -> Fraction | None:
    """A share of the position written as 30%, 0.3 or 3/10 -- the same
    three ways in `stop` and `trail` -- or None if `tok` isn't one."""
    t = tok.strip()
    try:
        if t.endswith("%"):
            f = Fraction(t[:-1]) / 100
        elif "/" in t or "." in t:
            f = Fraction(t)
        else:
            return None
    except (ValueError, ZeroDivisionError):
        return None
    if not 0 < f <= 1:
        raise CommandError(f"part '{tok}' must be above 0 and at most all of it")
    return f


def _part(tok: str) -> Fraction | int:
    """A tranche's size: a share of the position (parse_share) or a whole
    number of shares."""
    if (f := parse_share(tok)) is not None:
        return f
    try:
        n = int(tok)
    except ValueError:
        raise CommandError(
            f"part '{tok}' is not a share (30%, 0.3, 3/10) or a share count"
        ) from None
    if n <= 0:
        raise CommandError(f"part '{tok}' must be positive")
    return n


# Stop prices given as a level astor looks up: breakeven, or today's
# regular-hours low or high -- exactly, not a tick beyond, like the ORB stop.
STOP_WORDS = ("be", "lod", "hod")
StopPrice = float | str


def _stop_price(tok: str, what: str = "stop", allow_be: bool = True) -> StopPrice:
    t = tok.lower()
    if t in STOP_WORDS:
        if t == "be" and not allow_be:
            raise CommandError("an entry has no breakeven yet -- give a price, lod or hod")
        return t
    return _price(tok, what)


def parse_stop(rest: str) -> StopRequest:
    tokens = rest.split()
    if not tokens:
        raise CommandError(STOP_USAGE)
    symbol = _symbol(tokens[0])
    account, tokens = _split_account(tokens[1:])
    if len(tokens) == 1 and tokens[0].lower() == "be":
        return StopRequest(symbol, account, [], breakeven=True)
    if len(tokens) == 1:
        return StopRequest(symbol, account, [(Fraction(1), _stop_price(tokens[0]))])
    if not tokens or len(tokens) % 2:
        raise CommandError(STOP_USAGE)
    tranches = [
        (_part(tokens[i]), _stop_price(tokens[i + 1])) for i in range(0, len(tokens), 2)
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


EXIT_USAGE = "exit SYM [ACCOUNT] [SHARES | 1/3] [adn | adu | mkt | lmt|lim|limit PRICE]  (default: all of it, adn)"


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
        raise CommandError(f"exit order '{tokens[0]}' is not adn/adu/mkt/lmt/lim/limit; {EXIT_USAGE}")
    if order_type == "LMT":
        if len(tokens) != 2:
            raise CommandError("a limit exit needs a price: 'exit SYM [SHARES] lim PRICE'")
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
    if req.order_type == "STP LMT" and req.cap is not None and req.price is not None:
        if (req.cap < req.price) if long else (req.cap > req.price):
            problems.append(
                f"the limit {req.cap:.2f} is {'below' if long else 'above'} the stop entry "
                f"{req.price:.2f} -- it could never fill"
            )
    if req.order_type in ("STP", "STP LMT") and market is not None and req.price is not None:
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
    ref = req.price if req.price is not None else market
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
