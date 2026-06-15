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


def get_accounts(filename):
    """
    Return the chart of accounts as a list of dicts:
        {account, description, bclass, vatcode, group}
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


def get_client_profile(filename):
    """
    Bundle everything the invoice booker needs for one client:
        {file, accounts, expense_accounts, vat_codes, input_vat_codes, ap_account}
    expense_accounts = BClass 3 (Aufwand). input_vat_codes = Vorsteuer (I*/M* codes).
    ap_account defaults to 202000 but is taken from the chart if a 'Kreditoren'
    account exists (first BClass-2 account named Kreditoren).
    """
    accounts = get_accounts(filename)
    vat_codes = get_vat_codes(filename)

    expense_accounts = [a for a in accounts if a["bclass"] == "3"]

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
        "vat_codes": vat_codes,
        "input_vat_codes": input_vat_codes,
        "ap_account": ap_account,
    }
