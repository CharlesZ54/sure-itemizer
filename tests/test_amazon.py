from datetime import date

from itemizer.sources.amazon import ChargeView, ItemView, OrderView, purchases_for_order

D = date(2026, 10, 9)


def item(title, cents, qty=1):
    return ItemView(title=title, unit_cents=cents, quantity=qty)


def charge(cents, d=D, refund=False):
    return ChargeView(date=d, amount_cents=cents, is_refund=refund, payment_method="Visa ****0000")


def test_single_shipment_single_charge_gets_all_items():
    order = OrderView("111-1", [[item("Headphones", 2999), item("Shampoo", 899)]], grand_total_cents=4185)
    [p] = purchases_for_order(order, [charge(4185)])
    assert p.problem is None
    assert [line.name for line in p.lines] == ["Headphones", "Shampoo"]
    assert p.amount_cents == 4185 and p.reference == "111-1"


def test_charges_matched_to_their_shipments():
    # Two shipments, 8% tax; Amazon charged each shipment separately.
    order = OrderView("111-2", [[item("Monitor", 20000)], [item("Toothbrush", 500, qty=2)]],
                      grand_total_cents=22680)
    purchases = purchases_for_order(order, [charge(1080), charge(21600)])
    by_amount = {p.amount_cents: [line.name for line in p.lines] for p in purchases}
    assert by_amount == {1080: ["Toothbrush"], 21600: ["Monitor"]}
    assert all(p.problem is None for p in purchases)


def test_older_charge_outside_window_is_tolerated():
    # We only see the second shipment's charge; the first was before the lookback.
    order = OrderView("111-3", [[item("Monitor", 20000)], [item("Toothbrush", 500, qty=2)]],
                      grand_total_cents=22680)
    [p] = purchases_for_order(order, [charge(1080)])
    assert [line.name for line in p.lines] == ["Toothbrush"]


def test_ambiguous_assignment_is_reported_not_guessed():
    order = OrderView("111-4", [[item("A", 1000)], [item("B", 1000)]], grand_total_cents=2000)
    purchases = purchases_for_order(order, [charge(1000), charge(1000)])
    # A and B cost the same, so either charge could be either item.
    assert all(p.problem and "more than one way" in p.problem for p in purchases)


def test_identical_shipments_are_not_ambiguous():
    order = OrderView("111-5", [[item("Filter", 1000)], [item("Filter", 1000)]], grand_total_cents=2000)
    purchases = purchases_for_order(order, [charge(1000), charge(1000)])
    assert all(p.problem is None for p in purchases)
    assert len({p.external_id for p in purchases}) == 2


def test_gift_card_partial_payment_still_apportions():
    order = OrderView("111-6", [[item("Lamp", 3000), item("Bulbs", 1000)]], grand_total_cents=4300)
    [p] = purchases_for_order(order, [charge(2300)])  # $20 gift card covered the rest
    assert p.problem is None and len(p.lines) == 2


def test_refund_on_single_item_order():
    order = OrderView("111-7", [[item("Cable", 1299)]], grand_total_cents=1402)
    [p] = purchases_for_order(order, [charge(1402, refund=True)])
    assert p.amount_cents == -1402 and p.lines[0].name == "Cable"


def test_refund_on_multi_item_order_is_a_problem():
    order = OrderView("111-8", [[item("Cable", 1299), item("Case", 999)]], grand_total_cents=2482)
    [p] = purchases_for_order(order, [charge(1402, refund=True)])
    assert p.amount_cents == -1402 and p.problem


def test_missing_prices_are_a_problem():
    order = OrderView("111-9", [[item("A", None), item("B", 500)]], grand_total_cents=1000)
    [p] = purchases_for_order(order, [charge(1000)])
    assert p.problem == "some item prices are missing"


def test_unexplainable_charges_are_a_problem():
    order = OrderView("111-10", [[item("A", 1000)], [item("B", 3000)]], grand_total_cents=4000)
    purchases = purchases_for_order(order, [charge(1500), charge(2500)])
    assert all(p.problem for p in purchases)
