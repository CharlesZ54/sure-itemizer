from datetime import date

import pytest

from itemizer.matcher import match
from itemizer.models import Line, Purchase, SureTxn
from itemizer.money import allocate, cents_to_str, to_cents
from itemizer.planner import plan


def txn(id="t1", d=date(2026, 10, 9), cents=5000, name="Amazon - AMAZON MKTPL*EXAMPLE01", notes=None):
    return SureTxn(id=id, date=d, amount_cents=cents, name=name, notes=notes,
                   account_id="acc", account_name="Example Rewards Card (0000)", category=None)


def purchase(id="p1", d=date(2026, 10, 8), cents=5000, lines=None, problem=None):
    return Purchase(source="amazon", external_id=id, date=d, amount_cents=cents,
                    lines=lines or [], reference="112-0000000-0000000", problem=problem)


class TestMoney:
    def test_parts_sum_exactly(self):
        for total in (1, 100, 3175, 99999):
            for weights in ([1, 1, 1], [1999, 450, 2], [7]):
                assert sum(allocate(total, weights)) == total

    def test_proportional(self):
        # $30.75 charge for a $20 and a $9 item (tax/shipping in the difference)
        assert allocate(3075, [2000, 900]) == [2121, 954]

    def test_negative_total(self):
        assert allocate(-1001, [1, 1]) == [-501, -500]

    def test_zero_weights_split_evenly(self):
        assert allocate(10, [0, 0, 0]) == [4, 3, 3]

    def test_cents_round_trip(self):
        assert to_cents(19.99) == 1999
        assert to_cents("0.105") == 11
        assert cents_to_str(-5) == "-0.05"
        assert cents_to_str(123456) == "1234.56"

    def test_rejects_bad_weights(self):
        with pytest.raises(ValueError):
            allocate(100, [])
        with pytest.raises(ValueError):
            allocate(100, [-1, 2])


class TestMatcher:
    def test_exact_amount_within_window(self):
        pairs = match([txn()], [purchase()], (2, 5))
        assert [(t.id, p.external_id) for t, p in pairs] == [("t1", "p1")]

    def test_amount_must_match(self):
        assert match([txn(cents=5001)], [purchase()], (2, 5)) == []

    def test_outside_window(self):
        assert match([txn(d=date(2026, 10, 20))], [purchase()], (2, 5)) == []
        assert match([txn(d=date(2026, 10, 1))], [purchase()], (2, 5)) == []

    def test_same_price_charges_pair_by_closest_date(self):
        t_early, t_late = txn("early", date(2026, 10, 2)), txn("late", date(2026, 10, 9))
        p_early, p_late = purchase("pe", date(2026, 10, 1)), purchase("pl", date(2026, 10, 8))
        pairs = {t.id: p.external_id for t, p in match([t_late, t_early], [p_late, p_early], (2, 5))}
        assert pairs == {"early": "pe", "late": "pl"}

    def test_one_to_one(self):
        pairs = match([txn("a"), txn("b")], [purchase()], (2, 5))
        assert len(pairs) == 1


class TestPlanner:
    def test_multi_item_splits_with_exact_sum(self):
        p = purchase(cents=3075, lines=[Line("USB-C cable", 2000), Line("Toothpaste", 900, quantity=3)])
        result = plan(txn(cents=3075), p)
        assert result.action == "split"
        assert result.children == [("USB-C cable", 2121), ("Toothpaste (x3)", 954)]
        assert sum(c for _, c in result.children) == 3075

    def test_single_item_becomes_a_one_row_split(self):
        # The parent is usually already AI-categorized from the bare merchant
        # name; a fresh row named after the item gets categorized again.
        result = plan(txn(cents=4500), purchase(cents=4500, lines=[Line("Headphones", 3999)]))
        assert result.action == "split"
        assert result.children == [("Headphones", 4500)]

    def test_problem_skips(self):
        result = plan(txn(), purchase(problem="couldn't tell"))
        assert (result.action, result.reason) == ("skip", "couldn't tell")

    def test_refund_split_keeps_sign(self):
        p = purchase(cents=-1000, lines=[Line("A", 500), Line("B", 500)])
        result = plan(txn(cents=-1000), p)
        assert [c for _, c in result.children] == [-500, -500]

    def test_long_names_are_trimmed(self):
        p = purchase(lines=[Line("x" * 300, 1), Line("y", 1)])
        name = plan(txn(), p).children[0][0]
        assert len(name) <= 120 and name.endswith("…")
