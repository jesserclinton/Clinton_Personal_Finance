"""Monthly budgets, the spending heatmap, and the monthly CSV export."""
import csv
import io
from datetime import date


# Share of the budget used -> heat level. Lower bound inclusive.
LEVELS = [
    (0.0, "blue", "0–20%"),
    (0.2, "green", "20–60%"),
    (0.6, "yellow", "60–80%"),
    (0.8, "red", "80%+"),
]


def heat_level(pct):
    """'blue' / 'green' / 'yellow' / 'red' for a fraction used, or None with no budget."""
    if pct is None:
        return None
    level = LEVELS[0][1]
    for floor, name, _ in LEVELS:
        if pct >= floor:
            level = name
    return level


def month_add(month, delta):
    y, m = map(int, month.split("-"))
    m += delta
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def month_label(month, fmt="%B %Y"):
    try:
        return date.fromisoformat(month + "-01").strftime(fmt)
    except ValueError:
        return month


def effective_budgets(db, month):
    """category_id -> budget cents for a month.

    A budget carries forward: the latest amount set in this month or any
    earlier month applies until you change it. 0 means no budget.
    """
    rows = db.execute(
        """SELECT b.category_id, b.amount_cents FROM budgets b
           JOIN (SELECT category_id, MAX(month) AS m FROM budgets WHERE month <= ?
                 GROUP BY category_id) latest
             ON latest.category_id = b.category_id AND latest.m = b.month""",
        (month,),
    ).fetchall()
    return {r["category_id"]: r["amount_cents"] for r in rows if r["amount_cents"] > 0}


def set_budgets(db, month, amounts, start_month=None):
    """Save {category_id: cents or None} for a month (None/0 = no budget from here on).

    With start_month (earlier than month), each amount instead applies from
    start_month through month, replacing anything set in between; months
    before start_month keep their budgets, and changes already set for months
    after `month` are kept.
    """
    if start_month and start_month < month:
        for cid, cents in amounts.items():
            db.execute(
                "DELETE FROM budgets WHERE category_id = ? AND month BETWEEN ? AND ?",
                (cid, start_month, month),
            )
            db.execute(
                "INSERT INTO budgets (category_id, month, amount_cents) VALUES (?, ?, ?)",
                (cid, start_month, cents or 0),
            )
        db.commit()
        return sum(1 for c in amounts.values() if c)
    current = {**{cid: 0 for cid in amounts}, **effective_budgets(db, month)}
    changed = 0
    for cid, cents in amounts.items():
        cents = cents or 0
        if current.get(cid, 0) == cents:
            continue
        db.execute(
            """INSERT INTO budgets (category_id, month, amount_cents) VALUES (?, ?, ?)
               ON CONFLICT(category_id, month) DO UPDATE SET amount_cents = excluded.amount_cents""",
            (cid, month, cents),
        )
        changed += 1
    db.commit()
    return changed


def _net_spent(db, start_month, end_month):
    """{(category_id, month): net spending cents} for expense categories.

    Refunds in a category reduce its spending.
    """
    rows = db.execute(
        """SELECT t.category_id, substr(t.date, 1, 7) AS month, -SUM(t.amount_cents) AS spent
           FROM transactions t JOIN categories c ON c.id = t.category_id
           WHERE t.removed = 0 AND c.kind = 'expense'
             AND substr(t.date, 1, 7) BETWEEN ? AND ?
           GROUP BY t.category_id, month""",
        (start_month, end_month),
    ).fetchall()
    return {(r["category_id"], r["month"]): max(r["spent"], 0) for r in rows}


def budget_status(db, month):
    """One row per expense category: budget, spent, remaining, pct used, heat level.

    Categories with a budget come first (most used first), then unbudgeted
    categories with spending, then the rest.
    """
    budgets = effective_budgets(db, month)
    spent = _net_spent(db, month, month)
    cats = db.execute(
        "SELECT id, name FROM categories WHERE kind = 'expense' ORDER BY name COLLATE NOCASE"
    ).fetchall()
    rows = []
    for c in cats:
        b = budgets.get(c["id"])
        s = spent.get((c["id"], month), 0)
        pct = (s / b) if b else None
        rows.append({
            "category_id": c["id"], "category": c["name"], "budget_cents": b or 0,
            "spent_cents": s, "remaining_cents": (b - s) if b else None,
            "pct": pct, "level": heat_level(pct),
        })
    rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0), -r["spent_cents"], r["category"].lower()))
    return rows


