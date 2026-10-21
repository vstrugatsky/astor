"""Order-command parsing and pre-send checks, on hand-built input.

uv run python -m astor.test_order_entry
"""

from __future__ import annotations

from fractions import Fraction

from astor.core.order_entry import (
    CommandError,
    EnterRequest,
    entry_problems,
    parse_cancel,
    parse_enter,
    parse_exit,
    parse_stop,
    resolve_account,
    round_to_tick,
    stop_problems,
    tranche_sizes,
)

MANAGED = ["U1234566", "U7654329"]


def check(label: str, got, want) -> None:
    assert got == want, f"{label}: expected {want}, got {got}"


def raises(label: str, fn, *args) -> None:
    try:
        fn(*args)
    except CommandError:
        return
    raise AssertionError(f"{label}: expected CommandError")


def test_enter_the_example() -> None:
    r = parse_enter("SPCX short 20 stp 170.10 stop 172.01")
    check("parsed", r, EnterRequest("SPCX", None, "short", 20, "STP", 170.10, 172.01))
    check("action", r.action, "SELL")


def test_enter_case_and_account() -> None:
    r = parse_enter("spcx REG s 20 LMT 170.1")
    check("symbol", r.symbol, "SPCX")
    check("alias lowered", r.account, "reg")
    check("no stop", r.stop, None)
    r = parse_enter("nvda u1234566 l 5 alg stop 99.5")
    check("id uppered", r.account, "U1234566")
    check("alg (old name) is adn", (r.order_type, r.price, r.stop), ("ADN", None, 99.5))


def test_enter_malformed() -> None:
    raises("no price for stp", parse_enter, "SPCX s 20 stp stop 172")
    raises("price for mkt", parse_enter, "SPCX s 20 mkt 170")
    raises("bad direction", parse_enter, "SPCX x 20 mkt")
    raises("fractional qty", parse_enter, "SPCX s 2.5 mkt")
    raises("split stop not on enter", parse_enter, "SPCX s 20 mkt stop 1/2 172 1/2 173")
    r = parse_enter("SPCX s 20")
    check("no type -> Adaptive", (r.order_type, r.stop), ("ADN", None))
    r = parse_enter("SPCX s 20 stop 172")
    check("no type, with stop", (r.order_type, r.stop), ("ADN", 172.0))
    raises("mistyped type", parse_enter, "SPCX s 20 lmit 170")


def test_price_rounds_to_tick() -> None:
    check("cents", round_to_tick(170.104), 170.1)
    check("sub-dollar", round_to_tick(0.12347), 0.1235)
    check("enter rounds", parse_enter("A l 1 lmt 10.006").price, 10.01)


def test_stop_single_and_split() -> None:
    r = parse_stop("SPCX 170.10")
    check("single", r.tranches, [(Fraction(1), 170.10)])
    r = parse_stop("spcx reg 1/2 168.05 1/2 170.01")
    check("account", r.account, "reg")
    check("split", r.tranches, [(Fraction(1, 2), 168.05), (Fraction(1, 2), 170.01)])
    r = parse_stop("SPCX 5 168 15 170")
    check("shares", [p for p, _ in r.tranches], [5, 15])
    raises("dangling part", parse_stop, "SPCX 1/2 168 1/2")
    raises("zero fraction", parse_stop, "SPCX 0/2 168 1 170")


def test_tranche_sizes() -> None:
    half = Fraction(1, 2)
    check("even", tranche_sizes([half, half], 20), [10, 10])
    check("odd: remainder last", tranche_sizes([half, half], 21), [10, 11])
    check("thirds", tranche_sizes([Fraction(1, 3)] * 3, 10), [3, 3, 4])
    check("mixed", tranche_sizes([5, half], 10), [5, 5])
    check("whole", tranche_sizes([Fraction(1)], 7), [7])
    raises("short of position", tranche_sizes, [half], 20)
    raises("shares over", tranche_sizes, [15, 10], 20)
    raises("rounds to nothing", tranche_sizes, [half, half], 1)


