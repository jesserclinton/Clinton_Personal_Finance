"""Parse bank CSV exports into a common transaction shape.

Header names below come from third-party guides, not the banks' own docs,
so each field accepts several aliases. Adjust them once real exports are
in hand.
"""
import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

# canonical field -> accepted header names (lower-cased, stripped)
ALIASES = {
    "date": ["date", "posted date", "posting date", "transaction date", "post date"],
    "description": ["description", "payee", "merchant", "name"],
    "original_description": ["original description", "memo", "details"],
    "amount": ["amount", "transaction amount"],
    "debit": ["debit", "withdrawal", "withdrawals", "debit amount"],
    "credit": ["credit", "deposit", "deposits", "credit amount"],
    "category": ["category", "transaction category"],
    "check_number": ["check number", "check #", "check no", "check"],
    "status": ["status"],
}

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"]


class ImportError_(ValueError):
    """Raised when a file can't be read as a bank export."""


@dataclass
class ParsedRow:
    date: str            # ISO YYYY-MM-DD
    description: str
    original_description: str
    amount_cents: int    # negative = money out
    bank_category: str | None


def _map_headers(headers):
    lookup = {h.strip().lower(): h for h in headers if h}
    mapping = {}
    for field, names in ALIASES.items():
        for name in names:
            if name in lookup:
                mapping[field] = lookup[name]
                break
    return mapping


def detect_bank(headers):
    """Best guess at which bank produced a file, from its headers."""
    m = _map_headers(headers)
    if "category" in m and "original_description" in m:
        return "usaa"
    if "check_number" in m or any(h.strip().lower() == "running balance" for h in headers):
        return "truist"
    return "other"


def parse_date(value):
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ImportError_(f"Unrecognised date: {value!r}")


def parse_money(value):
    """'$1,234.56', '(12.00)', '-12' -> cents. Blank -> None."""
    if value is None:
        return None
    v = value.strip().replace("$", "").replace(",", "")
    if not v:
        return None
    negative = v.startswith("(") and v.endswith(")")
    v = v.strip("()")
    try:
        cents = int((Decimal(v) * 100).quantize(Decimal("1")))
    except InvalidOperation as exc:
        raise ImportError_(f"Unrecognised amount: {value!r}") from exc
    return -cents if negative else cents


def parse_csv(text, account_kind="checking", bank=None):
    """Parse CSV text. Returns (bank, rows).

    Amounts are normalised so negative always means money out. Credit card
    exports that report charges as positive are flipped when account_kind is
    'credit' and the bank is known to do that (USAA).
    """
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportError_("The file is empty or has no header row.")
    m = _map_headers(reader.fieldnames)
    if "date" not in m or "description" not in m:
        raise ImportError_(
            "Couldn't find Date and Description columns. Headers seen: "
            + ", ".join(reader.fieldnames)
        )
    if "amount" not in m and not ("debit" in m or "credit" in m):
        raise ImportError_("Couldn't find an Amount (or Debit/Credit) column.")

    bank = bank or detect_bank(reader.fieldnames)
    flip = account_kind == "credit" and bank == "usaa"

    rows = []
    for raw in reader:
        if not any((v or "").strip() for v in raw.values()):
            continue
        if "status" in m and (raw.get(m["status"]) or "").strip().lower() == "pending":
            continue  # pending items change; wait until they post
        if "amount" in m:
            cents = parse_money(raw.get(m["amount"]))
        else:
            debit = parse_money(raw.get(m.get("debit"))) if "debit" in m else None
            credit = parse_money(raw.get(m.get("credit"))) if "credit" in m else None
            cents = (credit or 0) - abs(debit or 0)
        if cents is None:
            continue
        if flip:
            cents = -cents
        desc = (raw.get(m["description"]) or "").strip()
        orig = (raw.get(m["original_description"]) or "").strip() if "original_description" in m else ""
        cat = (raw.get(m["category"]) or "").strip() if "category" in m else ""
        rows.append(
            ParsedRow(
                date=parse_date(raw[m["date"]]),
                description=desc or orig,
                original_description=orig or desc,
                amount_cents=cents,
                bank_category=cat or None,
            )
        )
    return bank, rows


_NOISE = {
    "POS", "DEBIT", "CREDIT", "PURCHASE", "ACH", "CARD", "CHECKCARD", "CHECK",
    "RECURRING", "PAYMENT", "WITHDRAWAL", "DEPOSIT", "ONLINE", "PREAUTHORIZED",
    "VISA", "TRANSFER", "THE",
}


def merchant_key(description):
    """Reduce a bank description to a stable key for 'remember this merchant'.

    'POS DEBIT STARBUCKS #1234 RALEIGH NC' -> 'STARBUCKS RALEIGH NC'
    """
    words = re.findall(r"[A-Z][A-Z&']+", description.upper())
    words = [w for w in words if w not in _NOISE]
    return " ".join(words[:3]) or description.strip().upper()[:40]
