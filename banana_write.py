"""
Direct writer for Banana Accounting via the documentChange API.

This generalises the proven Lets-Go-Digital reconstruction (the one-off
`gen_lego_year.py`, 2026-09-30): take a list of journal lines, build a
`documentChange` "Transactions" payload onto a base .ac2 (sent as base64), POST
it to the local Banana Plus engine, and get the updated .ac2 bytes back.

Companion to the read-only `banana_live.py`. Together they are the read+write pair
the Buchungsdesk needs to book a Banana client (the desk stays the workplace and
reaches this module on the Mac over a tunnel — the engine is Mac-local).

Proven field shape (verified live on the three Lego year files):
    Date, Doc, Description, AccountDebit, AccountCredit,
    Amount, AmountCurrency, ExchangeCurrency
CHF lines carry Amount == AmountCurrency and ExchangeCurrency "CHF" (no rate).
Foreign-currency lines add ExchangeCurrency + an ExchangeRate.

The write is bytes-in -> bytes-out: the engine takes the whole file (base64) plus
the change and returns a new file. It does NOT need the file open in Banana to
apply the transactions, but computed columns (Opening, balances) are recomputed by
Banana's recalc (Shift+Cmd+F9) on next open — see BananaWriter.post_document.

Stdlib only (urllib), self-signed cert ignored — same posture as banana_live.py.
"""

import os
import ssl
import json
import base64
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("BANANA_BASE_URL", "https://host.docker.internal:8089").rstrip("/")
TOKEN = os.environ.get("BANANA_TOKEN", "")
# Gates copied from the proven post_lego.sh: a real .ac2 comes back as binary >= 100 KB.
MIN_AC2_BYTES = int(os.environ.get("BANANA_MIN_AC2_BYTES", "100000"))

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class BananaWriteError(RuntimeError):
    """Raised when a line is malformed or the engine refuses / returns junk."""


def available():
    return bool(TOKEN)


def _fmt_amount(value, decimals):
    """Format a number as Banana expects: fixed decimals, dot separator, no grouping.
    `value` may be a number or an already-formatted string (passed through)."""
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            raise BananaWriteError("empty amount")
        return s
    return f"{float(value):.{decimals}f}"


def _txn_row(line):
    """Map one journal line dict to a Banana Transactions row.

    Required keys: date (YYYY-MM-DD), debit (Soll account), credit (Haben account),
                   amount (number or string).
    Optional keys: doc, description, currency (default CHF), rate (ExchangeRate for
                   a foreign currency), decimals (default 2; crypto files use 8).
    """
    for k in ("date", "debit", "credit", "amount"):
        if line.get(k) in (None, ""):
            raise BananaWriteError(f"line missing '{k}': {line!r}")
    currency = (line.get("currency") or "CHF").strip()
    decimals = int(line.get("decimals", 2))
    amt = _fmt_amount(line["amount"], decimals)

    fields = {
        "Date": str(line["date"]),
        "Doc": str(line.get("doc", "") or ""),
        "Description": str(line.get("description", "") or ""),
        "AccountDebit": str(line["debit"]),
        "AccountCredit": str(line["credit"]),
        "Amount": amt,
        "AmountCurrency": amt,
        "ExchangeCurrency": currency,
    }
    if line.get("rate") not in (None, ""):
        fields["ExchangeRate"] = _fmt_amount(line["rate"], int(line.get("rate_decimals", 6)))
    return {"fields": fields, "operation": {"name": "add"}}


def build_payload(base_ac2_bytes, lines, title=""):
    """Build the documentChange payload that adds `lines` (Transactions) onto the
    base .ac2. Pure function (no network) so it is unit-testable offline.
    """
    if not base_ac2_bytes:
        raise BananaWriteError("base_ac2_bytes is empty")
    if not lines:
        raise BananaWriteError("no lines to book")
    rows = [_txn_row(l) for l in lines]
    b64 = base64.b64encode(base_ac2_bytes).decode()
    return {
        "fileType": {"ac2": b64, "title": title or "Buchungsdesk"},
        "data": {
            "format": "documentChange",
            "data": [{"document": {"dataUnits": [
                {"nameXml": "Transactions", "data": {"rowLists": [{"rows": rows}]}},
            ]}}],
        },
    }


def control_total(lines):
    """Sum of the line amounts (numbers only) — the self-check the Lego build used
    to refuse a payload whose total didn't match the expected per-year subtotal."""
    total = 0.0
    for l in lines:
        v = l.get("amount")
        if not isinstance(v, str):
            total += float(v)
    return round(total, 2)


def post_document(base_ac2_bytes, lines, title="", expect_total=None, timeout=60, show=False):
    """Build + POST the payload to the local Banana engine and return the updated
    .ac2 bytes. Requires BANANA_TOKEN and the engine running (Banana open).

    expect_total: when given, refuse to POST unless the line amounts sum to it
    (+/- 0.005) — the Lego self-check, ported.
    show: when True, ask the engine to also open the returned file in Banana (the
    Lego build used this). A bridge booking leaves it False — it wants the bytes,
    not an extra open document.
    """
    if not TOKEN:
        raise BananaWriteError(
            "BANANA_TOKEN is not set — open Banana, enable its webserver, put the token "
            "in banana-import/.env (see set-banana-token.sh)."
        )
    if expect_total is not None:
        got = control_total(lines)
        if abs(got - float(expect_total)) > 0.005:
            raise BananaWriteError(f"control total {got:.2f} != expected {float(expect_total):.2f} — nothing posted")

    payload = build_payload(base_ac2_bytes, lines, title=title)
    q = "show&" if show else ""
    url = f"{BASE_URL}/v2/doc?{q}acstkn={urllib.parse.quote(TOKEN)}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
            data = r.read()
            ctype = r.headers.get("content-type", "")
    except Exception as e:  # noqa: BLE001 — surface any transport error uniformly
        raise BananaWriteError(
            f"Could not POST to the Banana engine at {BASE_URL}. Is Banana open with the "
            f"webserver enabled? ({e})"
        )
    # The engine returns JSON on error, the .ac2 binary on success.
    if "json" in ctype or (data[:1] in (b"{", b"[")):
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except Exception:
            msg = data[:500]
        raise BananaWriteError(f"engine refused the change: {msg}")
    if len(data) < MIN_AC2_BYTES:
        raise BananaWriteError(f"engine returned {len(data)} bytes (< {MIN_AC2_BYTES}) — not a valid .ac2")
    return data
