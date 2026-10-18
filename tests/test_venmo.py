from datetime import date

import pytest

from itemizer.models import SureTxn
from itemizer.planner import ORIGINAL_MARKER, plan
from itemizer.sources.venmo import parse_statement, purchases_from

# Shaped like Venmo's statement CSV; people and amounts are made up.
STATEMENT = """Account Statement - (@Pat-Example) ,,,,,,,,,,,,,,,,,,,,,
Account Activity,,,,,,,,,,,,,,,,,,,,,
,ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (tip),Amount (tax),Amount (fee),Tax Rate,Tax Exempt,Funding Source,Destination,Beginning Balance,Ending Balance,Statement Period Venmo Fees,Terminal Location,Year to Date Venmo Fees,Disclaimer
,,,,,,,,,,,,,,,,$10.00,,,,,
,1001,2026-10-01T12:00:00,Payment,Complete,pizza \U0001F355,Pat Example,Alex Doe,- $38.09,,0,,0,,Bank Account *0000,,,,,Venmo,,
,1002,2026-10-02T12:00:00,Payment,Complete,dinner,Sam Roe,Pat Example,+ $40.00,,0,,0,,,Venmo balance,,,,Venmo,,
,1003,2026-10-03T09:00:00,Charge,Complete,tickets,Pat Example,Jo Poe,+ $25.19,,0,,0,,,Venmo balance,,,,Venmo,,
,1004,2026-10-03T12:00:00,Payment,Complete,coffee,Pat Example,Kim Loe,- $5.00,,0,,0,,Venmo balance,,,,,Venmo,,
,1005,2026-10-04T08:00:00,Standard Transfer,Issued,,,,- $65.19,,0,,0,,,Bank Account *0000,,,,Venmo,,
,1006,2026-10-06T08:00:00,Instant Transfer,Issued,,,,- $100.00,,0,- $1.75,0,,,Bank Account *0000,,,,Venmo,,
,1007,2026-10-07T08:00:00,Charge,Complete,"utilities, Oct",Lee Moe,Pat Example,"- $1,076.20",,0,,0,,Bank Account *0000,,,,,Venmo,,
,1008,2026-10-08T08:00:00,Payment,Cancelled,oops,Pat Example,Kim Loe,- $9.00,,0,,0,,Bank Account *0000,,,,,Venmo,,
,,,,,,,,,,,,,,,,,$0.00,,,,
"""


def by_id(purchases):
    return {p.external_id: p for p in purchases}


def test_parses_rows_and_skips_balance_lines():
    rows = parse_statement(STATEMENT)
    assert [r.id for r in rows] == ["1001", "1002", "1003", "1004", "1005", "1006", "1007", "1008"]
    assert rows[0].cents == -3809 and rows[6].cents == -107620 and rows[5].fee_cents == 175
    assert rows[6].note == "utilities, Oct"


def test_rejects_files_that_arent_statements():
    with pytest.raises(ValueError):
        parse_statement("date,amount\n2026-01-01,5\n")


def test_payments_from_the_bank_are_described_with_who_and_why():
    p = by_id(purchases_from(parse_statement(STATEMENT)))
    assert p["1001"].amount_cents == 3809 and p["1001"].description == "Venmo to Alex Doe: pizza \U0001F355"
    # A request someone else sent you, which you paid: the other side is its sender.
    assert p["1007"].amount_cents == 107620 and p["1007"].description == "Venmo to Lee Moe: utilities, Oct"


def test_cash_outs_list_the_payments_they_carried():
    p = by_id(purchases_from(parse_statement(STATEMENT)))
    assert p["1005"].amount_cents == -6519
    assert p["1005"].description == "Venmo cash-out: Sam Roe (dinner), Jo Poe (tickets)"
    # Instant transfer: the bank gets the amount minus the fee.
    assert p["1006"].amount_cents == -9825 and p["1006"].description == "Venmo cash-out"


def test_balance_payments_and_cancelled_ones_never_reach_the_bank():
    p = by_id(purchases_from(parse_statement(STATEMENT)))
    assert "1004" not in p and "1008" not in p and "1002" not in p


def test_many_payments_are_summarised():
    rows = parse_statement(STATEMENT)
    extra = "\n".join(f",20{i},2026-10-05T0{i}:00:00,Payment,Complete,n{i},P{i},Pat Example,+ $1.00,,0,,0,,,"
                      f"Venmo balance,,,,Venmo,," for i in range(1, 6))
    more = parse_statement(STATEMENT.replace(",1006,", extra + "\n,1006,", 1))
    cash_out = by_id(purchases_from(more))["1006"]
    assert cash_out.description == "Venmo cash-out: P1 (n1), P2 (n2), P3 (n3) +2 more"
    assert len(rows) == 8


def test_rename_plan_keeps_the_bank_text_in_the_notes():
    txn = SureTxn("t", date(2026, 10, 2), 3809, "Transfer to Venmo - VENMO PAYMENT", "mine", "a", "Checking", None)
    purchase = by_id(purchases_from(parse_statement(STATEMENT)))["1001"]
    result = plan(txn, purchase)
    assert result.action == "rename" and result.name == "Venmo to Alex Doe: pizza \U0001F355"
    assert result.notes == f"mine\n{ORIGINAL_MARKER}Transfer to Venmo - VENMO PAYMENT"
    # Renaming again doesn't stack the original text.
    again = plan(SureTxn("t", txn.date, 3809, "something", result.notes, "a", "Checking", None), purchase)
    assert again.notes == result.notes