def test_cancel_and_exit() -> None:
    check("cancel", parse_cancel("spcx ira"), parse_cancel("SPCX IRA"))
    check("exit default adn", parse_exit("SPCX").order_type, "ADN")
    r = parse_exit("spcx reg lmt 168.2")
    check("exit lmt", (r.account, r.order_type, r.price), ("reg", "LMT", 168.2))
    check("exit mkt", parse_exit("SPCX mkt").order_type, "MKT")
    raises("lmt without price", parse_exit, "SPCX lmt")
    raises("mkt with price", parse_exit, "SPCX mkt 5")
    raises("stp exit", parse_exit, "SPCX stp 5")


def test_resolve_account() -> None:
    check("ira", resolve_account("ira", MANAGED), "U1234566")
    check("reg", resolve_account("reg", MANAGED), "U7654329")
    check("id any case", resolve_account("U7654329", MANAGED), "U7654329")
    check("none", resolve_account(None, MANAGED), None)
    raises("unknown id", resolve_account, "U000", MANAGED)
    raises("alias ambiguous", resolve_account, "reg", ["U19", "U29"])


def test_entry_problems() -> None:
    sell_stp = parse_enter("SPCX s 20 stp 170.10 stop 172.01")
    check("good short stp", entry_problems(sell_stp, 171.0, 0), [])
    check("no market, price-free checks only", entry_problems(sell_stp, None, 0), [])
    assert entry_problems(sell_stp, 169.0, 0), "sell stop above market fires at once"
    assert entry_problems(sell_stp, 171.0, 10), "would cover a long, not open a short"
    check("adding to a short is fine", entry_problems(sell_stp, 171.0, -10), [])

    bad_stop = parse_enter("SPCX s 20 lmt 170 stop 169")
    assert entry_problems(bad_stop, None, 0), "short stop below entry"
    mkt_long = parse_enter("NVDA l 5 mkt stop 99")
    check("long mkt ok", entry_problems(mkt_long, 100.0, 0), [])
    assert entry_problems(mkt_long, 98.0, 0), "long stop above the market"


def test_stop_problems() -> None:
    check("long below", stop_problems([95, 97], True, 100.0), [])
    check("one wrong", len(stop_problems([95, 101], True, 100.0)), 1)
    check("short above", stop_problems([105], False, 100.0), [])
    check("no market", stop_problems([1], False, None), [])


def test_buy_sell_put_the_direction_in_the_verb() -> None:
    r = parse_enter("nvda 100 stop 35.99", "long")
    check("buy: compact", (r.symbol, r.direction, r.qty, r.order_type, r.stop),
          ("NVDA", "long", 100, "ADN", 35.99))
    r = parse_enter("SPCX reg 20 stp 170.10 stop 172.01", "short")
    check("sell: account, type, stop", (r.account, r.direction, r.order_type, r.price, r.stop),
          ("reg", "short", "STP", 170.10, 172.01))
    raises("sell takes no direction", parse_enter, "SPCX s 20", "short")
    raises("needs a count", parse_enter, "SPCX", "long")


def test_shortable_file_parses() -> None:
    from astor.drivers.shortable import ShortableList, parse

    text = (
        "#BOF|2026.10.15|22:14:18\n"
        "#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|\n"
        "CCXI|USD|CHURCHILL CAPITAL CORP-CL A|852646546|X|1.9785|1.9015|4300000|B|\n"
        "NVDA|USD|NVIDIA CORP|4815747|X|3.6300|0.2500|>10000000|B|\n"
        "3LI|EUR|LONGWEI PETROLEUM|77577663|X|2.0390|0.4000|500||\n"
        "TINY|USD|TINY CORP|1|X|-40.0|45.0|300|B|\n"
    )
    as_of, available = parse(text)
    check("as of", f"{as_of:%Y-%m-%d %H:%M}", "2026-10-15 22:14")
    check("counts", available, {"CCXI": 4_300_000, "NVDA": 10_000_001, "TINY": 300})
    lst = ShortableList()
    check("unread: unknown", lst.available("BSP"), None)
    lst.as_of, lst._available = as_of, available
    check("missing: none to borrow", lst.available("BSP"), 0)
    check("listed", lst.available("TINY"), 300)