def heatmap_history(db, end_month, months=6):
    """Months (oldest first) and, per budgeted category, [(spent, budget, pct, level)] per month."""
    month_list = [month_add(end_month, -i) for i in range(months - 1, -1, -1)]
    spent = _net_spent(db, month_list[0], month_list[-1])
    budgets = {m: effective_budgets(db, m) for m in month_list}
    cat_ids = sorted({cid for b in budgets.values() for cid in b})
    names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM categories WHERE kind = 'expense'")}
    rows = []
    for cid in cat_ids:
        if cid not in names:
            continue
        cells = []
        for m in month_list:
            b = budgets[m].get(cid)
            s = spent.get((cid, m), 0)
            pct = (s / b) if b else None
            cells.append({"month": m, "spent_cents": s, "budget_cents": b or 0, "pct": pct, "level": heat_level(pct)})
        rows.append({"category_id": cid, "category": names[cid], "cells": cells})
    rows.sort(key=lambda r: r["category"].lower())
    return month_list, rows


def _money(cents):
    return f"{(cents or 0) / 100:.2f}"


def export_month_csv(db, month):
    """CSV of where the month ended: income sources, spending per category vs budget, net."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"Month: {month_label(month)}"])
    w.writerow([])

    w.writerow(["INCOME"])
    w.writerow(["Category", "Source", "Amount"])
    sources = db.execute(
        """SELECT COALESCE(c.name, 'Uncategorized') AS category, t.description AS source,
                  SUM(t.amount_cents) AS amount
           FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE substr(t.date, 1, 7) = ? AND t.removed = 0 AND t.amount_cents > 0
             AND (c.kind = 'income' OR t.category_id IS NULL)
           GROUP BY category, source ORDER BY category COLLATE NOCASE, amount DESC""",
        (month,),
    ).fetchall()
    # Refunds in an expense category reduce that category's spending instead.
    total_income = sum(r["amount"] for r in sources)
    for r in sources:
        w.writerow([r["category"], r["source"], _money(r["amount"])])
    w.writerow(["Total income", "", _money(total_income)])
    w.writerow([])

    w.writerow(["SPENDING"])
    w.writerow(["Category", "Budget", "Spent", "Remaining", "% of budget used"])
    status = budget_status(db, month)
    total_spent = 0
    for r in status:
        if not r["spent_cents"] and not r["budget_cents"]:
            continue
        total_spent += r["spent_cents"]
        w.writerow([
            r["category"],
            _money(r["budget_cents"]) if r["budget_cents"] else "",
            _money(r["spent_cents"]),
            _money(r["remaining_cents"]) if r["remaining_cents"] is not None else "",
            f"{r['pct'] * 100:.0f}%" if r["pct"] is not None else "",
        ])
    uncategorised = db.execute(
        """SELECT -COALESCE(SUM(amount_cents), 0) FROM transactions
           WHERE substr(date, 1, 7) = ? AND removed = 0 AND category_id IS NULL AND amount_cents < 0""",
        (month,),
    ).fetchone()[0]
    if uncategorised > 0:
        total_spent += uncategorised
        w.writerow(["Uncategorized", "", _money(uncategorised), "", ""])
    total_budget = sum(r["budget_cents"] for r in status)
    w.writerow(["Total spending", _money(total_budget) if total_budget else "", _money(total_spent),
                _money(total_budget - total_spent) if total_budget else "", ""])
    w.writerow([])

    w.writerow(["NET (income minus spending)", "", _money(total_income - total_spent)])
    return out.getvalue()


def first_month_with_data(db):
    row = db.execute("SELECT MIN(substr(date, 1, 7)) FROM transactions WHERE removed = 0").fetchone()
    return row[0]
