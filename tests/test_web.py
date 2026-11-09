import io

from .conftest import fixture_text


def upload(client, name, **form):
    data = {"file": (io.BytesIO(fixture_text(name).encode()), name), **form}
    return client.post("/import", data=data, content_type="multipart/form-data", follow_redirects=True)


def test_pages_render_empty(client):
    for path in ["/", "/import", "/review", "/transactions", "/categories"]:
        assert client.get(path).status_code == 200, path


def test_import_review_and_categorise_flow(client):
    r = upload(client, "plain_sample.csv", account_id="new", account_name="Checking", bank="other", kind="checking")
    assert b"Imported 4 new transactions" in r.data
    assert b"STARBUCKS" in r.data  # landed on the review page

    r = client.post("/transactions/1/category", data={"new_category": "Coffee", "remember": "on", "next": "/review"}, follow_redirects=True)
    assert b"applied to 1 other" in r.data
    assert b"STARBUCKS" not in r.data

    r = client.get("/transactions?month=2026-11")
    assert b"Coffee" in r.data


def test_bad_upload_shows_error(client):
    data = {"file": (io.BytesIO(b"foo,bar\n1,2\n"), "x.csv"), "account_id": "new", "account_name": "X", "bank": "other", "kind": "checking"}
    r = client.post("/import", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert b"Couldn&#39;t import" in r.data


def test_truist_export_upload_uses_accounts_in_file(client):
    r = upload(client, "truist_sample.csv", account_id="auto")
    assert b"Imported 5 new transactions" in r.data
    r = client.get("/transactions?month=2026-11")
    assert b"Groceries" in r.data and b"Paychecks/Salary" in r.data


def test_transactions_filter_by_category(client):
    upload(client, "truist_sample.csv", account_id="auto")
    r = client.get("/transactions?month=all&category=1")  # Groceries is created first
    assert b"Grocer Co" in r.data and b"Coffee House" not in r.data
    r = client.get("/transactions?month=2026-11&q=coffee")
    assert b"Coffee House" in r.data and b"Grocer Co" not in r.data
    r = client.get("/transactions?month=all&category=none")
    assert b"No transactions match" in r.data


def test_category_change_returns_to_filtered_view(client):
    upload(client, "truist_sample.csv", account_id="auto")
    r = client.post("/transactions/1/category",
                    data={"new_category": "Snacks", "next": "/transactions?month=all&category=1"})
    assert r.headers["Location"].endswith("/transactions?month=all&category=1")


def test_remove_and_restore_from_transactions_page(client):
    upload(client, "truist_sample.csv", account_id="auto")
    r = client.post("/transactions/remove", data={"tx": ["1", "2"], "action": "remove",
                                                  "next": "/transactions?month=all"}, follow_redirects=True)
    assert b"Removed 2 transaction(s)" in r.data and b"Grocer Co" not in r.data
    r = client.get("/transactions?month=all&removed=1")
    assert b"Grocer Co" in r.data and b"Restore" in r.data
    r = client.post("/transactions/remove", data={"tx": ["1"], "action": "restore",
                                                  "next": "/transactions?month=all"}, follow_redirects=True)
    assert b"Restored 1" in r.data and b"Grocer Co" in r.data


def test_undo_import_button(client):
    upload(client, "truist_sample.csv", account_id="auto")
    r = client.post("/imports/1/undo", follow_redirects=True)
    assert b"deleted its 5 transaction(s)" in r.data
    assert b"No transactions match" in client.get("/transactions?month=all").data


def test_transactions_page_has_edit_dialog_and_hidden_controls(client):
    upload(client, "truist_sample.csv", account_id="auto")
    html = client.get("/transactions?month=all").get_data(as_text=True)
    assert 'id="edit-dialog"' in html and html.count('class="icon-only secondary edit-btn"') == 5
    assert 'id="filters-panel" method="get" hidden' in html
    assert 'id="select-confirm"' in html and "hidden disabled" in html
    assert "<details>" not in html


def test_active_filter_chips(client):
    upload(client, "truist_sample.csv", account_id="auto")
    html = client.get("/transactions?month=2026-11&category=1&category=2&q=gro&direction=out").get_data(as_text=True)
    assert "November 2026" in html and "Groceries" in html and "Spending only" in html and "gro" in html
    # Removing one category chip keeps the others
    assert 'href="/transactions?month=2026-11&amp;category=2&amp;direction=out&amp;q=gro"' in html
    # Removing the month chip switches to all months
    assert 'href="/transactions?month=all&amp;category=1&amp;category=2&amp;direction=out&amp;q=gro"' in html
    assert "Clear all" in html
    plain = client.get("/transactions?month=all").get_data(as_text=True)
    assert "All months" in plain and "Clear all" not in plain


def test_transfers_excluded_from_page_totals(client):
    upload(client, "usaa_credit_sample.csv", account_id="new", account_name="USAA Visa", bank="usaa", kind="credit")
    html = client.get("/transactions?month=2026-11").get_data(as_text=True)
    assert "1 transfer(s)" in html and '<span class="tag">transfer</span>' in html
    assert "$15.49" in html  # spent
    summary = client.get("/?month=2026-11").get_data(as_text=True)
    assert "Transfers" in summary and "not counted" in summary


def test_categories_page_changes_kind(client):
    upload(client, "truist_sample.csv", account_id="auto")
    r = client.post("/categories/1/kind", data={"kind": "transfer"}, follow_redirects=True)
    assert r.status_code == 200
    assert client.post("/categories/1/kind", data={"kind": "nope"}).status_code == 400


def test_sort_headers_keep_filters_and_toggle(client):
    upload(client, "truist_sample.csv", account_id="auto")
    html = client.get("/transactions?month=all&direction=out").get_data(as_text=True)
    # Default is newest first; clicking Date flips to oldest first, other columns start A-Z / low-high
    assert 'href="/transactions?month=all&amp;direction=out&amp;sort=date&amp;dir=asc"' in html
    assert 'href="/transactions?month=all&amp;direction=out&amp;sort=description&amp;dir=asc"' in html
    html = client.get("/transactions?month=all&direction=out&sort=amount&dir=asc").get_data(as_text=True)
    assert 'aria-sort="ascending"' in html
    assert 'href="/transactions?month=all&amp;direction=out&amp;sort=amount&amp;dir=desc"' in html
    assert html.index("Landlord Llc Rent") < html.index("Grocer Co")
    # Removing a filter chip keeps the sort
    assert 'href="/transactions?month=all&amp;sort=amount&amp;dir=asc"' in html
    assert 'name="sort" value="amount"' in html


def test_budgets_page_save_and_export(client):
    upload(client, "truist_sample.csv", account_id="auto")
    assert client.get("/budgets?month=2026-11").status_code == 200
    r = client.post("/budgets", data={"month": "2026-11", "budget-1": "$10.00", "budget-3": ""}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Saved income and budgets" in html and "(1 change(s))" in html
    assert "46%" in html and "heat-green" in html   # Groceries: 4.58 of 10.00
    assert "Last 6 months" in html
    r = client.get("/export.csv?month=2026-11")
    assert r.mimetype == "text/csv" and "budget-2026-11.csv" in r.headers["Content-Disposition"]
    assert "Groceries,10.00,4.58,5.42,46%" in r.get_data(as_text=True)


def test_budgets_rejects_bad_input(client):
    upload(client, "truist_sample.csv", account_id="auto")
    r = client.post("/budgets", data={"month": "2026-11", "budget-1": "abc"}, follow_redirects=True)
    assert b"isn&#39;t an amount" in r.data
    assert client.get("/budgets?month=nope").status_code == 400


def test_budgets_apply_from_a_start_month(client):
    upload(client, "truist_sample.csv", account_id="auto")
    r = client.post("/budgets", data={"month": "2026-11", "budget-1": "10", "scope": "from",
                                      "start_month": "2026-03"}, follow_redirects=True)
    assert b"from March 2026 through November 2026" in r.data
    assert 'value="10.00"' in client.get("/budgets?month=2026-03").get_data(as_text=True)
    assert 'value="10.00"' not in client.get("/budgets?month=2026-02").get_data(as_text=True)
    r = client.post("/budgets", data={"month": "2026-11", "budget-1": "10", "scope": "from",
                                      "start_month": "2027-01"}, follow_redirects=True)
    assert b"must be on or before" in r.data


def test_budgets_page_income_and_savings(client):
    upload(client, "truist_sample.csv", account_id="auto")
    # Paychecks/Salary is category 4 in the sample (an income category)
    r = client.post("/budgets", data={"month": "2026-11", "budget-4": "3000", "actual-4": "",
                                      "new_income": "Side job", "new_income_expected": "200",
                                      "new_income_actual": "150", "budget-1": "100"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Left to save or invest" in html and "Side job" in html
    assert "$3,200.00" in html          # planned income: 3000 + 200
    assert "$3,100.00" in html          # planned left: 3200 - 100 groceries budget
    assert "$2,550.00" in html          # actual income: 2400 imported + 150 entered
    assert "entered · imported" in html


def test_heatmap_cells_link_to_filtered_transactions(client):
    upload(client, "truist_sample.csv", account_id="auto")
    client.post("/budgets", data={"month": "2026-11", "budget-1": "10"})
    html = client.get("/budgets?month=2026-11").get_data(as_text=True)
    assert 'href="/transactions?month=2026-11&amp;category=1"' in html
    assert 'href="/transactions?month=2026-08&amp;category=1"' in html   # earlier month in the grid


def test_search_box_sits_beside_filters_button(client):
    html = client.get("/transactions?month=all&q=coffee").get_data(as_text=True)
    toolbar = html[html.index('class="toolbar"'):html.index('id="filters-panel"')]
    # The search input lives in the toolbar, still submits with the filters form, and keeps its value
    assert 'id="tx-search"' in toolbar and 'form="filters-panel"' in toolbar and 'value="coffee"' in toolbar
    panel = html[html.index('id="filters-panel"'):html.index('</form>', html.index('id="filters-panel"'))]
    assert 'name="q"' not in panel
    # Live search swaps these regions in place
    assert 'id="tx-results"' in html and 'id="tx-chips"' in html


def test_edit_only_this_transaction(client, app):
    upload(client, "truist_sample.csv", account_id="auto")
    with app.app_context():
        from financeapp import db as dbmod
        db = dbmod.get_db()
        pair = db.execute("""SELECT merchant_key, MIN(id) a, MAX(id) b, MIN(category_id) c FROM transactions
                             GROUP BY merchant_key HAVING COUNT(*) > 1""").fetchone()
    assert pair, "fixture needs a merchant with two transactions"
    # The dialog sends remember=on plus only_this=on when the box is ticked
    client.post(f"/transactions/{pair['a']}/category",
                data={"new_category": "One-off", "remember": "on", "only_this": "on", "next": "/transactions"})
    with app.app_context():
        db = dbmod.get_db()
        a, b = (db.execute("SELECT c.name FROM transactions t LEFT JOIN categories c ON c.id = t.category_id WHERE t.id = ?",
                           (i,)).fetchone()[0] for i in (pair["a"], pair["b"]))
        assert a == "One-off" and b != "One-off"
        assert db.execute("SELECT COUNT(*) FROM rules WHERE merchant_key = ?", (pair["merchant_key"],)).fetchone()[0] == 0
    # Unticked, it still applies to the whole merchant
    client.post(f"/transactions/{pair['a']}/category",
                data={"new_category": "Whole merchant", "remember": "on", "next": "/transactions"})
    with app.app_context():
        db = dbmod.get_db()
        assert db.execute("SELECT COUNT(*) FROM rules WHERE merchant_key = ?", (pair["merchant_key"],)).fetchone()[0] == 1
