"""Business logic: importing into the database and categorising."""
import hashlib
from collections import Counter

from .importers import merchant_key, parse_csv


def get_or_create_category(db, name, kind=None):
    name = name.strip()
    row = db.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = db.execute(
        "INSERT INTO categories (name, kind) VALUES (?, ?)", (name, kind or "expense")
    )
    return cur.lastrowid


def _fingerprint(account_id, row, occurrence):
    # occurrence keeps two identical same-day purchases apart while still
    # matching the same pair when an overlapping export is imported again.
    key = f"{account_id}|{row.date}|{row.amount_cents}|{row.original_description}|{occurrence}"
    return hashlib.sha256(key.encode()).hexdigest()


def categorise(db, mkey, bank_category, amount_cents):
    """Pick a category for a new transaction. Returns (category_id, source).

    Order: your saved merchant rules first (so your corrections beat the
    bank), then the bank's own category, otherwise left for review.
    """
    rule = db.execute(
        "SELECT category_id FROM rules WHERE merchant_key = ?", (mkey,)
    ).fetchone()
    if rule:
        return rule["category_id"], "rule"
    if bank_category:
        kind = "income" if amount_cents > 0 else "expense"
        return get_or_create_category(db, bank_category, kind), "bank"
    return None, None


def account_from_name(db, name):
    """Find or create an account named in a multi-account export.

    'Truist - Credit Card - Truist - credit' -> bank 'truist', kind 'credit'.
    """
    row = db.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    lower = name.lower()
    bank = next((b for b in ("truist", "usaa") if lower.startswith(b)), "other")
    kind = "credit" if "credit" in lower else "savings" if "saving" in lower else "checking"
    return db.execute(
        "INSERT INTO accounts (name, bank, kind) VALUES (?, ?, ?)", (name, bank, kind)
    ).lastrowid


