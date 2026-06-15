"""
Turn extracted AP invoices into Banana double-entry accrual bookings + an import file.

Booking convention (mirrors Cadorit's existing books, verified against a real 2025
"Miete" entry):

    Debit  <expense account>   /   Credit  202000 (Kreditoren)   |   gross amount
    VatCode = input-VAT code (e.g. I81 = Vorsteuer 8.1%); blank for VAT-exempt
              (social insurance, BVG/pension). Banana computes the VAT split itself.

Each invoice = ONE transaction row. The expense account comes from the per-client
learned vendor map first, else the AI's suggestion (grounded in the live chart).
The credit account, VAT codes and account list all come per-client from banana_live.

Output = a Banana "Transactions" import file (tab-separated, column-ID headers):
    Date  Doc  Description  AccountDebit  AccountCredit  Amount  VatCode
"""

import os
import re
import json
from datetime import datetime

VENDOR_MAP_DIR = os.environ.get("VENDOR_MAP_DIR", "/app/data/vendor_maps")

# Banana transactions-import columns (column IDs, the unambiguous import header).
EXPORT_COLUMNS = ["Date", "Doc", "Description", "AccountDebit", "AccountCredit", "Amount", "VatCode"]

_LEGAL_SUFFIXES = {"ag", "gmbh", "sa", "sarl", "ltd", "inc", "llc", "kg", "co", "gruppe", "group"}


# --------------------------------------------------------------------------- #
# Vendor normalization + per-client learned map
# --------------------------------------------------------------------------- #

def normalize_vendor(name):
    """Loose key so 'Swisscom (Schweiz) AG' and 'Swisscom AG' map to the same vendor."""
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    toks = [t for t in toks if t not in _LEGAL_SUFFIXES]
    return " ".join(toks)


def client_slug(filename):
    """Stable filesystem-safe key per client file (drops the .ac2 extension)."""
    base = re.sub(r"\.ac2$", "", filename or "", flags=re.I)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "default"


def _map_path(slug):
    return os.path.join(VENDOR_MAP_DIR, f"{slug}.json")


