"""How `track` and `trail` take a trade out: which rules, for what share of
the position, against what R, and what happens at the close.

    trail SYM [ENTRY STOP] [ACCOUNT] [1.5R] [PCT RULE ...] [close 2.5R | close exit]
    track SYM ...   (the same; track notifies where trail sells)

    1.5R      R is 1.5 x |entry - stop| for every rule measured in R: the
              +1R breakeven and the giveback limits. For a stop set tight.
    35% gb    the giveback rule -- trail's tiered limit, track's 0.5R off
              the peak once armed at +1R.
    35% avwap a close 0.05 x ADR(1) past AVWAP against the trade, once a
              close has been on the trade's side of it. avwap0.1 sets the
              distance.
    30% be    no rule of its own: these shares ride the stop, which goes to
              breakeven at +1R like every stop does.
    close 2.5R  at the close, sell what's left if it is up less than 2.5R
              (the default is 2R); trail sells, track says to.
    close exit  sell what's left at the close whatever the gain. A bare
              `close` is refused: which of the two it meant is a guess.

Each rule fires once, on its own, in whatever order the market brings
them. Percentages short of 100 leave the rest to `be`. No rules at all is
the original behavior: 100% gb.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from astor.core.order_entry import (
    CommandError,
    _price,
    account_token,
    is_account,
    parse_share,
)

Rule = Literal["gb", "avwap", "be", "level"]
RULES: tuple[Rule, ...] = ("gb", "avwap", "be", "level")
DEFAULT_AVWAP_ADR = 0.05
# At the close, what's left goes unless it is up at least this many R.
DEFAULT_CLOSE_HOLD_R = 2.0

_R = re.compile(r"^(\d+(?:\.\d+)?)r$", re.IGNORECASE)
_AVWAP = re.compile(r"^avwap(\d*\.?\d+)?$", re.IGNORECASE)


@dataclass(frozen=True)
class Leg:
    rule: Rule
    pct: float
    avwap_adr: float = DEFAULT_AVWAP_ADR
    # 'level': a price on the chart -- these shares sell on a 1-min close
    # beyond it -- given as a price, or as 'lod'/'hod' until live looks up
    # today's low or high when the trail starts
    level: float | None = None
    level_word: str | None = None

    @property
    def key(self) -> str:
        """What tells this leg apart: its rule, or for a level its price."""
        if self.rule != "level":
            return self.rule
        return f"@{self.level:g}" if self.level is not None else f"@{self.level_word}"

    def label(self) -> str:
        rule = self.rule
        if rule == "avwap" and self.avwap_adr != DEFAULT_AVWAP_ADR:
            rule = f"avwap{self.avwap_adr:g}"
        if rule == "level":
            rule = f"{self.level:.2f}" if self.level is not None else str(self.level_word)
            if self.level is not None and self.level_word:
                rule = f"{self.level_word} {self.level:.2f}"
        return f"{round(self.pct, 1):g}% {rule}"


@dataclass(frozen=True)
class ExitPlan:
    legs: tuple[Leg, ...] = (Leg("gb", 100.0),)
    r_mult: float = 1.0
    # Held through the close at this gain or more; None sells it anyway.
    close_hold_r: float | None = DEFAULT_CLOSE_HOLD_R

    def leg(self, rule: Rule) -> Leg | None:
        return next((g for g in self.legs if g.rule == rule), None)

    def leg_by_key(self, key: str) -> Leg | None:
        return next((g for g in self.legs if g.key == key), None)

    @property
    def is_default(self) -> bool:
        return self == ExitPlan()

    def describe(self) -> str:
        parts = [", ".join(g.label() for g in self.legs)]
        if self.r_mult != 1.0:
            parts.insert(0, f"{self.r_mult:g}R")
        if self.close_hold_r is None:
            parts.append("close exit")
        elif self.close_hold_r != DEFAULT_CLOSE_HOLD_R:
            parts.append(f"close {self.close_hold_r:g}R")
        return "; ".join(parts)


def allocate(legs: tuple[Leg, ...], qty: int) -> dict[str, int]:
    """Whole shares per leg, adding up to `qty`: each its floor, the shares
    left over to the largest fractions -- the earliest leg on a tie."""
    exact = [g.pct / 100.0 * qty for g in legs]
    shares = [int(x) for x in exact]
    by_fraction = sorted(range(len(legs)), key=lambda i: (-(exact[i] - shares[i]), i))
    for i in by_fraction[: qty - sum(shares)]:
        shares[i] += 1
    return {g.key: n for g, n in zip(legs, shares, strict=True)}


@dataclass
class TradeCommand:
    symbol: str
    entry: float | None = None
    stop: float | None = None
    account: str | None = None
    plan: ExitPlan = field(default_factory=ExitPlan)


def _rule(tok: str) -> tuple[Rule, float] | None:
    t = tok.lower()
    if t in ("gb", "be"):
        return t, DEFAULT_AVWAP_ADR  # type: ignore[return-value]
    m = _AVWAP.match(t)
    if m:
        return "avwap", float(m.group(1)) if m.group(1) else DEFAULT_AVWAP_ADR
    return None


def _leg(tok: str, pct: float) -> Leg | None:
    """The rule a share is given to: gb, avwap, be, or a level -- a price,
    lod or hod."""
    if (rule := _rule(tok)) is not None:
        return Leg(rule[0], pct, rule[1])
    if tok.lower() in ("lod", "hod"):
        return Leg("level", pct, level_word=tok.lower())
    try:
        float(tok)
    except ValueError:
        return None
    return Leg("level", pct, level=_price(tok, "level"))


def _share(tok: str) -> float | None:
    """A share of the position, as a percent: 30%, 0.3 or 3/10. A decimal
    above 1 is a price, not a share."""
    t = tok.strip()
    if "." in t and not t.endswith("%") and "/" not in t:
        try:
            if float(t) > 1:
                return None
        except ValueError:
            return None
    f = parse_share(t)
    return float(f * 100) if f is not None else None


def parse_trade_command(rest: str, verb: str = "trail") -> TradeCommand:
    usage = (
        f"{verb} SYM [ENTRY STOP] [ACCOUNT] [1.5R] [SHARE RULE ...] [close 2.5R|close exit]"
        f"  (SHARE: 30%, 0.3, 3/10; RULE: gb, avwap, be, a price, lod, hod)"
        f"  e.g. '{verb} XYZ 1.5R 25% gb 0.25 75.59 1/4 avwap 25% be close exit'"
    )
    tokens = rest.split()
    if not tokens:
        raise CommandError(usage)
    cmd = TradeCommand(tokens[0].upper().replace(".", " "))
    prices: list[float] = []
    legs: list[Leg] = []
    r_mult, close_hold_r = 1.0, DEFAULT_CLOSE_HOLD_R
    pending: tuple[str, float] | None = None  # (as typed, percent)

    rest_tokens = tokens[1:]
    i = 0
    while i < len(rest_tokens):
        tok = rest_tokens[i]
        i += 1
        if pending is not None:
            leg = _leg(tok, pending[1])
            if leg is None:
                raise CommandError(
                    f"'{pending[0]}' needs a rule after it (gb, avwap, be, a price, lod, hod), not '{tok}'"
                )
            legs.append(leg)
            pending = None
            continue
        if tok.lower() == "close":
            # The token right after `close` is its own: 'exit' or an R.
            nxt = rest_tokens[i].lower() if i < len(rest_tokens) else ""
            if nxt == "exit":
                close_hold_r, i = None, i + 1
            elif (m := _R.match(nxt)) is not None:
                close_hold_r, i = float(m.group(1)), i + 1
            else:
                raise CommandError(
                    "'close' needs 'exit' (sell at the close whatever the gain) or an R "
                    "('close 2.5R': sell if up less than 2.5R); the default is 2R"
                )
            continue
        if (pct := _share(tok)) is not None:
            pending = (tok, pct)
        elif (m := _R.match(tok)) is not None:
            r_mult = float(m.group(1))
            if r_mult <= 0:
                raise CommandError(f"'{tok}': R must be above 0")
        elif (rule := _rule(tok)) is not None:
            # a rule with no share: the whole position
            legs.append(Leg(rule[0], 100.0, rule[1]))
        elif tok.lower() in ("lod", "hod"):
            legs.append(Leg("level", 100.0, level_word=tok.lower()))
        elif is_account(tok):
            if cmd.account is not None:
                raise CommandError(f"two accounts given ('{cmd.account}', '{tok}')")
            cmd.account = account_token(tok)
        else:
            try:
                prices.append(float(tok))
            except ValueError:
                raise CommandError(f"'{tok}' isn't a price, account, R, share or rule; {usage}") from None
    if pending is not None:
        raise CommandError(f"'{pending[0]}' needs a rule after it (gb, avwap, be, a price, lod, hod)")
    if len(prices) not in (0, 2):
        raise CommandError(f"give both ENTRY and STOP, or neither; {usage}")
    if prices:
        cmd.entry, cmd.stop = prices

    keys = [g.key for g in legs]
    if len(set(keys)) != len(keys):
        raise CommandError("each rule once -- give it its whole share in one place")
    total = sum(g.pct for g in legs)
    if total > 100.0 + 1e-6:
        raise CommandError(f"the shares add up to {total:g}%, over 100%")
    if legs and total < 100.0 - 1e-6:
        # The rest rides the stop.
        be = next((g for g in legs if g.rule == "be"), None)
        rest_pct = 100.0 - total
        if be is None:
            legs.append(Leg("be", rest_pct))
        else:
            legs[legs.index(be)] = Leg("be", be.pct + rest_pct)
    cmd.plan = ExitPlan(tuple(legs) if legs else ExitPlan().legs, r_mult, close_hold_r)
    return cmd
