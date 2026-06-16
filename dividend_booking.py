"""
Turn extracted dividend vouchers into Banana double-entry bookings + an import file.

Booking convention for a Swiss-source dividend (issuer ISIN starts CH), composed
transaction sharing one Doc number:

    Debit  <bank/custody account>   Amount = net        (cash actually received)
    Debit  <Verrechnungssteuer-Guthaben>  Amount = swiss_wht   (35% reclaim, asset)
    Credit <securities income account>    Amount = gross       (Wertschriftenertrag)

    debits (net + swiss_wht) == credit (gross). No VatCode (dividends are VAT-exempt).

A dividend with NO Swiss VST (and no foreign tax) collapses to a 2-line entry
(Debit bank net / Credit income gross, net == gross).

Foreign withholding tax (US/DE/...) is NOT reclaimable as Swiss VST. v1 books the
clean Swiss case automatically; a voucher carrying foreign WHT is FLAGGED and held
back from the export so it can be booked manually (the entry would not balance with
just bank + VST debits).

The bank/custody account is chosen once per batch (a dropdown of the client's
BClass-1 accounts). The Verrechnungssteuer-Guthaben account is auto-detected from
the chart (overridable). The income account comes from the per-client learned
security map (keyed by ISIN) first, else the AI's suggestion (grounded in the live
chart of income accounts).

Output = a Banana "Transactions" import file (tab-separated, column-ID headers):
    Date  Doc  Description  AccountDebit  AccountCredit  Amount  VatCode
"""

import os
import re
import json
from datetime import datetime

SECURITY_MAP_DIR = os.environ.get("SECURITY_MAP_DIR", "/app/data/security_maps")

# Banana transactions-import columns (column IDs, the unambiguous import header).
EXPORT_COLUMNS = ["Date", "Doc", "Description", "AccountDebit", "AccountCredit", "Amount", "VatCode"]

# Net+VST vs gross may round by a rappen; tolerate a tiny gap before flagging.
BALANCE_TOL = 0.02


# --------------------------------------------------------------------------- #
# Per-client learned security map (keyed by ISIN, fallback normalized name)
# --------------------------------------------------------------------------- #

def normalize_security(name):
    """Loose key for a security name when no ISIN is available."""
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return " ".join(toks)


def security_key(isin, name):
    """ISIN is the stable key; fall back to a normalized name when absent."""
    isin = (isin or "").strip().upper()
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
        return isin
    return normalize_security(name)


def client_slug(filename):
    """Stable filesystem-safe key per client file (drops the .ac2 extension)."""
    base = re.sub(r"\.ac2$", "", filename or "", flags=re.I)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "default"


def _map_path(slug):
    return os.path.join(SECURITY_MAP_DIR, f"{slug}.json")


