import csv
import io

from financeapp import budgets, ledger

from .conftest import make_account


def add(db, acct, date, desc, cents, category):
    cid = ledger.get_or_create_category(db, category, "income" if cents > 0 and category == "Paycheck" else None)
    n = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    db.execute(
        """INSERT INTO transactions (account_id, date, description, amount_cents, category_id,
           category_source, merchant_key, fingerprint) VALUES (?, ?, ?, ?, ?, 'user', ?, ?)""",
        (acct, date, desc, cents, cid, desc.upper(), f"fp{n}"),
    )
    db.commit()
    return cid


def test_heat_levels():
    assert budgets.heat_level(None) is None
    assert budgets.heat_level(0) == "blue"
    assert budgets.heat_level(0.19) == "blue"
    assert budgets.heat_level(0.2) == "green"
    assert budgets.heat_level(0.59) == "green"
    assert budgets.heat_level(0.6) == "yellow"
    assert budgets.heat_level(0.8) == "red"
    assert budgets.heat_level(1.5) == "red"


def test_month_add():
    assert budgets.month_add("2026-11", 1) == "2026-12"
    assert budgets.month_add("2026-12", 1) == "2027-01"
    assert budgets.month_add("2026-01", -1) == "2025-12"
    assert budgets.month_add("2026-11", -5) == "2026-06"


def test_budgets_carry_forward_and_change(db):
    acct = make_account(db, "Checking", "other")
    groceries = add(db, acct, "2026-10-05", "Kroger", -5000, "Groceries")
    budgets.set_budgets(db, "2026-09", {groceries: 40000})
    assert budgets.effective_budgets(db, "2026-08") == {}
    assert budgets.effective_budgets(db, "2026-12") == {groceries: 40000}
    budgets.set_budgets(db, "2026-11", {groceries: 30000})
    assert budgets.effective_budgets(db, "2026-10") == {groceries: 40000}
    assert budgets.effective_budgets(db, "2027-01") == {groceries: 30000}
    budgets.set_budgets(db, "2027-02", {groceries: 0})          # stop budgeting
    assert budgets.effective_budgets(db, "2027-03") == {}
    assert budgets.set_budgets(db, "2027-03", {groceries: 0}) == 0  # no-op


def test_budget_status_uses_net_spending_and_skips_transfers(db):
    acct = make_account(db, "Checking", "other")
    g = add(db, acct, "2026-11-02", "Kroger", -30000, "Groceries")
    add(db, acct, "2026-11-03", "Kroger refund", 5000, "Groceries")
    d = add(db, acct, "2026-11-04", "Wendys", -9000, "Fast Food")
    add(db, acct, "2026-11-05", "Payment to USAA", -50000, "Credit Card Payment")   # transfer
    budgets.set_budgets(db, "2026-11", {g: 50000, d: 10000})
    rows = {r["category"]: r for r in budgets.budget_status(db, "2026-11")}
    assert "Credit Card Payment" not in rows
    assert rows["Groceries"]["spent_cents"] == 25000 and rows["Groceries"]["level"] == "green"   # 50%
    assert rows["Fast Food"]["level"] == "red" and rows["Fast Food"]["remaining_cents"] == 1000  # 90%
    assert list(rows)[0] == "Fast Food"   # most used first


def test_heatmap_history(db):
    acct = make_account(db, "Checking", "other")
    g = add(db, acct, "2026-10-02", "Kroger", -10000, "Groceries")
    add(db, acct, "2026-11-02", "Kroger", -45000, "Groceries")
    budgets.set_budgets(db, "2026-10", {g: 50000})
    months, rows = budgets.heatmap_history(db, "2026-11", months=3)
    assert months == ["2026-09", "2026-10", "2026-11"]
    cells = rows[0]["cells"]
    assert [c["level"] for c in cells] == [None, "green", "red"]


