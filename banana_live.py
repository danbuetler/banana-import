"""
Read-only client for the Banana Accounting webserver (API v2).

When Daniel works on a client, that client's .ac2 file is open in Banana, which
exposes a local HTTPS webserver. This module reads the LIVE chart of accounts and
VAT codes for the active client so the invoice booker can propose real account
numbers and VAT codes per client (they differ per client — that's the whole point).

Endpoints (token auth, self-signed cert):
    GET {BASE}/v2/docs                                  -> JSON list of open .ac2 files
    GET {BASE}/v2/doc/{file}/table/Accounts/rows        -> HTML table (chart of accounts)
    GET {BASE}/v2/doc/{file}/table/VatCodes/rows        -> HTML table (VAT codes)

Config (env, set in .env):
    BANANA_BASE_URL   default https://host.docker.internal:8089  (host from inside Docker)
    BANANA_TOKEN      the acstkn access token (Banana > webserver settings)

Stdlib only (urllib) — no extra dependency. Cert verification is disabled because
Banana's localhost webserver uses a self-signed certificate (same as banana-mcp).
"""

import os
import re
import ssl
import json
import html
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("BANANA_BASE_URL", "https://host.docker.internal:8089").rstrip("/")
TOKEN = os.environ.get("BANANA_TOKEN", "")
API_VER = "v2"

# Default Swiss-KMU collective creditors (AP) control account; overridable per client.
DEFAULT_AP_ACCOUNT = "202000"

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class BananaUnavailable(RuntimeError):
    """Raised when the Banana webserver can't be reached or isn't configured."""


def available():
    return bool(TOKEN)


