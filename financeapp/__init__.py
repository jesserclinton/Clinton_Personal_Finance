"""Personal budgeting web app."""
import os
from datetime import date

from flask import Flask, abort, flash, redirect, render_template, request, url_for

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
        rows = ledger.month_summary(db, month)
        to_review = db.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL"
        ).fetchone()[0]
        return render_template(
            "index.html",
            month=month,
            rows=rows,
            income=sum(r["income_cents"] for r in rows),
            spent=sum(r["spent_cents"] for r in rows),
            to_review=to_review,
        )

    @app.route("/import", methods=["GET", "POST"])
    def import_view():
        db = dbmod.get_db()
        if request.method == "POST":
            account_id = request.form.get("account_id")
            if account_id == "new":
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
                result = ledger.import_csv(db, int(account_id), upload.filename, text)
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
               WHERE t.category_id IS NULL ORDER BY t.date DESC"""
        ).fetchall()
        return render_template("review.html", txs=txs, categories=categories())

    @app.route("/transactions")
    def transactions():
        month = request.args.get("month") or date.today().strftime("%Y-%m")
        txs = dbmod.get_db().execute(
            """SELECT t.*, a.name AS account, c.name AS category FROM transactions t
               JOIN accounts a ON a.id = t.account_id
               LEFT JOIN categories c ON c.id = t.category_id
               WHERE substr(t.date, 1, 7) = ? ORDER BY t.date DESC, t.id DESC""",
            (month,),
        ).fetchall()
        return render_template(
            "transactions.html", txs=txs, month=month, categories=categories()
        )

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
               LEFT JOIN transactions t ON t.category_id = c.id
               GROUP BY c.id ORDER BY c.name"""
        ).fetchall()
        rules = db.execute(
            """SELECT r.*, c.name AS category FROM rules r
               JOIN categories c ON c.id = r.category_id ORDER BY r.merchant_key"""
        ).fetchall()
        return render_template("categories.html", rows=rows, rules=rules)

    @app.post("/rules/<int:rule_id>/delete")
    def delete_rule(rule_id):
        db = dbmod.get_db()
        db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        db.commit()
        return redirect(url_for("categories_view"))

    return app
