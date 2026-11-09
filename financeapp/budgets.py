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


# Over budget, red darkens steadily from the normal red at 100% to the
# darkest red at OVER_CAP (and stays there beyond it).
_RED = ((0x8C, 0x3A, 0x32), (0x72, 0x2E, 0x28))       # gradient start/end at 100%
_DARKEST = ((0x45, 0x16, 0x13), (0x33, 0x10, 0x0E))   # at OVER_CAP and above
OVER_CAP = 2.0


def _mix(a, b, t):
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def over_budget_style(pct):
    """Inline background for pct > 100%: darker the further over. '' otherwise."""
    if pct is None or pct <= 1:
        return ""
    t = min((pct - 1) / (OVER_CAP - 1), 1)
    start = _mix(_RED[0], _DARKEST[0], t)
    end = _mix(_RED[1], _DARKEST[1], t)
    return f"background:linear-gradient(145deg, {start} 0%, {end} 100%)"


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


def _imported_income(db, month):
    """{category_id or None: cents} received this month (income categories and uncategorised)."""
    rows = db.execute(
        """SELECT t.category_id, SUM(t.amount_cents) AS amount
           FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
           WHERE substr(t.date, 1, 7) = ? AND t.removed = 0 AND t.amount_cents > 0
             AND (c.kind = 'income' OR t.category_id IS NULL)
           GROUP BY t.category_id""",
        (month,),
    ).fetchall()
    return {r["category_id"]: r["amount"] for r in rows}


def set_income_actuals(db, month, amounts):
    """Save hand-entered actual income {category_id: cents or None}; None clears the override."""
    for cid, cents in amounts.items():
        if cents is None:
            db.execute("DELETE FROM income_actuals WHERE category_id = ? AND month = ?", (cid, month))
        else:
            db.execute(
                """INSERT INTO income_actuals (category_id, month, amount_cents) VALUES (?, ?, ?)
                   ON CONFLICT(category_id, month) DO UPDATE SET amount_cents = excluded.amount_cents""",
                (cid, month, cents),
            )
    db.commit()


def income_status(db, month):
    """One row per income category: expected (carries forward), imported, entered, actual, difference."""
    expected = effective_budgets(db, month)
    imported = _imported_income(db, month)
    entered = {r["category_id"]: r["amount_cents"] for r in db.execute(
        "SELECT category_id, amount_cents FROM income_actuals WHERE month = ?", (month,))}
    cats = db.execute(
        "SELECT id, name FROM categories WHERE kind = 'income' ORDER BY name COLLATE NOCASE"
    ).fetchall()
    rows = []
    for c in cats:
        e = expected.get(c["id"], 0)
        imp = imported.get(c["id"], 0)
        manual = entered.get(c["id"])
        actual = manual if manual is not None else imp
        rows.append({
            "category_id": c["id"], "category": c["name"], "expected_cents": e,
            "imported_cents": imp, "entered_cents": manual, "actual_cents": actual,
            "difference_cents": actual - e,
        })
    if imported.get(None):
        rows.append({
            "category_id": None, "category": "Uncategorized", "expected_cents": 0,
            "imported_cents": imported[None], "entered_cents": None,
            "actual_cents": imported[None], "difference_cents": imported[None],
        })
    return rows


def _uncategorised_spending(db, month):
    return db.execute(
        """SELECT -COALESCE(SUM(amount_cents), 0) FROM transactions
           WHERE substr(date, 1, 7) = ? AND removed = 0 AND category_id IS NULL AND amount_cents < 0""",
        (month,),
    ).fetchone()[0]


def savings_summary(db, month, income=None, status=None):
    """Planned and actual money left over to move to savings or investments."""
    income = income if income is not None else income_status(db, month)
    status = status if status is not None else budget_status(db, month)
    expected_income = sum(r["expected_cents"] for r in income)
    actual_income = sum(r["actual_cents"] for r in income)
    budgeted = sum(r["budget_cents"] for r in status)
    spent = sum(r["spent_cents"] for r in status) + _uncategorised_spending(db, month)
    return {
        "expected_income": expected_income, "actual_income": actual_income,
        "budgeted_spending": budgeted, "actual_spending": spent,
        "planned_left": expected_income - budgeted, "actual_left": actual_income - spent,
    }


def _money(cents):
    return f"{(cents or 0) / 100:.2f}"


def export_month_csv(db, month):
    """CSV of where the month ended: income vs expected, spending vs budget, what's left to save."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"Month: {month_label(month)}"])
    w.writerow([])

    income = income_status(db, month)
    w.writerow(["INCOME"])
    w.writerow(["Category", "Expected", "Actual", "Difference", "Actual entered by hand?"])
    for r in income:
        if not r["expected_cents"] and not r["actual_cents"]:
            continue
        w.writerow([r["category"], _money(r["expected_cents"]) if r["expected_cents"] else "",
                    _money(r["actual_cents"]), _money(r["difference_cents"]),
                    "yes" if r["entered_cents"] is not None else ""])
    w.writerow(["Total income", _money(sum(r["expected_cents"] for r in income)),
                _money(sum(r["actual_cents"] for r in income)),
                _money(sum(r["difference_cents"] for r in income)), ""])
    w.writerow([])

    w.writerow(["INCOME SOURCES (imported)"])
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
    for r in sources:
        w.writerow([r["category"], r["source"], _money(r["amount"])])
    w.writerow([])

    w.writerow(["SPENDING"])
    w.writerow(["Category", "Budget", "Spent", "Remaining", "% of budget used"])
    status = budget_status(db, month)
    for r in status:
        if not r["spent_cents"] and not r["budget_cents"]:
            continue
        w.writerow([
            r["category"],
            _money(r["budget_cents"]) if r["budget_cents"] else "",
            _money(r["spent_cents"]),
            _money(r["remaining_cents"]) if r["remaining_cents"] is not None else "",
            f"{r['pct'] * 100:.0f}%" if r["pct"] is not None else "",
        ])
    uncategorised = _uncategorised_spending(db, month)
    if uncategorised > 0:
        w.writerow(["Uncategorized", "", _money(uncategorised), "", ""])
    s = savings_summary(db, month, income, status)
    w.writerow(["Total spending", _money(s["budgeted_spending"]) if s["budgeted_spending"] else "",
                _money(s["actual_spending"]),
                _money(s["budgeted_spending"] - s["actual_spending"]) if s["budgeted_spending"] else "", ""])
    w.writerow([])

    w.writerow(["LEFT TO SAVE OR INVEST", "Planned", "Actual"])
    w.writerow(["Income", _money(s["expected_income"]), _money(s["actual_income"])])
    w.writerow(["Spending", _money(s["budgeted_spending"]), _money(s["actual_spending"])])
    w.writerow(["Left over", _money(s["planned_left"]), _money(s["actual_left"])])
    return out.getvalue()


def first_month_with_data(db):
    row = db.execute("SELECT MIN(substr(date, 1, 7)) FROM transactions WHERE removed = 0").fetchone()
    return row[0]