def test_adaptive_priorities() -> None:
    from astor.drivers.orders import entry_orders, exit_order

    check("adu entry", parse_enter("NVDA 100 adu stop 35.99", "long").order_type, "ADU")
    check("adn entry", parse_enter("NVDA l 100 ADN").order_type, "ADN")
    check("adu exit", parse_exit("NVDA reg adu").order_type, "ADU")
    raises("adu takes no price", parse_enter, "NVDA 100 adu 36.10", "long")
    for typ, priority in (("ADN", "Normal"), ("ADU", "Urgent")):
        parent, _ = entry_orders(parse_enter(f"NVDA 100 {typ} stop 35.99", "long"), "U1234566")
        check(f"{typ} entry is Adaptive {priority}",
              (parent.orderType, parent.algoStrategy, parent.algoParams[0].value),
              ("MKT", "Adaptive", priority))
        order = exit_order("SELL", 100, "U1234566", typ)
        check(f"{typ} exit is Adaptive {priority}", order.algoParams[0].value, priority)
    check("trail's override", exit_order("SELL", 1, "U1234566", "ADN", priority="Urgent").algoParams[0].value, "Urgent")


def test_lmt_entry_works_outside_regular_hours() -> None:
    from astor.drivers.orders import entry_orders

    parent, child = entry_orders(parse_enter("SPCX 20 lmt 170.10 stop 172.01", "short"), "U7654329")
    check("LMT entry works pre-market", parent.outsideRth, True)
    check("its stop waits for regular hours", child.outsideRth, False)
    parent, _ = entry_orders(parse_enter("SPCX 20 stp 170.10", "short"), "U7654329")
    check("STP entry waits for the open", parent.outsideRth, False)


def test_untyped_entry_is_marked() -> None:
    check("no type: defaulted", parse_enter("NVDA 100 stop 35.99", "long").type_defaulted, True)
    check("adn typed: not", parse_enter("NVDA 100 adn stop 35.99", "long").type_defaulted, False)
    check("lmt typed: not", parse_enter("NVDA 100 lmt 36 stop 35.99", "long").type_defaulted, False)


def _entry_driver(market: float, bid: float, ask: float, in_rth: bool = True):
    """A LiveDriver whose IBKR calls are stubbed: one quote, nothing held."""
    from types import SimpleNamespace as NS

    from astor.drivers.live import LiveDriver
    from astor.drivers.shortable import ShortableList

    d = LiveDriver.__new__(LiveDriver)
    d.ib = NS(managedAccounts=lambda: ["U1234566", "U7654329"], positions=lambda *a: [])
    d.shortable = ShortableList()
    quote = NS(bid=bid, ask=ask, last=market, marketPrice=lambda: market, midpoint=lambda: (bid + ask) / 2)

    async def contract(sym):
        return NS(symbol=sym)

    async def rth(sym):
        return in_rth

    async def snap(c):
        return quote

    d._contract, d._in_rth, d._snapshot, d._quote = contract, rth, snap, snap
    return d


def test_a_stop_entry_already_through_goes_as_adaptive() -> None:
    import asyncio

    req = parse_enter("ARQT 200 stp 30.35 stop 30.50", "short")
    t = asyncio.run(_entry_driver(30.31, 30.30, 30.32).prepare_entry(req))
    check("not refused", isinstance(t, str), False)
    check("sent as Adaptive", (t.req.order_type, t.parent.algoStrategy), ("ADN", "Adaptive"))
    check("and says why", "already through the sell stop 30.35" in t.summary, True)
    check("stop kept", t.child.auxPrice, 30.50)

    t = asyncio.run(_entry_driver(30.31, 30.30, 30.32, in_rth=False).prepare_entry(req))
    check("outside hours: a limit at the bid", (t.req.order_type, t.req.price), ("LMT", 30.30))

    t = asyncio.run(_entry_driver(30.40, 30.39, 30.41).prepare_entry(req))
    check("not yet through: the stop entry stands", (t.req.order_type, t.req.price), ("STP", 30.35))


