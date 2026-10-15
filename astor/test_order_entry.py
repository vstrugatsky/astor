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
    check("alg no price", (r.order_type, r.price, r.stop), ("ALG", None, 99.5))


def test_enter_malformed() -> None:
    raises("no price for stp", parse_enter, "SPCX s 20 stp stop 172")
    raises("price for mkt", parse_enter, "SPCX s 20 mkt 170")
    raises("bad direction", parse_enter, "SPCX x 20 mkt")
    raises("fractional qty", parse_enter, "SPCX s 2.5 mkt")
    raises("split stop not on enter", parse_enter, "SPCX s 20 mkt stop 1/2 172 1/2 173")
    r = parse_enter("SPCX s 20")
    check("no type -> Adaptive", (r.order_type, r.stop), ("ALG", None))
    r = parse_enter("SPCX s 20 stop 172")
    check("no type, with stop", (r.order_type, r.stop), ("ALG", 172.0))
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
    check("exit default alg", parse_exit("SPCX").order_type, "ALG")
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
          ("NVDA", "long", 100, "ALG", 35.99))
    r = parse_enter("SPCX reg 20 stp 170.10 stop 172.01", "short")
    check("sell: account, type, stop", (r.account, r.direction, r.order_type, r.price, r.stop),
          ("reg", "short", "STP", 170.10, 172.01))
    raises("sell takes no direction", parse_enter, "SPCX s 20", "short")
    raises("needs a count", parse_enter, "SPCX", "long")


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> None:
    for fn in TESTS:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
