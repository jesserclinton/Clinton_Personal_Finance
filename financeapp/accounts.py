"""Account balances you type in, checked against imported transactions.

Each balance is "what the bank showed at the end of this day". Between two
balances for the same account, the change should equal the sum of the
transactions imported in between. A difference means something is missing,
doubled, or removed that shouldn't be.

Balances are signed like transactions: money you have is positive. For a
credit card, what you owe is stored as a negative balance, matching card
charges, which are negative.
"""
from datetime import date

KINDS = ("checking", "savings", "credit")


def add_balance(db, account_id, as_of, balance_cents, note=""):
    date.fromisoformat(as_of)  # raises ValueError if malformed
    db.execute(
        """INSERT INTO account_balances (account_id, as_of, balance_cents, note)
           VALUES (?, ?, ?, ?)
           ON CONFLICT (account_id, as_of) DO UPDATE
           SET balance_cents = excluded.balance_cents, note = excluded.note""",
        (account_id, as_of, balance_cents, note.strip() or None),
    )
    db.commit()


def delete_balance(db, balance_id):
    db.execute("DELETE FROM account_balances WHERE id = ?", (balance_id,))
    db.commit()


def _activity(db, account_id, after=None, through=None):
    """(sum of cents, count) of kept transactions with after < date <= through."""
    where, params = ["account_id = ?", "removed = 0"], [account_id]
    if after:
        where.append("date > ?")
        params.append(after)
    if through:
        where.append("date <= ?")
        params.append(through)
    row = db.execute(
        f"SELECT COALESCE(SUM(amount_cents), 0), COUNT(*) FROM transactions WHERE {' AND '.join(where)}",
        params,
    ).fetchone()
    return row[0], row[1]


def reconcile(db, account):
    """Everything the Accounts page shows for one account."""
    aid = account["id"]
    first_tx, last_tx, n_tx = db.execute(
        "SELECT MIN(date), MAX(date), COUNT(*) FROM transactions WHERE account_id = ? AND removed = 0",
        (aid,),
    ).fetchone()
    balances = db.execute(
        "SELECT * FROM account_balances WHERE account_id = ? ORDER BY as_of", (aid,)
    ).fetchall()

    checks, prev = [], None
    for b in balances:
        check = {
            "id": b["id"], "as_of": b["as_of"], "balance_cents": b["balance_cents"], "note": b["note"],
            "prev_as_of": None, "activity_cents": None, "n": 0, "expected_cents": None,
            "diff_cents": None, "past_imports": bool(last_tx is None or b["as_of"] > last_tx),
        }
        if prev:
            activity, n = _activity(db, aid, after=prev["as_of"], through=b["as_of"])
            expected = prev["balance_cents"] + activity
            check.update(prev_as_of=prev["as_of"], activity_cents=activity, n=n,
                         expected_cents=expected, diff_cents=b["balance_cents"] - expected)
        checks.append(check)
        prev = b

    latest = balances[-1] if balances else None
    estimate = None
    if latest:
        after, n_after = _activity(db, aid, after=latest["as_of"])
        first = balances[0]
        before, _ = _activity(db, aid, through=first["as_of"])
        estimate = {
            # The balance once every imported transaction is counted
            "through": last_tx if last_tx and last_tx > latest["as_of"] else latest["as_of"],
            "balance_cents": latest["balance_cents"] + after,
            "n_after": n_after,
            # What the account must have held before the first imported transaction
            # (worked back from the earliest balance you entered)
            "opening_cents": first["balance_cents"] - before if first_tx and first_tx <= first["as_of"] else None,
        }
    mismatches = [c for c in checks if c["diff_cents"]]
    return {
        "account": account,
        "first_tx": first_tx,
        "last_tx": last_tx,
        "n_tx": n_tx,
        "checks": list(reversed(checks)),  # newest first
        "estimate": estimate,
        "status": ("none" if not balances else "single" if len(balances) == 1
                   else "off" if mismatches else "ok"),
        "n_mismatches": len(mismatches),
    }


def all_accounts(db):
    accounts = db.execute(
        """SELECT * FROM accounts ORDER BY
             CASE kind WHEN 'checking' THEN 0 WHEN 'savings' THEN 1 ELSE 2 END, name"""
    ).fetchall()
    return [reconcile(db, a) for a in accounts]