def _get(path):
    if not TOKEN:
        raise BananaUnavailable(
            "BANANA_TOKEN is not set — open Banana, enable its webserver, and put the "
            "access token in banana-import/.env (BANANA_TOKEN)."
        )
    url = f"{BASE_URL}/{API_VER}/{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}acstkn={urllib.parse.quote(TOKEN)}"
    try:
        with urllib.request.urlopen(url, context=_SSL_CTX, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("content-type", "")
    except Exception as e:  # noqa: BLE001 — surface any transport error uniformly
        raise BananaUnavailable(
            f"Could not reach the Banana webserver at {BASE_URL}. Is Banana open with the "
            f"client's file and the webserver enabled? ({e})"
        )
    return body, ctype


def _doc_path(filename, path):
    return f"doc/{urllib.parse.quote(filename)}/{path}"


def _parse_html_rows(body):
    """
    Banana's /table/{name}/rows returns an HTML table. Map each <tbody> <tr> to a
    dict keyed by the first <thead> row's <th> column names. Robust to the empty
    second header row and to cells containing nested markup / &nbsp;.
    """
    thead = re.search(r"<thead>(.*?)</thead>", body, re.S)
    headers = []
    if thead:
        first_row = re.search(r"<tr>(.*?)</tr>", thead.group(1), re.S)
        if first_row:
            headers = [
                html.unescape(re.sub(r"<.*?>", "", c)).replace("\xa0", " ").strip()
                for c in re.findall(r"<th>(.*?)</th>", first_row.group(1), re.S)
            ]
    tbody = re.search(r"<tbody>(.*?)</tbody>", body, re.S)
    rows = []
    if tbody and headers:
        for tr in re.findall(r"<tr>(.*?)</tr>", tbody.group(1), re.S):
            cells = [
                html.unescape(re.sub(r"<.*?>", "", c)).replace("\xa0", " ").strip()
                for c in re.findall(r"<td>(.*?)</td>", tr, re.S)
            ]
            if not cells:
                continue
            rows.append({headers[i]: cells[i] for i in range(min(len(headers), len(cells)))})
    return rows


def list_open_files():
    """Return the list of .ac2 files currently open in Banana."""
    body, ctype = _get("docs")
    if "json" in ctype:
        data = json.loads(body)
        if isinstance(data, list):
            return [str(x) for x in data]
        if isinstance(data, dict):
            # Some builds wrap the list, e.g. {"documents": [...]}.
            for v in data.values():
                if isinstance(v, list):
                    return [str(x) for x in v]
    # Fallback: scrape any .ac2 names out of an HTML/text response.
    return sorted(set(re.findall(r"[^\s\"'<>]+\.ac2", body)))


def _parse_amount(s):
    """Banana Balance cell -> float (or None). Handles apostrophe thousands separators."""
    s = (s or "").strip().replace("'", "").replace("’", "").replace(" ", "")
    if not s:
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def get_accounts(filename):
    """
    Return the chart of accounts as a list of dicts:
        {account, description, bclass, vatcode, group, balance}
    balance = the account's current book balance (float, or None if blank).
    Only real account rows (numeric Account) are returned — group/total rows dropped.
    """
    body, _ = _get(_doc_path(filename, "table/Accounts/rows"))
    out = []
    for r in _parse_html_rows(body):
        acct = (r.get("Account") or "").strip()
        if not re.fullmatch(r"\d{3,10}", acct):
            continue
        out.append({
            "account": acct,
            "description": (r.get("Description") or "").strip(),
            "bclass": (r.get("BClass") or "").strip(),
            "vatcode": (r.get("VatCode") or "").strip(),
            "group": (r.get("Gr") or "").strip(),
            "balance": _parse_amount(r.get("Balance")),
        })
    return out


def get_vat_codes(filename):
    """
    Return defined VAT codes as a list of dicts: {code, description, rate}.
    Only rows that actually carry a VatCode are returned (skips section headings).
    """
    body, _ = _get(_doc_path(filename, "table/VatCodes/rows"))
    out = []
    for r in _parse_html_rows(body):
        code = (r.get("VatCode") or "").strip()
        if not code:
            continue
        rate = (r.get("VatRate") or "").strip()
        out.append({
            "code": code,
            "description": (r.get("Description") or "").strip(),
            "rate": rate,
        })
    return out


def _detect_wht_account(accounts):
    """
    Find the Verrechnungssteuer-Guthaben (Swiss withholding-tax reclaim) account:
    a BClass-1 asset whose description names withholding / Verrechnungssteuer. The
    reclaim is a receivable FROM the federal tax admin, so the account is often
    named 'ESTV Withholding Tax' — keep ESTV. Only exclude the VAT-side control
    accounts (MwSt / VAT / clearing). It is a default guess (editable in the UI).
    """
    for a in accounts:
        if a["bclass"] != "1":
            continue
        d = a["description"].lower()
        if ("verrechnungssteuer" in d or "withholding" in d or "anticipatory" in d) \
                and "mwst" not in d and "vat" not in d and "clearing" not in d:
            return a["account"]
    return ""


def get_client_profile(filename):
    """
    Bundle everything the invoice + dividend bookers need for one client:
        {file, accounts, expense_accounts, income_accounts, asset_accounts,
         vat_codes, input_vat_codes, ap_account, wht_account}
    expense_accounts = BClass 3 (Aufwand). income_accounts = all P&L accounts
    (BClass 3 + 4) so financial-result accounts classed as BClass 3 are offered.
    asset_accounts = BClass 1 (Aktiven) — the bank/custody + VST dropdowns for
    dividend mode. input_vat_codes = Vorsteuer (I*/M* codes).
    ap_account defaults to 202000 but is taken from the chart if a 'Kreditoren'
    account exists (first BClass-2 account named Kreditoren).
    wht_account = auto-detected Verrechnungssteuer-Guthaben (BClass-1), else ''.
    """
    accounts = get_accounts(filename)
    vat_codes = get_vat_codes(filename)

    expense_accounts = [a for a in accounts if a["bclass"] == "3"]
    # Income picker for dividends: offer all P&L accounts (BClass 3 + 4). Some
    # charts class financial-result accounts (e.g. 6950 Financial revenue) as
    # BClass 3, so a BClass-4-only list would wrongly exclude the right account.
    income_accounts = [a for a in accounts if a["bclass"] in ("3", "4")]
    asset_accounts = [a for a in accounts if a["bclass"] == "1"]

    # Input-VAT codes are the deductible ones (M = material/services, I = investment
    # & operating). Exclude the *-1/*-2 net/amount variants — we book gross-inclusive.
    input_vat_codes = [
        v for v in vat_codes
        if re.fullmatch(r"[MI]\d{2}", v["code"]) and v["rate"]
    ]

    ap_account = DEFAULT_AP_ACCOUNT
    for a in accounts:
        if a["bclass"] == "2" and "kreditor" in a["description"].lower() \
                and "mwst" not in a["description"].lower() \
                and "estv" not in a["description"].lower():
            ap_account = a["account"]
            break

    return {
        "file": filename,
        "accounts": accounts,
        "expense_accounts": expense_accounts,
        "income_accounts": income_accounts,
        "asset_accounts": asset_accounts,
        "vat_codes": vat_codes,
        "input_vat_codes": input_vat_codes,
        "ap_account": ap_account,
        "wht_account": _detect_wht_account(accounts),
    }


# ----------------------------------------------------------------------------
# Balances tree (DESK-73): the full Accounts table INCLUDING group + section
# rows, so the Buchungsdesk can render the client's own Bilanz / Erfolgsrechnung
# Gliederung (group subtotals) rather than a fixed KMU template.
# ----------------------------------------------------------------------------
def get_file_meta(filename):
    """Company name, base currency and fiscal-year dates from the file info
    (GET doc/<file>/info). ValueXml carries ISO-clean values; fall back to Value."""
    body, _ = _get(_doc_path(filename, "info"))
    meta = {}
    for r in _parse_html_rows(body):
        k = (r.get("IdXml") or "").strip()
        if not k or k in meta:
            continue
        meta[k] = (r.get("ValueXml") or r.get("Value") or "").strip()
    company = (meta.get("Company") or "").strip()
    if not company:   # some files leave Company blank and fill the person fields
        company = " ".join(x for x in (meta.get("Name", ""), meta.get("FamilyName", "")) if x).strip()
    return {
        "company": company,
        "currency": meta.get("BasicCurrency") or "CHF",
        "opening": meta.get("OpeningDate") or "",
        "closure": meta.get("ClosureDate") or "",
        "vat": (meta.get("VatNumber") or "").strip(),
        "city": (meta.get("City") or "").strip(),
        "zip": (meta.get("Zip") or "").strip(),
    }


def get_balances_tree(filename):
    """Full Accounts table in Banana's own display order, group + section rows kept.

    Returns {file, company, currency, opening, closure, rows:[...]}. Each row:
        {section, group, account, description, bclass, gr, balance}
    - section: non-empty on a section header (BILANZ/AKTIVEN/PASSIVEN/ERTRAG/AUFWAND, '*','1'..'4','00')
    - group:   the group id on a group/total row (Account is then empty)
    - account: the account number on a posting account (Group is then empty)
    - gr:       the parent group id (links both accounts and groups up the tree)
    - balance:  base-currency balance (signed as Banana stores it: debit +, credit -)
    """
    meta = get_file_meta(filename)
    body, _ = _get(_doc_path(filename, "table/Accounts/rows"))
    rows = []
    for r in _parse_html_rows(body):
        sect = (r.get("Section") or "").strip()
        grp = (r.get("Group") or "").strip()
        acct = (r.get("Account") or "").strip()
        desc = (r.get("Description") or "").strip()
        if not (sect or grp or acct or desc):
            continue   # spacer row
        rows.append({
            "section": sect,
            "group": grp,
            "account": acct,
            "description": desc,
            "bclass": (r.get("BClass") or "").strip(),
            "gr": (r.get("Gr") or "").strip(),
            "balance": _parse_amount(r.get("Balance")),
            "opening": _parse_amount(r.get("Opening")),   # base-currency opening balance (for interim Bilanz)
            "currency": (r.get("Currency") or "").strip(),  # account currency (for the Prüfung Mehrwährungs-Dimension)
        })
    return dict(meta, file=filename, rows=rows)


def get_postings(filename):
    """All simple-entry rows (base currency) for the period columns of ER/Bilanz (DESK-73 parity).
    Returns [{date, debit, credit, amount, taxable, vat, vataccount}] — one row per Banana
    transaction line. `amount` = gross (base currency); for a VAT row Banana posts the NET
    (`taxable`) to the taxed account and the tax (`vat`, signed) to `vataccount`, so the desk can
    reconstruct the same net account balances Banana shows. Projected to the needed columns so the
    response stays under Banana's ~320 KB table-response wall."""
    body, _ = _get(_doc_path(filename, "table/Transactions/rows?columns="
                             "Date,AccountDebit,AccountCredit,Amount,VatTaxable,VatAmount,VatAccount"))
    out = []
    for r in _parse_html_rows(body):
        ad = (r.get("AccountDebit") or "").strip()
        ac = (r.get("AccountCredit") or "").strip()
        if not ad and not ac:
            continue
        out.append({"date": (r.get("Date") or "").strip(), "debit": ad, "credit": ac,
                    "amount": _parse_amount(r.get("Amount")) or 0.0,
                    "taxable": _parse_amount(r.get("VatTaxable")),
                    "vat": _parse_amount(r.get("VatAmount")),
                    "vataccount": (r.get("VatAccount") or "").strip()})
    return out


def _vat_codes_config(filename):
    """{code: {gr1:[grid…], rate:float|None, section:str}} from the VatCodes table.
    section = the VAT-report group (Gr): 1.1 sales due, 1.F flat rate, 1.2 acquisition,
    2 recoverable (input), Z not considered. gr1 = the ESTV grids the code feeds."""
    body, _ = _get(_doc_path(filename, "table/VatCodes/rows?columns=VatCode,Gr,Gr1,VatRate"))
    out = {}
    for r in _parse_html_rows(body):
        code = (r.get("VatCode") or "").strip()
        if not code:
            continue
        gr1 = [g.strip() for g in (r.get("Gr1") or "").split(";") if g.strip()]
        out[code] = {"gr1": gr1, "rate": _parse_amount(r.get("VatRate")), "section": (r.get("Gr") or "").strip()}
    return out


def get_vat(filename, start="", end=""):
    """Per-VAT-code base + tax for a period (date range on the Transactions), with each
    code's ESTV grids and section, for the desk's Swiss MWST return (DESK-73 Slice 2).
    Returns {company, currency, vat, city, zip, start, end, lines, codes:[…]}.
    Each code: {code, gr1:[grid…], rate, section, base, tax} (base = VatTaxable sum,
    tax = VatAmount sum; both signed as Banana stores them)."""
    meta = get_file_meta(filename)
    cfg = _vat_codes_config(filename)
    body, _ = _get(_doc_path(filename, "table/Transactions/rows?columns=Date,VatCode,VatTaxable,VatAmount"))
    agg, lines = {}, 0
    for r in _parse_html_rows(body):
        code = (r.get("VatCode") or "").strip()
        if not code:
            continue
        dt = (r.get("Date") or "").strip()
        if (start and dt < start) or (end and dt > end):
            continue
        a = agg.setdefault(code, [0.0, 0.0])
        a[0] += _parse_amount(r.get("VatTaxable")) or 0.0
        a[1] += _parse_amount(r.get("VatAmount")) or 0.0
        lines += 1
    codes = [dict(cfg.get(c, {"gr1": [], "rate": None, "section": ""}),
                  code=c, base=round(b, 2), tax=round(t, 2)) for c, (b, t) in sorted(agg.items())]
    return {"file": filename, "company": meta["company"], "currency": meta["currency"],
            "vat": meta["vat"], "city": meta["city"], "zip": meta["zip"],
            "opening": meta["opening"], "closure": meta["closure"],
            "start": start, "end": end, "lines": lines, "codes": codes}


def get_account_card(filename, account, start="", end=""):
    """Base-currency movements touching one account, for the ER/Bilanz drill-down.
    Banana is simple-entry (one debit + one credit account per row); `Amount` is the
    base-currency amount. Returns {opening, moves}: `moves` = [{date, doc, description,
    debit, credit}] within [start, end] (file order); `opening` = the account's balance
    AS OF `start` (file opening + movements before `start`), so closing = opening + net
    ties to the drilled figure for any period.

    Only the six columns we need are requested — Banana's webserver truncates a table
    response at ~320 KB, and the full Transactions table (≈40 columns) blows past that on
    real files; the projection keeps it well under the limit."""
    account = str(account).strip()
    cols = "Date,Doc,Description,AccountDebit,AccountCredit,Amount,VatTaxable,VatAmount,VatAccount"
    body, _ = _get(_doc_path(filename, "table/Transactions/rows?columns=" + cols))
    out, before = [], 0.0
    for r in _parse_html_rows(body):
        signed = _row_effect(account, r)                   # this account's net movement (debit + / credit −)
        if signed is None:
            continue
        dt = (r.get("Date") or "").strip()
        if start and dt < start:
            before += signed
            continue
        if end and dt > end:
            continue
        out.append({
            "date": dt,
            "doc": (r.get("Doc") or "").strip(),
            "description": (r.get("Description") or "").strip(),
            "debit": round(signed, 2) if signed > 0 else 0.0,
            "credit": round(-signed, 2) if signed < 0 else 0.0,
        })
    opening = round((_account_opening(filename, account) or 0.0) + before, 2)
    return {"opening": opening, "moves": out}


def get_recent_txns(filename, since=""):
    """Transactions rows from `since` on (ISO date), projected to the columns the Buchungsdesk
    needs to confirm a handed booking landed (DESK-69): Date, Doc, Description, AccountDebit,
    AccountCredit, Amount (base currency), AmountCurrency. Read live from the OPEN document, so
    the desk sees the extension's in-memory change without a reload. Only the few columns are
    requested — the full Transactions table blows past the webserver's ~320 KB response wall."""
    cols = "Date,Doc,Description,AccountDebit,AccountCredit,Amount,AmountCurrency,ExchangeCurrency"
    body, _ = _get(_doc_path(filename, "table/Transactions/rows?columns=" + cols))
    out = []
    for r in _parse_html_rows(body):
        dt = (r.get("Date") or "").strip()
        if not dt or (since and dt < since):
            continue
        out.append({
            "Date": dt,
            "Doc": (r.get("Doc") or "").strip(),
            "Description": (r.get("Description") or "").strip(),
            "AccountDebit": (r.get("AccountDebit") or "").strip(),
            "AccountCredit": (r.get("AccountCredit") or "").strip(),
            "Amount": (r.get("Amount") or "").strip(),
            "AmountCurrency": (r.get("AmountCurrency") or "").strip(),
        })
    return out


def _row_effect(account, r):
    """The net movement of one transaction row on `account` (debit +, credit −), handling the
    VAT split like Banana: the taxed account gets the net, the VAT account the tax, the money
    account the gross. Returns None if the row does not touch the account."""
    deb = (r.get("AccountDebit") or "").strip()
    cred = (r.get("AccountCredit") or "").strip()
    va = (r.get("VatAccount") or "").strip()
    if account not in (deb, cred, va):
        return None
    amt = _parse_amount(r.get("Amount")) or 0.0
    vat = _parse_amount(r.get("VatAmount")) or 0.0
    taxable = _parse_amount(r.get("VatTaxable"))
    if vat and va and taxable is not None:
        if account == va:
            return vat
        if vat < 0:                                        # sales: credit net, debit gross
            return amt if account == deb else -taxable
        return taxable if account == deb else -amt          # input: debit net, credit gross
    return amt if account == deb else (-amt if account == cred else 0.0)


def _account_opening(filename, account):
    """Base-currency opening balance of one account (Debit +, Credit −), so the drill
    can tie closing = opening + movements. None if the account has no opening balance."""
    body, _ = _get(_doc_path(filename, "table/Accounts/rows?columns=Account,Opening"))
    for r in _parse_html_rows(body):
        if (r.get("Account") or "").strip() == str(account).strip():
            return _parse_amount(r.get("Opening"))
    return None
