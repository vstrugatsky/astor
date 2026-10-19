"""How `track` and `trail` take a trade out: which rules, for what share of
the position, against what R, and what happens at the close.

    trail SYM [ENTRY STOP] [ACCOUNT] [1.5R] [PCT RULE ...] [close]
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
    close     whatever is left goes at the close -- trail even at +2R and
              beyond, track as a notice. Without it trail keeps its 2R rule.

Each rule fires once, on its own, in whatever order the market brings
them. Percentages short of 100 leave the rest to `be`. No rules at all is
the original behavior: 100% gb.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from astor.core.order_entry import CommandError, account_token, is_account

Rule = Literal["gb", "avwap", "be"]
RULES: tuple[Rule, ...] = ("gb", "avwap", "be")
DEFAULT_AVWAP_ADR = 0.05

_R = re.compile(r"^(\d+(?:\.\d+)?)r$", re.IGNORECASE)
_PCT = re.compile(r"^(\d+(?:\.\d+)?)%$")
_AVWAP = re.compile(r"^avwap(\d*\.?\d+)?$", re.IGNORECASE)


@dataclass(frozen=True)
class Leg:
    rule: Rule
    pct: float
    avwap_adr: float = DEFAULT_AVWAP_ADR

    def label(self) -> str:
        rule = self.rule
        if rule == "avwap" and self.avwap_adr != DEFAULT_AVWAP_ADR:
            rule = f"avwap{self.avwap_adr:g}"
        return f"{self.pct:g}% {rule}"


@dataclass(frozen=True)
class ExitPlan:
    legs: tuple[Leg, ...] = (Leg("gb", 100.0),)
    r_mult: float = 1.0
    close: bool = False

    def leg(self, rule: Rule) -> Leg | None:
        return next((g for g in self.legs if g.rule == rule), None)

    @property
    def is_default(self) -> bool:
        return self == ExitPlan()

    def describe(self) -> str:
        parts = [", ".join(g.label() for g in self.legs)]
        if self.r_mult != 1.0:
            parts.insert(0, f"{self.r_mult:g}R")
        if self.close:
            parts.append("close")
        return "; ".join(parts)


def allocate(legs: tuple[Leg, ...], qty: int) -> dict[Rule, int]:
    """Whole shares per leg, adding up to `qty`: each its floor, the shares
    left over to the largest fractions -- the earliest leg on a tie."""
    exact = [g.pct / 100.0 * qty for g in legs]
    shares = [int(x) for x in exact]
    by_fraction = sorted(range(len(legs)), key=lambda i: (-(exact[i] - shares[i]), i))
    for i in by_fraction[: qty - sum(shares)]:
        shares[i] += 1
    return {g.rule: n for g, n in zip(legs, shares, strict=True)}


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


def parse_trade_command(rest: str, verb: str = "trail") -> TradeCommand:
    usage = (
        f"{verb} SYM [ENTRY STOP] [ACCOUNT] [1.5R] [PCT gb|avwap|be ...] [close]"
        f"  e.g. '{verb} XYZ 1.5R 35% gb 35% avwap 30% be close'"
    )
    tokens = rest.split()
    if not tokens:
        raise CommandError(usage)
    cmd = TradeCommand(tokens[0].upper().replace(".", " "))
    prices: list[float] = []
    legs: list[Leg] = []
    r_mult, close = 1.0, False
    pending_pct: float | None = None

    for tok in tokens[1:]:
        if pending_pct is not None:
            rule = _rule(tok)
            if rule is None:
                raise CommandError(f"'{pending_pct:g}%' needs a rule after it (gb, avwap, be), not '{tok}'")
            legs.append(Leg(rule[0], pending_pct, rule[1]))
            pending_pct = None
        elif (m := _PCT.match(tok)) is not None:
            pending_pct = float(m.group(1))
            if pending_pct <= 0:
                raise CommandError(f"'{tok}': a share of the position must be above 0%")
        elif (m := _R.match(tok)) is not None:
            r_mult = float(m.group(1))
            if r_mult <= 0:
                raise CommandError(f"'{tok}': R must be above 0")
        elif tok.lower() == "close":
            close = True
        elif (rule := _rule(tok)) is not None:
            # a rule with no percent: the whole position
            legs.append(Leg(rule[0], 100.0, rule[1]))
        elif is_account(tok):
            if cmd.account is not None:
                raise CommandError(f"two accounts given ('{cmd.account}', '{tok}')")
            cmd.account = account_token(tok)
        else:
            try:
                prices.append(float(tok))
            except ValueError:
                raise CommandError(f"'{tok}' isn't a price, account, R, share or rule; {usage}") from None
    if pending_pct is not None:
        raise CommandError(f"'{pending_pct:g}%' needs a rule after it (gb, avwap, be)")
    if len(prices) not in (0, 2):
        raise CommandError(f"give both ENTRY and STOP, or neither; {usage}")
    if prices:
        cmd.entry, cmd.stop = prices

    rules = [g.rule for g in legs]
    if len(set(rules)) != len(rules):
        raise CommandError("each rule once -- give it its whole share in one place")
    total = sum(g.pct for g in legs)
    if total > 100.0 + 1e-9:
        raise CommandError(f"the shares add up to {total:g}%, over 100%")
    if legs and total < 100.0 - 1e-9:
        # The rest rides the stop.
        be = next((g for g in legs if g.rule == "be"), None)
        rest_pct = 100.0 - total
        if be is None:
            legs.append(Leg("be", rest_pct))
        else:
            legs[legs.index(be)] = Leg("be", be.pct + rest_pct)
    cmd.plan = ExitPlan(tuple(legs) if legs else ExitPlan().legs, r_mult, close)
    return cmd
