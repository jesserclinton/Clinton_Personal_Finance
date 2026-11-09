import io

from financeapp import accounts

from .conftest import fixture_text, make_account


def add_tx(db, account_id, day, cents, removed=0):
    n = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    db.execute(
        """INSERT INTO transactions (account_id, date, description, amount_cents, merchant_key, fingerprint, removed)
           VALUES (?, ?, 'X', ?, 'x', ?, ?)""",
        (account_id, day, cents, f"fp{n}", removed),
    )
    db.commit()


def account(db, aid):
    return db.execute("SELECT * FROM accounts WHERE id = ?", (aid,)).fetchone()


def test_single_balance_gives_estimate_and_opening(db):
    aid = make_account(db, "Checking", "truist")
    add_tx(db, aid, "2026-10-01", 100_000)
    add_tx(db, aid, "2026-10-05", -20_000)
    add_tx(db, aid, "2026-10-20", -5_000)
    accounts.add_balance(db, aid, "2026-10-10", 130_000)
    r = accounts.reconcile(db, account(db, aid))
    assert r["status"] == "single"
    # 1,300 at end of Oct 10, then -50 on Oct 20
    assert r["estimate"]["balance_cents"] == 125_000
    assert r["estimate"]["n_after"] == 1
    # 1,300 - (1,000 - 200) = 500 before the first transaction
    assert r["estimate"]["opening_cents"] == 50_000


def test_two_balances_match_or_show_difference(db):
    aid = make_account(db, "Checking", "truist")
    accounts.add_balance(db, aid, "2026-10-01", 50_000)
    add_tx(db, aid, "2026-10-01", 999)       # on the start day: already in that balance
    add_tx(db, aid, "2026-10-02", -10_000)
    add_tx(db, aid, "2026-10-31", 20_000)    # on the end day: counted
    add_tx(db, aid, "2026-10-15", -7_000, removed=1)  # removed: not counted
    accounts.add_balance(db, aid, "2026-10-31", 60_000)
    r = accounts.reconcile(db, account(db, aid))
    assert r["status"] == "ok"
    latest = r["checks"][0]
    assert latest["activity_cents"] == 10_000 and latest["n"] == 2 and latest["diff_cents"] == 0

    accounts.add_balance(db, aid, "2026-10-31", 59_000)  # same date replaces it
    r = accounts.reconcile(db, account(db, aid))
    assert len(r["checks"]) == 2
    assert r["status"] == "off" and r["checks"][0]["diff_cents"] == -1_000


def test_credit_card_owed_is_negative_balance(client, db):
    aid = make_account(db, "Card", "truist", kind="credit")
    client.post(f"/accounts/{aid}/balances", data={"balance": "200.00", "as_of": "2026-10-01"})
    add_tx(db, aid, "2026-10-05", -5_000)  # a charge
    r = client.post(f"/accounts/{aid}/balances", data={"balance": "$250", "as_of": "2026-10-31"},
                    follow_redirects=True)
    assert b"Transactions match" in r.data
    assert b"$250.00" in r.data and b"Amount owed" in r.data


def test_accounts_page_flow(client, db):
    assert client.get("/accounts").status_code == 200
    r = client.post("/accounts", data={"name": "USAA Savings", "bank": "usaa", "kind": "savings"},
                    follow_redirects=True)
    assert b"Added USAA Savings" in r.data
    aid = db.execute("SELECT id FROM accounts WHERE name = 'USAA Savings'").fetchone()[0]
    r = client.post(f"/accounts/{aid}/balances", data={"balance": "abc", "as_of": "2026-11-01"},
                    follow_redirects=True)
    assert b"isn&#39;t an amount" in r.data
    r = client.post(f"/accounts/{aid}/balances", data={"balance": "1,234.56", "as_of": "2026-11-01"},
                    follow_redirects=True)
    assert b"$1,234.56" in r.data and b"Add a second balance" in r.data
    bid = db.execute("SELECT id FROM account_balances").fetchone()[0]
    client.post(f"/balances/{bid}/delete")
    assert db.execute("SELECT COUNT(*) FROM account_balances").fetchone()[0] == 0


def test_mismatch_links_to_transactions_in_range(client, db):
    data = {"file": (io.BytesIO(fixture_text("truist_sample.csv").encode()), "t.csv"), "account_id": "auto"}
    client.post("/import", data=data, content_type="multipart/form-data")
    aid = db.execute("SELECT id FROM accounts WHERE name LIKE '%checking'").fetchone()[0]
    client.post(f"/accounts/{aid}/balances", data={"balance": "100", "as_of": "2026-10-31"})
    r = client.post(f"/accounts/{aid}/balances", data={"balance": "999", "as_of": "2026-11-05"},
                    follow_redirects=True)
    assert b"1 period doesn&#39;t match" in r.data
    # +2,400 payroll, -1,500 rent: expected 1,000, entered 999
    assert b"$1,000.00" in r.data and b"-$1.00" in r.data
    r = client.get(f"/transactions?month=all&account={aid}&after=2026-10-31&through=2026-11-05")
    assert b"Landlord" in r.data and b"Grocer Co" not in r.data
    assert b"after Oct 31, 2026 through Nov 5, 2026" in r.data
    r = client.get(f"/transactions?month=all&after=2026-11-01")
    assert b"Employer" not in r.data and b"Landlord" in r.data
