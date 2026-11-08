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
