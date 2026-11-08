import pytest

from financeapp.importers import ImportError_, detect_bank, merchant_key, parse_csv, parse_money

from .conftest import fixture_text


def test_detects_banks():
    assert detect_bank(["Date", "Description", "Check Number", "Amount", "Running Balance"]) == "truist"
    assert detect_bank(["Date", "Description", "Original Description", "Category", "Amount"]) == "usaa"
    assert detect_bank(["Date", "Description", "Amount"]) == "other"


def test_truist_has_no_category():
    bank, rows = parse_csv(fixture_text("truist_sample.csv"))
    assert bank == "truist"
    assert len(rows) == 4
    assert all(r.bank_category is None for r in rows)
    assert rows[0].date == "2026-11-03" and rows[0].amount_cents == -575


def test_usaa_checking_keeps_category_and_skips_pending():
    bank, rows = parse_csv(fixture_text("usaa_checking_sample.csv"))
    assert bank == "usaa"
    assert [r.bank_category for r in rows] == ["Groceries", "Gas & Fuel", "Paycheck"]
    assert rows[0].amount_cents == -8214


def test_usaa_credit_card_signs_are_flipped():
    _, rows = parse_csv(fixture_text("usaa_credit_sample.csv"), account_kind="credit")
    netflix, payment = rows
    assert netflix.amount_cents == -1549   # a charge is money out
    assert payment.amount_cents == 50000


def test_debit_credit_columns():
    _, rows = parse_csv("Date,Description,Debit,Credit\n01/02/2026,Coffee,4.50,\n01/03/2026,Refund,,10\n")
    assert [r.amount_cents for r in rows] == [-450, 1000]


def test_money_formats():
    assert parse_money("$1,234.56") == 123456
    assert parse_money("(12.00)") == -1200
    assert parse_money("") is None


def test_bad_file_is_rejected():
    with pytest.raises(ImportError_):
        parse_csv("foo,bar\n1,2\n")


def test_merchant_key_ignores_noise_and_store_numbers():
    assert merchant_key("POS DEBIT STARBUCKS #1234 RALEIGH NC") == "STARBUCKS RALEIGH NC"
    assert merchant_key("POS DEBIT STARBUCKS #9876 RALEIGH NC") == "STARBUCKS RALEIGH NC"
