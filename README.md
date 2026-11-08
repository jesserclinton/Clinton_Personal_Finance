# Clinton Personal Finance

A small, private budgeting web app. It imports bank CSV exports, sorts
transactions into categories, and keeps everything in a local SQLite
database so your history builds up over time. Nothing leaves your machine.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
flask --app financeapp run
```

Then open http://127.0.0.1:5000. Your data lives in `instance/finance.db`,
which is git-ignored, as are all `.csv` files outside `tests/fixtures/`.

## How categorising works

1. **Your saved rules first.** When you change a category and tick
   "remember for this merchant", future transactions from that merchant
   go to your category, even if the bank says otherwise.
2. **The bank's category next.** Truist's export includes one (the
   specific subcategory, such as Groceries, is used). New bank categories
   are created automatically.
3. **Otherwise it waits on the Review page** for you to pick a category or
   type a new one.

You can change any transaction's category on the Transactions page.
Re-importing a file, or one with an overlapping date range, skips
transactions that are already in the database.

## Getting your CSVs

- **USAA:** desktop site, open the account, Export/Download from the
  transaction history, choose CSV. About 18 months of history is available.
- **Truist:** the `ExportData.csv` export. It can include several accounts,
  including linked accounts at other banks, so import it with "Use the
  accounts named in the file" and each account is created automatically.

Column names are matched flexibly (see `ALIASES` in
`financeapp/importers.py`). The Truist format is confirmed from a real
export; the USAA format is still based on third-party guides.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Roadmap

- [x] CSV import for Truist and USAA, with duplicate skipping
- [x] Automatic categories, review queue, editable categories, merchant rules
- [ ] Monthly budgets per category with a heatmap (0–20% blue, 20–60% green,
      60–80% yellow, 80–100%+ red)
- [ ] CSV export of income and spending per category
- [ ] PDF statement import for older history
- [ ] Automated ingest (later, after the prototype)