def test_export_csv(db):
    acct = make_account(db, "Checking", "other")
    add(db, acct, "2026-11-01", "Acme Payroll", 200000, "Paycheck")
    g = add(db, acct, "2026-11-02", "Kroger", -30000, "Groceries")
    add(db, acct, "2026-11-05", "Payment to USAA", -50000, "Credit Card Payment")
    db.execute("""INSERT INTO transactions (account_id, date, description, amount_cents, merchant_key, fingerprint)
                  VALUES (?, '2026-11-06', 'Mystery', -1000, 'M', 'fpx')""", (acct,))
    db.commit()
    budgets.set_budgets(db, "2026-11", {g: 40000})
    pay = db.execute("SELECT id FROM categories WHERE name='Paycheck'").fetchone()[0]
    budgets.set_budgets(db, "2026-11", {pay: 250000})
    rows = list(csv.reader(io.StringIO(budgets.export_month_csv(db, "2026-11"))))
    assert rows[0] == ["Month: November 2026"]
    assert ["Paycheck", "2500.00", "2000.00", "-500.00", ""] in rows
    assert ["Paycheck", "Acme Payroll", "2000.00"] in rows
    assert ["Total income", "2500.00", "2000.00", "-500.00", ""] in rows
    assert ["Groceries", "400.00", "300.00", "100.00", "75%"] in rows
    assert ["Uncategorized", "", "10.00", "", ""] in rows
    assert ["Total spending", "400.00", "310.00", "90.00", ""] in rows
    assert ["Left over", "2100.00", "1690.00"] in rows
    assert not any(r and r[0] == "Credit Card Payment" for r in rows)


def test_apply_budget_from_an_earlier_month(db):
    acct = make_account(db, "Checking", "other")
    g = add(db, acct, "2026-06-05", "Kroger", -5000, "Groceries")
    budgets.set_budgets(db, "2026-09", {g: 40000})
    budgets.set_budgets(db, "2027-01", {g: 45000})          # a later change stays
    budgets.set_budgets(db, "2026-11", {g: 50000}, start_month="2026-07")
    assert budgets.effective_budgets(db, "2026-06") == {}                # before the start: unchanged
    assert budgets.effective_budgets(db, "2026-07") == {g: 50000}
    assert budgets.effective_budgets(db, "2026-09") == {g: 50000}        # replaced the Sep budget
    assert budgets.effective_budgets(db, "2026-12") == {g: 50000}
    assert budgets.effective_budgets(db, "2027-02") == {g: 45000}        # later change kept
    assert budgets.first_month_with_data(db) == "2026-06"
    # Clearing from a start month removes the budget for that range only
    budgets.set_budgets(db, "2026-11", {g: 0}, start_month="2026-10")
    assert budgets.effective_budgets(db, "2026-09") == {g: 50000}
    assert budgets.effective_budgets(db, "2026-10") == {}
    assert budgets.effective_budgets(db, "2027-02") == {g: 45000}


def test_income_expected_actual_and_savings(db):
    acct = make_account(db, "Checking", "other")
    pay = add(db, acct, "2026-11-01", "Acme Payroll", 200000, "Paycheck")
    side = ledger.get_or_create_category(db, "Side job", "income")
    g = add(db, acct, "2026-11-02", "Kroger", -30000, "Groceries")
    budgets.set_budgets(db, "2026-11", {pay: 400000, side: 50000, g: 50000})
    budgets.set_income_actuals(db, "2026-11", {side: 60000})       # paid outside imported accounts
    rows = {r["category"]: r for r in budgets.income_status(db, "2026-11")}
    assert rows["Paycheck"]["actual_cents"] == 200000 and rows["Paycheck"]["difference_cents"] == -200000
    assert rows["Side job"]["actual_cents"] == 60000 and rows["Side job"]["entered_cents"] == 60000
    s = budgets.savings_summary(db, "2026-11")
    assert s["planned_left"] == 450000 - 50000
    assert s["actual_left"] == 260000 - 30000
    # Expected income carries forward; entered actuals don't
    assert {r["category"]: r["expected_cents"] for r in budgets.income_status(db, "2026-12")}["Paycheck"] == 400000
    assert {r["category"]: r["entered_cents"] for r in budgets.income_status(db, "2026-12")}["Side job"] is None
    budgets.set_income_actuals(db, "2026-11", {side: None})        # clear the override
    assert {r["category"]: r["actual_cents"] for r in budgets.income_status(db, "2026-11")}["Side job"] == 0


def test_over_budget_red_darkens():
    assert budgets.over_budget_style(None) == ""
    assert budgets.over_budget_style(0.9) == ""
    assert budgets.over_budget_style(1.0) == ""
    at_110 = budgets.over_budget_style(1.1)
    at_150 = budgets.over_budget_style(1.5)
    at_200 = budgets.over_budget_style(2.0)
    assert at_200 == budgets.over_budget_style(3.5) == \
        "background:linear-gradient(145deg, #451613 0%, #33100e 100%)"
    lum = lambda style: sum(int(style[style.index("#") + i:style.index("#") + i + 2], 16) for i in (1, 3, 5))
    assert lum(at_110) > lum(at_150) > lum(at_200)
