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


def test_filter_transactions(db):
    ledger.import_csv(db, None, "ExportData.csv", fixture_text("truist_sample.csv"))
    groceries = db.execute("SELECT id FROM categories WHERE name='Groceries'").fetchone()[0]
    dining = db.execute("SELECT id FROM categories WHERE name='Restaurants/Dining'").fetchone()[0]

    assert len(ledger.filter_transactions(db)) == 5
    assert len(ledger.filter_transactions(db, category_ids=[groceries])) == 2
    assert len(ledger.filter_transactions(db, category_ids=[groceries, dining])) == 3
    assert len(ledger.filter_transactions(db, month="2026-10")) == 0
    assert len(ledger.filter_transactions(db, direction="in")) == 1
    assert len(ledger.filter_transactions(db, search="coffee")) == 1
    usaa = db.execute("SELECT id FROM accounts WHERE bank='usaa'").fetchone()[0]
    assert len(ledger.filter_transactions(db, account_id=usaa)) == 1

    # Uncategorised can be combined with categories
    tx = ledger.filter_transactions(db, search="Landlord")[0]
    db.execute("UPDATE transactions SET category_id = NULL WHERE id = ?", (tx["id"],))
    rows = ledger.filter_transactions(db, category_ids=[dining], uncategorised=True)
    assert {r["description"] for r in rows} == {"Coffee House", "Landlord Llc Rent"}


def test_removed_transactions_are_hidden_and_stay_removed(db):
    ledger.import_csv(db, None, "ExportData.csv", fixture_text("truist_sample.csv"))
    coffee = ledger.filter_transactions(db, search="Coffee")[0]["id"]
    assert ledger.set_removed(db, [coffee]) == 1

    assert ledger.filter_transactions(db, search="Coffee") == []
    assert [r["id"] for r in ledger.filter_transactions(db, removed=True)] == [coffee]
    summary = {r["category"] for r in ledger.month_summary(db, "2026-11")}
    assert "Restaurants/Dining" not in summary

    # Re-importing the same file doesn't bring it back
    again = ledger.import_csv(db, None, "ExportData.csv", fixture_text("truist_sample.csv"))
    assert again["added"] == 0
    assert ledger.filter_transactions(db, search="Coffee") == []

    ledger.set_removed(db, [coffee], removed=False)
    assert len(ledger.filter_transactions(db, search="Coffee")) == 1


def test_undo_import_allows_reimport(db):
    first = ledger.import_csv(db, None, "ExportData.csv", fixture_text("truist_sample.csv"))
    assert ledger.undo_import(db, first["import_id"]) == 5
    assert ledger.filter_transactions(db) == []
    again = ledger.import_csv(db, None, "ExportData.csv", fixture_text("truist_sample.csv"))
    assert again["added"] == 5


def test_old_database_gets_removed_column(tmp_path):
    import re
    from financeapp.db import SCHEMA, connect, init_db
    conn = connect(tmp_path / "old.db")
    # The schema as it was before the removed column
    old = re.sub(r",\n    removed INTEGER[^\n]*", "", SCHEMA)
    assert "removed" not in old
    conn.executescript(old)
    init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    assert "removed" in cols


def test_card_payment_becomes_transfer_and_leaves_totals(db):
    acct = make_account(db, "USAA Visa", "usaa", "credit")
    ledger.import_csv(db, acct, "u.csv", fixture_text("usaa_credit_sample.csv"))
    kind = db.execute("SELECT kind FROM categories WHERE name='Credit Card Payment'").fetchone()[0]
    assert kind == "transfer"
    rows = ledger.month_summary(db, "2026-11")
    counted = [r for r in rows if ledger.counts_in_totals(r)]
    assert sum(r["income_cents"] for r in counted) == 0          # payment not income
    assert sum(r["spent_cents"] for r in counted) == 1549         # Netflix only


def test_transfer_detection():
    assert ledger.looks_like_transfer("Credit Card Payment")
    assert ledger.looks_like_transfer("Transfers")
    assert not ledger.looks_like_transfer("Groceries")


def test_change_category_kind(db):
    cid = ledger.get_or_create_category(db, "Savings move")
    ledger.set_category_kind(db, cid, "transfer")
    assert db.execute("SELECT kind FROM categories WHERE id=?", (cid,)).fetchone()[0] == "transfer"
    import pytest
    with pytest.raises(ValueError):
        ledger.set_category_kind(db, cid, "bogus")


def test_payment_descriptions_become_transfers_whatever_the_bank_category(db):
    text = (
        "Status,Date,Original Description,Custom Description,Merchant,Geo,High Level Category,"
        "Category-Subcategory,Tag,Currency,Amount,Split Type,Note,Account Name,Account Number,Simple Description\n"
        "posted,11/05/2026, PAYMENT RECEIVED THANK YOU, -,-,US-,Income,Other Income,-,$,500.00,-, -,"
        "Truist - Credit Card - Truist - credit,x0001, Credit Card Payment Received\n"
        "posted,11/05/2026, USAA CREDIT CRD PAYMENT ACH DEBIT, -,USAA,US-,Miscellaneous Expenses,Other Expenses,-,$,-500.00,-, -,"
        "Truist - Bank - Truist - checking,x0003, Payment to USAA\n"
        "posted,11/05/2026, GROCER CO, -,Grocer Co,US-,Food Expenses,Groceries,-,$,-20.00,-, -,"
        "Truist - Bank - Truist - checking,x0003, Grocer Co\n"
    )
    ledger.import_csv(db, None, "e.csv", text)
    rows = {r["description"]: r for r in ledger.filter_transactions(db)}
    for d in ("Credit Card Payment Received", "Payment to USAA"):
        assert rows[d]["category"] == "Credit Card Payment" and rows[d]["category_kind"] == "transfer"
        assert rows[d]["category_source"] == "auto"
    assert rows["Grocer Co"]["category"] == "Groceries"
    counted = [r for r in ledger.month_summary(db, "2026-11") if ledger.counts_in_totals(r)]
    assert sum(r["income_cents"] for r in counted) == 0
    assert sum(r["spent_cents"] for r in counted) == 2000


def test_upgrade_fixes_existing_database(db):
    # Simulate data imported before transfers existed
    db.execute("PRAGMA user_version = 0")
    acct = make_account(db, "Checking", "other")
    exp = db.execute("INSERT INTO categories (name, kind) VALUES ('Other Expenses', 'expense')").lastrowid
    old = db.execute("INSERT INTO categories (name, kind) VALUES ('Credit Card Payment', 'expense')").lastrowid
    for i, (desc, cat, src) in enumerate([("Payment to USAA", exp, "bank"),
                                          ("Payment to USAA", exp, "user"),
                                          ("Rent", exp, "bank"),
                                          ("Autopay", old, "bank")]):
        db.execute("""INSERT INTO transactions (account_id, date, description, amount_cents, category_id,
                      category_source, merchant_key, fingerprint) VALUES (?, '2026-11-01', ?, -100, ?, ?, 'K', ?)""",
                   (acct, desc, cat, src, f"fp{i}"))
    db.commit()
    ledger.upgrade(db)
    kinds = {r["name"]: r["kind"] for r in db.execute("SELECT * FROM categories")}
    assert kinds["Credit Card Payment"] == "transfer"
    cats = [r[0] for r in db.execute("SELECT category_id FROM transactions ORDER BY id")]
    assert cats == [old, exp, exp, old]   # the user's own choice is left alone
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
