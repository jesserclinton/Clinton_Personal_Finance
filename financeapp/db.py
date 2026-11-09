"""SQLite connection handling and schema."""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    bank TEXT NOT NULL,              -- 'truist', 'usaa' or 'other'
    kind TEXT NOT NULL DEFAULT 'checking'  -- 'checking', 'savings' or 'credit'
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL DEFAULT 'expense'   -- 'expense', 'income' or 'transfer'
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    rows_added INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0
);

-- amount is signed: negative = money out, positive = money in,
-- whatever convention the bank's file used.
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    import_id INTEGER REFERENCES imports(id),
    date TEXT NOT NULL,                  -- ISO YYYY-MM-DD
    description TEXT NOT NULL,
    original_description TEXT,
    amount_cents INTEGER NOT NULL,
    bank_category TEXT,
    category_id INTEGER REFERENCES categories(id),
    category_source TEXT,                -- 'bank', 'rule' or 'user'
    merchant_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    removed INTEGER NOT NULL DEFAULT 0   -- 1 = hidden from totals; kept so re-imports skip it
);
CREATE INDEX IF NOT EXISTS ix_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS ix_tx_merchant ON transactions(merchant_key);

-- "Always put this merchant in this category"
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    merchant_key TEXT NOT NULL UNIQUE,
    category_id INTEGER NOT NULL REFERENCES categories(id)
);

-- Actual income you enter by hand for a month, overriding what was imported
-- (e.g. a paycheck that lands in an account you don't import).
CREATE TABLE IF NOT EXISTS income_actuals (
    category_id INTEGER NOT NULL REFERENCES categories(id),
    month TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (category_id, month)
);

-- Monthly budget per category. For income categories this is expected income.
CREATE TABLE IF NOT EXISTS budgets (
    category_id INTEGER NOT NULL REFERENCES categories(id),
    month TEXT NOT NULL,                 -- YYYY-MM
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (category_id, month)
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(conn):
    conn.executescript(SCHEMA)
    # Upgrade databases created before a column existed.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "removed" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN removed INTEGER NOT NULL DEFAULT 0")
    conn.commit()
