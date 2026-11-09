"""Personal budgeting web app."""
import os
from datetime import date

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

from . import budgets as budgetmod
from . import db as dbmod
from . import ledger
from .importers import ImportError_


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FINANCEAPP_SECRET", "dev-only-local"),
        DATABASE=os.path.join(app.instance_path, "finance.db"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)

    with app.app_context():
        dbmod.init_db(dbmod.get_db())
        ledger.upgrade(dbmod.get_db())
    app.teardown_appcontext(dbmod.close_db)

    @app.template_filter("money")
    def money(cents):
        cents = cents or 0
        sign = "-" if cents < 0 else ""
        return f"{sign}${abs(cents) / 100:,.2f}"

    def categories():
        return dbmod.get_db().execute("SELECT * FROM categories ORDER BY name").fetchall()

    def resolve_category(form):
        """Use a chosen category, or create the new one typed in."""
        new_name = (form.get("new_category") or "").strip()
        if new_name:
            return ledger.get_or_create_category(dbmod.get_db(), new_name)
        cid = form.get("category_id")
        if not cid:
            abort(400, "Pick a category or type a new one.")
        return int(cid)

    @app.route("/")
    def index():
        month = request.args.get("month") or date.today().strftime("%Y-%m")
        db = dbmod.get_db()
        all_rows = ledger.month_summary(db, month)
        rows = [r for r in all_rows if ledger.counts_in_totals(r)]
        transfers = [r for r in all_rows if not ledger.counts_in_totals(r)]
        to_review = db.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL AND removed = 0"
        ).fetchone()[0]
        return render_template(
            "index.html",
            month=month,
            rows=rows,
            transfers=transfers,
            income=sum(r["income_cents"] for r in rows),
            spent=sum(r["spent_cents"] for r in rows),
            to_review=to_review,
        )

    def _month_arg():
        month = request.values.get("month") or date.today().strftime("%Y-%m")
        try:
            date.fromisoformat(month + "-01")
        except ValueError:
            abort(400, "Month should look like 2026-11")
        return month

    @app.route("/budgets", methods=["GET", "POST"])
    def budgets_view():
        db = dbmod.get_db()
        month = _month_arg()
        if request.method == "POST":
            amounts = {}
            for key, value in request.form.items():
                if not key.startswith("budget-"):
                    continue
                cid = int(key.removeprefix("budget-"))
                value = value.strip().replace("$", "").replace(",", "")
                try:
                    amounts[cid] = round(float(value) * 100) if value else 0
                except ValueError:
                    flash(f"\"{value}\" isn't an amount.", "error")
                    return redirect(url_for("budgets_view", month=month))
                if amounts[cid] < 0:
                    amounts[cid] = 0
            n = budgetmod.set_budgets(db, month, amounts)
            flash(f"Saved {n} budget change(s) from {budgetmod.month_label(month)} onward.", "ok")
            return redirect(url_for("budgets_view", month=month))
        status = budgetmod.budget_status(db, month)
        months, history = budgetmod.heatmap_history(db, month)
        budgeted = [r for r in status if r["budget_cents"]]
        return render_template(
            "budgets.html",
            month=month,
            month_name=budgetmod.month_label(month),
            prev_month=budgetmod.month_add(month, -1),
            next_month=budgetmod.month_add(month, 1),
            status=status,
            budgeted=budgeted,
            total_budget=sum(r["budget_cents"] for r in budgeted),
            total_budgeted_spent=sum(r["spent_cents"] for r in budgeted),
            months=months,
            month_short=lambda m: budgetmod.month_label(m, "%b %Y"),
            history=history,
            levels=budgetmod.LEVELS,
        )

    @app.route("/export.csv")
    def export_csv():
        month = _month_arg()
        body = budgetmod.export_month_csv(dbmod.get_db(), month)
        return Response(
            body,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=budget-{month}.csv"},
        )

    @app.route("/import", methods=["GET", "POST"])
    def import_view():
        db = dbmod.get_db()
        if request.method == "POST":
            account_id = request.form.get("account_id")
            if account_id == "auto":
                account_id = None
            elif account_id == "new":
                name = request.form.get("account_name", "").strip()
                if not name:
                    flash("Give the new account a name.", "error")
                    return redirect(url_for("import_view"))
                cur = db.execute(
                    "INSERT INTO accounts (name, bank, kind) VALUES (?, ?, ?)",
                    (name, request.form.get("bank", "other"), request.form.get("kind", "checking")),
                )
                db.commit()
                account_id = cur.lastrowid
            upload = request.files.get("file")
            if not upload or not upload.filename:
                flash("Choose a CSV file to import.", "error")
                return redirect(url_for("import_view"))
            try:
                text = upload.read().decode("utf-8-sig")
                result = ledger.import_csv(
                    db, int(account_id) if account_id else None, upload.filename, text
                )
            except (ImportError_, UnicodeDecodeError, ValueError) as exc:
                db.rollback()
                flash(f"Couldn't import {upload.filename}: {exc}", "error")
                return redirect(url_for("import_view"))
            flash(
                f"Imported {result['added']} new transactions"
                f" ({result['skipped']} already in the database were skipped).",
                "ok",
            )
            return redirect(url_for("review"))
        accounts = db.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        history = db.execute(
            """SELECT i.*, a.name AS account FROM imports i JOIN accounts a ON a.id = i.account_id
               ORDER BY i.id DESC LIMIT 20"""
        ).fetchall()
        return render_template("import.html", accounts=accounts, history=history)

    @app.route("/review")
    def review():
        txs = dbmod.get_db().execute(
            """SELECT t.*, a.name AS account FROM transactions t
               JOIN accounts a ON a.id = t.account_id
               WHERE t.category_id IS NULL AND t.removed = 0 ORDER BY t.date DESC"""
        ).fetchall()
        return render_template("review.html", txs=txs, categories=categories())

    @app.route("/transactions")
    def transactions():
        args = request.args
        # A month is shown by default; "all" (or a blank month) means every month.
        month = args.get("month", date.today().strftime("%Y-%m"))
        month = "" if month == "all" else month
        picked = args.getlist("category")
        cat_ids = [int(c) for c in picked if c.isdigit()]
        filters = dict(
            month=month or None,
            category_ids=cat_ids,
            uncategorised="none" in picked,
            account_id=args.get("account", type=int),
            direction=args.get("direction") or None,
            search=(args.get("q") or "").strip() or None,
            removed=args.get("removed") == "1",
        )
        sort = args.get("sort") if args.get("sort") in ledger.SORTS else "date"
        direction = args.get("dir")
        descending = (direction == "desc") if direction in ("asc", "desc") else ledger.DEFAULT_DESCENDING[sort]
        # Current query, so sort links keep the filters and filter links keep the sort
        query = {
            "month": month or "all",
            "category": list(picked),
            "account": filters["account_id"],
            "direction": filters["direction"],
            "q": filters["search"],
            "removed": "1" if filters["removed"] else None,
            "sort": sort if sort != "date" or not descending else None,
            "dir": ("desc" if descending else "asc") if (sort != "date" or not descending) else None,
        }

        def sort_url(col):
            # Clicking the active column flips it; a new column starts in its natural order
            desc = (not descending) if col == sort else ledger.DEFAULT_DESCENDING[col]
            params = {**query, "sort": col, "dir": "desc" if desc else "asc"}
            return url_for("transactions", **{k: v for k, v in params.items() if v})

        db = dbmod.get_db()
        txs = ledger.filter_transactions(db, **filters, sort=sort, descending=descending)
        counted = [t for t in txs if ledger.counts_in_totals(t)]
        accounts = db.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        return render_template(
            "transactions.html",
            txs=txs,
            month=month,
            picked=picked,
            filters=filters,
            categories=categories(),
            accounts=accounts,
            total_in=sum(t["amount_cents"] for t in counted if t["amount_cents"] > 0),
            total_out=sum(-t["amount_cents"] for t in counted if t["amount_cents"] < 0),
            n_transfers=len(txs) - len(counted),
            next_url=request.full_path,
            chips=_filter_chips(query, month, picked, filters, categories(), accounts),
            sort=sort,
            descending=descending,
            sort_url=sort_url,
            query=query,
        )

    def _filter_chips(base, month, picked, filters, cats, accounts):
        """(label, url without that filter) for each active filter."""

        def without(key, value=None):
            params = {**base, "category": list(base["category"])}
            if key == "category":
                params["category"].remove(value)
            else:
                params[key] = "all" if key == "month" else None
            return url_for("transactions", **{k: v for k, v in params.items() if v})

        names = {str(c["id"]): c["name"] for c in cats}
        names["none"] = "Uncategorized"
        acct_names = {a["id"]: a["name"] for a in accounts}
        chips = []
        if month:
            try:
                label = date.fromisoformat(month + "-01").strftime("%B %Y")
            except ValueError:
                label = month
            chips.append(("Month", label, without("month")))
        for c in picked:
            chips.append(("Category", names.get(c, c), without("category", c)))
        if filters["account_id"]:
            chips.append(("Account", acct_names.get(filters["account_id"], "?"), without("account")))
        if filters["direction"]:
            chips.append(("Type", "Spending only" if filters["direction"] == "out" else "Income only",
                          without("direction")))
        if filters["search"]:
            chips.append(("Search", f"\u201c{filters['search']}\u201d", without("q")))
        if filters["removed"]:
            chips.append(("Showing", "Removed transactions", without("removed")))
        return chips

    @app.post("/transactions/<int:tx_id>/category")
    def change_category(tx_id):
        db = dbmod.get_db()
        cid = resolve_category(request.form)
        remember = request.form.get("remember") == "on"
        try:
            others = ledger.set_category(db, tx_id, cid, remember=remember)
        except ValueError:
            abort(404)
        msg = "Category updated."
        if remember:
            msg += f" Saved for this merchant and applied to {others} other transaction(s)."
        flash(msg, "ok")
        return redirect(request.form.get("next") or url_for("review"))

    @app.post("/transactions/remove")
    def remove_transactions():
        ids = request.form.getlist("tx")
        restore = request.form.get("action") == "restore"
        n = ledger.set_removed(dbmod.get_db(), ids, removed=not restore)
        if not ids:
            flash("Tick the transactions first.", "error")
        elif restore:
            flash(f"Restored {n} transaction(s).", "ok")
        else:
            flash(f"Removed {n} transaction(s). They won't count in any totals and "
                  "won't come back if you import the same dates again. "
                  "Tick \"Show removed\" to restore them.", "ok")
        return redirect(request.form.get("next") or url_for("transactions"))

    @app.post("/imports/<int:import_id>/undo")
    def undo_import(import_id):
        n = ledger.undo_import(dbmod.get_db(), import_id)
        flash(f"Undid that import and deleted its {n} transaction(s).", "ok")
        return redirect(url_for("import_view"))

    @app.route("/categories", methods=["GET", "POST"])
    def categories_view():
        db = dbmod.get_db()
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if name:
                ledger.get_or_create_category(db, name, request.form.get("kind", "expense"))
                db.commit()
            return redirect(url_for("categories_view"))
        rows = db.execute(
            """SELECT c.*, COUNT(t.id) AS n FROM categories c
               LEFT JOIN transactions t ON t.category_id = c.id AND t.removed = 0
               GROUP BY c.id ORDER BY c.name"""
        ).fetchall()
        rules = db.execute(
            """SELECT r.*, c.name AS category FROM rules r
               JOIN categories c ON c.id = r.category_id ORDER BY r.merchant_key"""
        ).fetchall()
        return render_template("categories.html", rows=rows, rules=rules)

    @app.post("/categories/<int:category_id>/kind")
    def change_category_kind(category_id):
        try:
            ledger.set_category_kind(dbmod.get_db(), category_id, request.form.get("kind"))
        except ValueError:
            abort(400)
        return redirect(url_for("categories_view"))

    @app.post("/rules/<int:rule_id>/delete")
    def delete_rule(rule_id):
        db = dbmod.get_db()
        db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        db.commit()
        return redirect(url_for("categories_view"))

    return app
