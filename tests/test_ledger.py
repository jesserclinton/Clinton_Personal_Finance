from financeapp import ledger

from .conftest import fixture_text, make_account


def test_import_dedupes_overlapping_files(db):
    acct = make_account(db, "Checking", "other")
    first = ledger.import_csv(db, acct, "a.csv", fixture_text("plain_sample.csv"))
    again = ledger.import_csv(db, acct, "b.csv", fixture_text("plain_sample.csv"))
    assert first["added"] == 4  # the two identical Starbucks rows both count
    assert (again["added"], again["skipped"]) == (0, 4)


def test_truist_export_creates_accounts_and_uses_categories(db):
    result = ledger.import_csv(db, None, "ExportData.csv", fixture_text("truist_sample.csv"))
    assert (result["added"], result["accounts"]) == (5, 3)
    accts = {r["name"]: (r["bank"], r["kind"]) for r in db.execute("SELECT * FROM accounts")}
    assert accts["Truist - Credit Card - Truist - credit"] == ("truist", "credit")
    assert accts["Truist - Bank - Truist - checking"] == ("truist", "checking")
    assert accts["USAA - Credit Card - CASH REWARDS VISA (x0002"] == ("usaa", "credit")
    cats = {r["name"]: r["kind"] for r in db.execute("SELECT * FROM categories")}
    assert cats["Groceries"] == "expense" and cats["Paychecks/Salary"] == "income"
    keys = {r[0] for r in db.execute("SELECT merchant_key FROM transactions")}
    assert "GROCER CO" in keys  # from the clean Merchant column

    again = ledger.import_csv(db, None, "ExportData.csv", fixture_text("truist_sample.csv"))
    assert (again["added"], again["skipped"]) == (0, 5)


def test_file_without_account_column_needs_an_account(db):
    import pytest
    with pytest.raises(ValueError):
        ledger.import_csv(db, None, "a.csv", fixture_text("plain_sample.csv"))


def test_bank_categories_are_used_and_created(db):
    acct = make_account(db, "USAA Checking", "usaa")
    ledger.import_csv(db, acct, "u.csv", fixture_text("usaa_checking_sample.csv"))
    rows = db.execute(
        "SELECT c.name, t.category_source FROM transactions t JOIN categories c ON c.id = t.category_id"
    ).fetchall()
    assert {r["name"] for r in rows} == {"Groceries", "Gas & Fuel", "Paycheck"}
    assert all(r["category_source"] == "bank" for r in rows)
    kind = db.execute("SELECT kind FROM categories WHERE name='Paycheck'").fetchone()["kind"]
    assert kind == "income"


def test_uncategorised_then_remembered(db):
    acct = make_account(db, "Checking", "other")
    ledger.import_csv(db, acct, "a.csv", fixture_text("plain_sample.csv"))
    uncategorised = db.execute("SELECT id FROM transactions WHERE category_id IS NULL").fetchall()
    assert len(uncategorised) == 4

    coffee = ledger.get_or_create_category(db, "Coffee")
    tx = db.execute("SELECT id FROM transactions WHERE description LIKE '%STARBUCKS%' LIMIT 1").fetchone()
    others = ledger.set_category(db, tx["id"], coffee, remember=True)
    assert others == 1  # the other Starbucks visit was sorted too

    # A later import of the same merchant is sorted by the saved rule.
    later = "Date,Description,Check Number,Amount,Running Balance\n11/20/2026,POS DEBIT STARBUCKS #1234 RALEIGH NC,,-6.10,1\n"
    ledger.import_csv(db, acct, "later.csv", later)
    row = db.execute("SELECT category_id, category_source FROM transactions WHERE amount_cents = -610").fetchone()
    assert (row["category_id"], row["category_source"]) == (coffee, "rule")


def test_user_can_override_bank_category(db):
    acct = make_account(db, "USAA Checking", "usaa")
    ledger.import_csv(db, acct, "u.csv", fixture_text("usaa_checking_sample.csv"))
    shell = db.execute("SELECT id FROM transactions WHERE description = 'Shell'").fetchone()["id"]
    car = ledger.get_or_create_category(db, "Car")
    ledger.set_category(db, shell, car, remember=False)
    row = db.execute("SELECT category_id, category_source FROM transactions WHERE id = ?", (shell,)).fetchone()
    assert (row["category_id"], row["category_source"]) == (car, "user")


def test_rule_beats_bank_category_on_future_imports(db):
    acct = make_account(db, "USAA Checking", "usaa")
    ledger.import_csv(db, acct, "u.csv", fixture_text("usaa_checking_sample.csv"))
    shell = db.execute("SELECT id FROM transactions WHERE description = 'Shell'").fetchone()["id"]
    car = ledger.get_or_create_category(db, "Car")
    ledger.set_category(db, shell, car, remember=True)
    nxt = "Date,Description,Original Description,Category,Amount\n2026-11-20,Shell,SHELL OIL 57444 CARY NC,Gas & Fuel,-30.00\n"
    ledger.import_csv(db, acct, "n.csv", nxt)
    row = db.execute("SELECT category_id FROM transactions WHERE amount_cents = -3000").fetchone()
    assert row["category_id"] == car


def test_manual_choice_is_not_overwritten_by_a_rule(db):
    acct = make_account(db, "Checking", "other")
    ledger.import_csv(db, acct, "a.csv", fixture_text("plain_sample.csv"))
    a, b = [r["id"] for r in db.execute("SELECT id FROM transactions WHERE description LIKE '%STARBUCKS%'")]
    gift = ledger.get_or_create_category(db, "Gifts")
    coffee = ledger.get_or_create_category(db, "Coffee")
    ledger.set_category(db, a, gift)                 # one-off
    ledger.set_category(db, b, coffee, remember=True)
    assert db.execute("SELECT category_id FROM transactions WHERE id=?", (a,)).fetchone()[0] == gift


def test_month_summary(db):
    acct = make_account(db, "USAA Checking", "usaa")
    ledger.import_csv(db, acct, "u.csv", fixture_text("usaa_checking_sample.csv"))
    rows = {r["category"]: r for r in ledger.month_summary(db, "2026-11")}
    assert rows["Groceries"]["spent_cents"] == 8214
    assert rows["Paycheck"]["income_cents"] == 150000