def import_csv(db, account_id, filename, text):
    """Import a CSV into one account, or with account_id=None into the
    accounts named in the file (Truist's export can cover several)."""
    kind = "checking"
    if account_id is not None:
        acct = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if acct is None:
            raise ValueError("Unknown account")
        kind = acct["kind"]
    _, rows = parse_csv(text, account_kind=kind)
    if account_id is None and any(not r.account_name for r in rows):
        raise ValueError(
            "This file doesn't say which account each transaction belongs to. "
            "Pick an account instead."
        )

    accounts = {}
    if account_id is None:
        for r in rows:
            if r.account_name not in accounts:
                accounts[r.account_name] = account_from_name(db, r.account_name)
        if not accounts:
            raise ValueError("The file has no transactions.")
    first_account = account_id if account_id is not None else next(iter(accounts.values()))

    cur = db.execute(
        "INSERT INTO imports (account_id, filename) VALUES (?, ?)", (first_account, filename)
    )
    import_id = cur.lastrowid
    seen = Counter()
    added = skipped = 0
    for row in rows:
        acct_id = account_id if account_id is not None else accounts[row.account_name]
        ident = (acct_id, row.date, row.amount_cents, row.original_description)
        seen[ident] += 1
        fp = _fingerprint(acct_id, row, seen[ident])
        if db.execute("SELECT 1 FROM transactions WHERE fingerprint = ?", (fp,)).fetchone():
            skipped += 1
            continue
        mkey = merchant_key(row.merchant or row.original_description or row.description)
        cat_id, source = categorise(db, mkey, row.bank_category, row.amount_cents)
        db.execute(
            """INSERT INTO transactions
               (account_id, import_id, date, description, original_description,
                amount_cents, bank_category, category_id, category_source,
                merchant_key, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (acct_id, import_id, row.date, row.description, row.original_description,
             row.amount_cents, row.bank_category, cat_id, source, mkey, fp),
        )
        added += 1
    db.execute(
        "UPDATE imports SET rows_added = ?, rows_skipped = ? WHERE id = ?",
        (added, skipped, import_id),
    )
    db.commit()
    return {"added": added, "skipped": skipped, "import_id": import_id,
            "accounts": len(accounts) or 1}


def set_category(db, tx_id, category_id, remember=False):
    """Change one transaction's category (overrides the bank's).

    With remember=True, also save a rule for this merchant and apply it to
    every other transaction from the same merchant that you haven't
    individually set yourself.
    """
    tx = db.execute("SELECT merchant_key FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if tx is None:
        raise ValueError("Unknown transaction")
    db.execute(
        "UPDATE transactions SET category_id = ?, category_source = 'user' WHERE id = ?",
        (category_id, tx_id),
    )
    updated = 0
    if remember:
        db.execute(
            """INSERT INTO rules (merchant_key, category_id) VALUES (?, ?)
               ON CONFLICT(merchant_key) DO UPDATE SET category_id = excluded.category_id""",
            (tx["merchant_key"], category_id),
        )
        updated = db.execute(
            """UPDATE transactions SET category_id = ?, category_source = 'rule'
               WHERE merchant_key = ? AND id != ?
                 AND (category_source IS NULL OR category_source != 'user')""",
            (category_id, tx["merchant_key"], tx_id),
        ).rowcount
    db.commit()
    return updated


def month_summary(db, month):
    """Income and spending per category for a YYYY-MM month."""
    return db.execute(
        """SELECT COALESCE(c.name, 'Uncategorized') AS category, t.category_id,
                  SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents ELSE 0 END) AS income_cents,
                  SUM(CASE WHEN t.amount_cents < 0 THEN -t.amount_cents ELSE 0 END) AS spent_cents,
                  COUNT(*) AS n
           FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE substr(t.date, 1, 7) = ? AND t.removed = 0
           GROUP BY t.category_id ORDER BY spent_cents DESC, income_cents DESC""",
        (month,),
    ).fetchall()


def filter_transactions(db, month=None, category_ids=(), uncategorised=False,
                        account_id=None, direction=None, search=None, removed=False):
    """Transactions matching every filter given. Empty filters match all.

    category_ids and uncategorised combine with OR (show these categories,
    plus uncategorised ones if asked). direction is 'out' or 'in'.
    """
    where, params = ["t.removed = ?"], [1 if removed else 0]
    if month:
        where.append("substr(t.date, 1, 7) = ?")
        params.append(month)
    cat_terms = []
    if category_ids:
        cat_terms.append(f"t.category_id IN ({','.join('?' * len(category_ids))})")
        params.extend(category_ids)
    if uncategorised:
        cat_terms.append("t.category_id IS NULL")
    if cat_terms:
        where.append("(" + " OR ".join(cat_terms) + ")")
    if account_id:
        where.append("t.account_id = ?")
        params.append(account_id)
    if direction == "out":
        where.append("t.amount_cents < 0")
    elif direction == "in":
        where.append("t.amount_cents > 0")
    if search:
        where.append("(t.description LIKE ? OR t.original_description LIKE ?)")
        params.extend([f"%{search}%"] * 2)
    sql = """SELECT t.*, a.name AS account, c.name AS category FROM transactions t
             JOIN accounts a ON a.id = t.account_id
             LEFT JOIN categories c ON c.id = t.category_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.date DESC, t.id DESC"
    return db.execute(sql, params).fetchall()


def set_removed(db, tx_ids, removed=True):
    """Hide transactions from every view and total, or bring them back.

    Removed transactions stay in the database so importing an overlapping
    file later doesn't add them again.
    """
    ids = [int(i) for i in tx_ids]
    if not ids:
        return 0
    n = db.execute(
        f"UPDATE transactions SET removed = ? WHERE id IN ({','.join('?' * len(ids))})",
        [1 if removed else 0, *ids],
    ).rowcount
    db.commit()
    return n


def undo_import(db, import_id):
    """Delete everything one import added, so the file can be imported again."""
    n = db.execute("DELETE FROM transactions WHERE import_id = ?", (import_id,)).rowcount
    db.execute("DELETE FROM imports WHERE id = ?", (import_id,))
    db.commit()
    return n