def load_vendor_map(slug):
    try:
        with open(_map_path(slug), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def lookup_vendor(vendor_map, vendor_name):
    """
    Find a learned mapping for this vendor. Exact normalized match first; else a
    conservative token-prefix match so trivial name variants resolve to the same
    vendor (e.g. 'Swisscom AG' ↔ 'Swisscom (Schweiz) AG' → 'swisscom' is a prefix
    of 'swisscom schweiz'). Requires the first token to match, so distinct vendors
    don't collide. Returns the mapping dict or None.
    """
    vkey = normalize_vendor(vendor_name)
    if not vkey:
        return None
    if vkey in vendor_map:
        return vendor_map[vkey]
    vtoks = vkey.split()
    for k, v in vendor_map.items():
        ktoks = k.split()
        if not ktoks or ktoks[0] != vtoks[0]:
            continue
        short, long = sorted((vtoks, ktoks), key=len)
        if long[:len(short)] == short:  # one is a token-prefix of the other
            return v
    return None


def save_vendor_map(slug, vmap):
    os.makedirs(VENDOR_MAP_DIR, exist_ok=True)
    with open(_map_path(slug), "w", encoding="utf-8") as f:
        json.dump(vmap, f, ensure_ascii=False, indent=2)


def learn_vendor(slug, vendor_name, account, vatcode):
    """Remember a confirmed vendor→account+VAT mapping so it auto-fills next time."""
    vmap = load_vendor_map(slug)
    vmap[normalize_vendor(vendor_name)] = {
        "account": str(account or "").strip(),
        "vatcode": str(vatcode or "").strip(),
        "vendor_name": vendor_name,
    }
    save_vendor_map(slug, vmap)
    return vmap


# --------------------------------------------------------------------------- #
# VAT code + amount helpers
# --------------------------------------------------------------------------- #

def _norm_rate(rate):
    try:
        return round(float(rate), 1)
    except (TypeError, ValueError):
        return None


def pick_vat_code(rate, input_vat_codes, prefer="I"):
    """
    Map a VAT rate to one of the client's actual input-VAT codes. Prefer I-codes
    (Investition & Betriebsaufwand — what Cadorit uses for operating expenses);
    fall back to M-codes (Material/Dienstleistung). Returns '' for no/zero VAT.
    """
    r = _norm_rate(rate)
    if not r:
        return ""
    candidates = [c for c in (input_vat_codes or []) if _norm_rate(c.get("rate")) == r]
    if not candidates:
        return ""
    for pref in (prefer, "M" if prefer == "I" else "I"):
        for c in candidates:
            if c["code"].upper().startswith(pref):
                return c["code"]
    return candidates[0]["code"]


def _to_iso(date_str):
    """DD.MM.YYYY (or already-ISO) -> YYYY-MM-DD for Banana import. Passthrough on miss."""
    s = str(date_str or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Build a booking row from an extraction
# --------------------------------------------------------------------------- #

def build_booking(extraction, profile, vendor_map):
    """
    Combine one AI extraction with the live client profile + learned vendor map into
    a reviewable booking row. Does NOT raise on missing data — it flags warnings so
    the review UI can surface them.
    """
    vendor = (extraction.get("vendor_name") or "").strip()
    learned = lookup_vendor(vendor_map, vendor)

    valid_accounts = {a["account"] for a in profile.get("expense_accounts", [])}
    warnings = []

    # Account: learned map wins; else the AI suggestion if it's a real expense account.
    account_source = "none"
    account = ""
    if learned and learned.get("account"):
        account = learned["account"]
        account_source = "map"
    else:
        suggested = str(extraction.get("suggested_account") or "").strip()
        if suggested and suggested in valid_accounts:
            account = suggested
            account_source = "ai"
        elif suggested:
            warnings.append(f"AI suggested account {suggested} which isn't an expense account — review.")
    if not account:
        warnings.append("No expense account assigned — pick one before importing.")

    # VAT code: learned override wins; else derive from the rate.
    rate = extraction.get("vat_rate")
    if learned and "vatcode" in learned:
        vatcode = learned["vatcode"]
    else:
        vatcode = pick_vat_code(rate, profile.get("input_vat_codes"))

    currency = (extraction.get("currency") or "CHF").strip().upper()
    if currency != "CHF":
        warnings.append(f"Invoice is in {currency} — v1 books CHF only; handle this one manually.")

    gross = _num(extraction.get("total_gross"))
    if gross is None:
        warnings.append("Could not read a total amount — review.")

    if not extraction.get("is_invoice", True):
        warnings.append(f"Detected as '{extraction.get('doc_type', 'non-invoice')}', not an invoice — likely skip.")

    desc = (extraction.get("description") or "").strip() or vendor
    if desc and not desc.startswith("RG_"):   # RG_ = Rechnung (invoice) marker on every booking
        desc = "RG_" + desc

    return {
        "vendor": vendor,
        "date": (extraction.get("invoice_date") or "").strip(),
        "due_date": (extraction.get("due_date") or "").strip(),
        "doc": (extraction.get("invoice_number") or "").strip(),
        "description": desc,
        "account_debit": account,
        "account_credit": profile.get("ap_account", "202000"),
        "amount": gross,
        "vatcode": vatcode,
        "currency": currency,
        "vat_rate": rate,
        "reference": (extraction.get("reference") or "").strip(),
        "is_invoice": bool(extraction.get("is_invoice", True)),
        "account_source": account_source,
        "account_reason": (extraction.get("suggested_account_reason") or "").strip(),
        "warnings": warnings,
    }


def process_invoice(filepath, profile, vendor_map):
    """Extract one PDF and build its booking row. Imports invoice_extract lazily."""
    import invoice_extract
    extraction = invoice_extract.extract_invoice(filepath, profile.get("expense_accounts"))
    return build_booking(extraction, profile, vendor_map)


# --------------------------------------------------------------------------- #
# Export → Banana transactions import file
# --------------------------------------------------------------------------- #

def _q(v):
    """Field value safe for a tab-separated file (no embedded tabs/newlines)."""
    return "" if v is None else re.sub(r"[\t\r\n]+", " ", str(v)).strip()


def export_banana_tsv(rows, include_non_invoices=False):
    """
    Build a Banana 'Transactions' import file (tab-separated, column-ID headers).
    Skips rows with no amount or no debit account (can't be booked) and, by default,
    rows flagged as non-invoices. Returns (tsv_string, included_count, skipped).
    """
    out = ["\t".join(EXPORT_COLUMNS)]
    included, skipped = 0, []
    for r in rows:
        if r.get("skip"):
            skipped.append((r.get("vendor", "?"), "marked skip"))
            continue
        if not include_non_invoices and not r.get("is_invoice", True):
            skipped.append((r.get("vendor", "?"), "not an invoice"))
            continue
        if r.get("amount") is None:
            skipped.append((r.get("vendor", "?"), "no amount"))
            continue
        if not r.get("account_debit"):
            skipped.append((r.get("vendor", "?"), "no expense account"))
            continue
        out.append("\t".join([
            _to_iso(r.get("date")),
            _q(r.get("doc")),
            _q(r.get("description")),
            _q(r.get("account_debit")),
            _q(r.get("account_credit")),
            f"{r['amount']:.2f}",
            _q(r.get("vatcode")),
        ]))
        included += 1
    return "\n".join(out), included, skipped
