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


def import_csv(db, account_id, filename, text):
    acct = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if acct is None:
        raise ValueError("Unknown account")
    bank = acct["bank"] if acct["bank"] in ("truist", "usaa") else None
    _, rows = parse_csv(text, account_kind=acct["kind"], bank=bank)

    cur = db.execute(
        "INSERT INTO imports (account_id, filename) VALUES (?, ?)", (account_id, filename)
    )
    import_id = cur.lastrowid
    seen = Counter()
    added = skipped = 0
    for row in rows:
        ident = (row.date, row.amount_cents, row.original_description)
        seen[ident] += 1
        fp = _fingerprint(account_id, row, seen[ident])
        if db.execute("SELECT 1 FROM transactions WHERE fingerprint = ?", (fp,)).fetchone():
            skipped += 1
            continue
        mkey = merchant_key(row.original_description or row.description)
        cat_id, source = categorise(db, mkey, row.bank_category, row.amount_cents)
        db.execute(
            """INSERT INTO transactions
               (account_id, import_id, date, description, original_description,
                amount_cents, bank_category, category_id, category_source,
                merchant_key, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, import_id, row.date, row.description, row.original_description,
             row.amount_cents, row.bank_category, cat_id, source, mkey, fp),
        )
        added += 1
    db.execute(
        "UPDATE imports SET rows_added = ?, rows_skipped = ? WHERE id = ?",
        (added, skipped, import_id),
    )
    db.commit()
    return {"added": added, "skipped": skipped, "import_id": import_id}


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
        """SELECT COALESCE(c.name, 'Uncategorized') AS category,
                  SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents ELSE 0 END) AS income_cents,
                  SUM(CASE WHEN t.amount_cents < 0 THEN -t.amount_cents ELSE 0 END) AS spent_cents,
                  COUNT(*) AS n
           FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE substr(t.date, 1, 7) = ?
           GROUP BY category ORDER BY spent_cents DESC, income_cents DESC""",
        (month,),
    ).fetchall()