def load_security_map(slug):
    try:
        with open(_map_path(slug), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def lookup_security(security_map, isin, name):
    return security_map.get(security_key(isin, name))


def save_security_map(slug, smap):
    os.makedirs(SECURITY_MAP_DIR, exist_ok=True)
    with open(_map_path(slug), "w", encoding="utf-8") as f:
        json.dump(smap, f, ensure_ascii=False, indent=2)


def learn_security(slug, isin, name, income_account):
    """Remember a confirmed security→income-account mapping so it auto-fills next time."""
    smap = load_security_map(slug)
    smap[security_key(isin, name)] = {
        "income_account": str(income_account or "").strip(),
        "security_name": name,
        "isin": (isin or "").strip().upper(),
    }
    save_security_map(slug, smap)
    return smap


# --------------------------------------------------------------------------- #
# Amount / date helpers
# --------------------------------------------------------------------------- #

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


def _pos(v):
    """Positive amount or None (taxes/income are quoted as magnitudes)."""
    n = _num(v)
    return abs(n) if n is not None else None


# --------------------------------------------------------------------------- #
# Build a booking row from an extraction
# --------------------------------------------------------------------------- #

def build_booking(extraction, profile, security_map, bank_account=""):
    """
    Combine one AI extraction with the live client profile + learned security map
    into a reviewable booking row. Does NOT raise on missing data — it flags
    warnings so the review UI can surface them.
    """
    name = (extraction.get("security_name") or "").strip()
    isin = (extraction.get("isin") or "").strip().upper()
    learned = lookup_security(security_map, isin, name)

    valid_income = {a["account"] for a in profile.get("income_accounts", [])}
    warnings = []

    # Income account: learned map wins; else the AI suggestion if it's a real income account.
    account_source = "none"
    income_account = ""
    if learned and learned.get("income_account"):
        income_account = learned["income_account"]
        account_source = "map"
    else:
        suggested = str(extraction.get("suggested_account") or "").strip()
        if suggested and suggested in valid_income:
            income_account = suggested
            account_source = "ai"
        elif suggested:
            warnings.append(f"AI suggested account {suggested} which isn't an income account — review.")
    if not income_account:
        warnings.append("No income account assigned — pick one before importing.")

    wht_account = profile.get("wht_account", "")

    currency = (extraction.get("currency") or "CHF").strip().upper()
    if currency != "CHF":
        warnings.append(f"Voucher is in {currency} — v1 books CHF only; handle this one manually.")

    gross = _pos(extraction.get("gross_amount"))
    net = _pos(extraction.get("net_amount"))
    swiss_wht = _pos(extraction.get("swiss_withholding_tax")) or 0.0
    foreign_wht = _pos(extraction.get("foreign_withholding_tax")) or 0.0

    if gross is None or net is None:
        warnings.append("Could not read gross/net amounts — review.")

    if swiss_wht and not wht_account:
        warnings.append("Swiss withholding tax present but no Verrechnungssteuer-Guthaben "
                        "account found in the chart — pick one before importing.")

    if foreign_wht:
        warnings.append(f"Foreign withholding tax {foreign_wht:.2f} {currency} present — not "
                        "reclaimable as Swiss VST; this voucher is held back, book it manually.")

    # Balance check: bank(net) + VST(swiss_wht) must equal the income credit (gross).
    balances = (gross is not None and net is not None
                and abs((net + swiss_wht) - gross) <= BALANCE_TOL)
    if gross is not None and net is not None and not balances and not foreign_wht:
        warnings.append(f"Net + VST ({(net + swiss_wht):.2f}) ≠ gross ({gross:.2f}) — "
                        "amounts don't reconcile; review before importing.")

    if not extraction.get("is_dividend", True):
        warnings.append(f"Detected as '{extraction.get('doc_type', 'non-dividend')}', "
                        "not a dividend — likely skip.")

    qty = extraction.get("quantity")
    qty_txt = f"{int(qty)} " if isinstance(qty, (int, float)) and qty == int(qty) else (f"{qty} " if qty else "")
    desc = (f"DIV_{name}" + (f" {qty_txt}Stk" if qty_txt else "")).strip()

    return {
        "security": name,
        "isin": isin,
        "valor": (extraction.get("valor") or "").strip(),
        "date": (extraction.get("value_date") or "").strip(),
        "doc": "",  # assigned at export so composed rows share one Doc
        "description": desc,
        "bank_account": (bank_account or "").strip(),
        "wht_account": wht_account,
        "income_account": income_account,
        "currency": currency,
        "gross": gross,
        "net": net,
        "swiss_wht": round(swiss_wht, 2),
        "foreign_wht": round(foreign_wht, 2),
        "issuer_country": (extraction.get("issuer_country") or "").strip().upper(),
        "is_dividend": bool(extraction.get("is_dividend", True)),
        "balances": balances,
        "account_source": account_source,
        "account_reason": (extraction.get("suggested_account_reason") or "").strip(),
        "warnings": warnings,
    }


def process_dividend(filepath, profile, security_map, bank_account=""):
    """Extract one PDF and build its booking row. Imports dividend_extract lazily."""
    import dividend_extract
    extraction = dividend_extract.extract_dividend(filepath, profile.get("income_accounts"))
    return build_booking(extraction, profile, security_map, bank_account)


# --------------------------------------------------------------------------- #
# Export → Banana transactions import file (composed multi-row entries)
# --------------------------------------------------------------------------- #

def _q(v):
    """Field value safe for a tab-separated file (no embedded tabs/newlines)."""
    return "" if v is None else re.sub(r"[\t\r\n]+", " ", str(v)).strip()


def _row(date_iso, doc, desc, debit, credit, amount):
    return "\t".join([date_iso, _q(doc), _q(desc), _q(debit), _q(credit), f"{amount:.2f}", ""])


def _bookable_reason(r):
    """Return a skip reason string if the row cannot be booked, else None."""
    if r.get("skip"):
        return "marked skip"
    if not r.get("is_dividend", True):
        return "not a dividend"
    if r.get("gross") is None or r.get("net") is None:
        return "missing amounts"
    if not r.get("bank_account"):
        return "no bank account"
    if not r.get("income_account"):
        return "no income account"
    if r.get("foreign_wht"):
        return "foreign withholding tax — book manually"
    if r.get("swiss_wht") and not r.get("wht_account"):
        return "no VST-Guthaben account"
    if not r.get("balances"):
        return "does not reconcile"
    return None


def export_banana_tsv(rows):
    """
    Build a Banana 'Transactions' import file (composed entries, one Doc per voucher).
    Returns (tsv_string, included_count, skipped). Each included voucher emits 2-3
    rows sharing a 'DIV-n' Doc; included_count counts vouchers, not rows.
    """
    out = ["\t".join(EXPORT_COLUMNS)]
    included, skipped, n = 0, [], 0
    for r in rows:
        reason = _bookable_reason(r)
        if reason:
            skipped.append((r.get("security", "?"), reason))
            continue
        n += 1
        doc = f"DIV-{n}"
        date_iso = _to_iso(r.get("date"))
        desc = r.get("description") or r.get("security") or ""
        gross, net, swiss_wht = r["gross"], r["net"], r.get("swiss_wht") or 0.0

        out.append(_row(date_iso, doc, desc, r["bank_account"], "", net))
        if swiss_wht:
            out.append(_row(date_iso, doc, f"{desc} (VST 35%)", r["wht_account"], "", swiss_wht))
        out.append(_row(date_iso, doc, desc, "", r["income_account"], gross))
        included += 1
    return "\n".join(out), included, skipped