def test_a_desktop_order_is_named() -> None:
    from types import SimpleNamespace as NS

    from astor.drivers.orders import unbound_problem

    def stop(qty, price, oid):
        return NS(order=NS(orderId=oid, orderType="STP", action="SELL", totalQuantity=qty, auxPrice=price))

    check("astor's own: fine", unbound_problem([stop(4, 536.52, 2410)]), None)
    check(
        "a Desktop stop, named",
        unbound_problem([stop(4, 536.52, 2410), stop(8, 535.0, 0)]),
        "the 8@535.00 stop was placed in IBKR Desktop, so astor can't change it -- "
        "cancel it there, or set all of this position's orders here",
    )
    two = unbound_problem([stop(4, 535.0, 0), stop(4, 534.0, 0)])
    check("two of them", two.startswith("these 4@535.00 stop, 4@534.00 stop were placed"), True)


def test_stop_to_breakeven() -> None:
    import asyncio
    from types import SimpleNamespace as NS

    from astor.core.tracking import SellRuleTracker, TrailTracker
    from astor.drivers.live import LiveDriver
    from astor.test_tracking import _executor_holding

    r = parse_stop("noc ira BE")
    check("parsed", (r.symbol, r.account, r.breakeven, r.tranches), ("NOC", "ira", True, []))

    # Long 12 NOC at 536.52, stops 4@542.62 (past breakeven) and 8@529.00
    ex, _ = _executor_holding(12, [(4, 542.62), (8, 529.0)])
    d = LiveDriver.__new__(LiveDriver)
    d.tracker, d.trailer, d.executor, d.desk = SellRuleTracker(), TrailTracker(), ex, NS(dry_run=True)
    d.ib = NS(positions=lambda *a: [NS(contract=NS(symbol="XYZ"), account="U1", position=12, avgCost=536.6033)])
    store = NS(record_alert=lambda *a, **k: None, commit=lambda: None)
    out = asyncio.run(d._stop_to_breakeven("XYZ", "U1", "run", store))
    check("only the loose tranche moves, to avgCost rounded up", "move stop 529.00 -> 536.61" in out, True)
    d.trailer.track("XYZ", 536.52, 529.0, "U1")
    out = asyncio.run(d._stop_to_breakeven("XYZ", "U1", "run", store))
    check("astor's own entry when it has the trade", "move stop 529.00 -> 536.52" in out, True)


def test_exit_part_of_the_position() -> None:
    from fractions import Fraction
    from types import SimpleNamespace as NS

    from astor.core.tracking import SellRuleTracker, TrailTracker
    from astor.drivers.live import LiveDriver
    from astor.test_tracking import _live_executor

    check("shares", (parse_exit("NOC 4").qty, parse_exit("NOC 4").order_type), (4, "ADN"))
    check("a fraction, adu", (parse_exit("noc ira 1/3 adu").qty, parse_exit("noc ira 1/3 adu").order_type),
          (Fraction(1, 3), "ADU"))
    check("lmt", (parse_exit("NOC 4 lmt 540").qty, parse_exit("NOC 4 lmt 540").price), (4, 540.0))
    check("all of it, as before", parse_exit("NOC").qty, None)

    ex, trades, sent, state, run = _live_executor(100, [(50, 97.0), (50, 98.0)])
    ex.ib.managedAccounts = lambda: ["U1"]
    ex.ib.positions = lambda *a: [NS(contract=NS(symbol="XYZ"), account="U1", position=state["held"])]
    d = LiveDriver.__new__(LiveDriver)
    d.ib, d.desk = ex.ib, ex
    d.tracker, d.trailer = SellRuleTracker(), TrailTracker()
    d.trailer.track("XYZ", 100.0, 98.0, "U1")

    async def rth(sym):
        return True

    d._in_rth = rth
    store = NS(record_alert=lambda *a, **k: None, commit=lambda: None)
    out = run(d.exit_position(parse_exit("XYZ 35"), "run", store))
    check("35 sold", [o.totalQuantity for o in sent], [35])
    check("stops cover the 65 left", sum(t.order.totalQuantity for t in trades), 65)
    check("trail keeps the rest", ("XYZ", "U1") in d.trailer.trades, True)
    check("and says what it did", "SELL 35" in out, True)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
