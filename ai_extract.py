"""
AI extraction for arbitrary PDF statements (credit cards, varied banks, etc.).

Heuristic table/positional parsing cannot generalise across issuers (Centurion,
Miles & More, every bank lays things out differently, with section-based signs,
FX second-lines and run-together text). This sends the PDF straight to Claude
(the same engine odoo-assistant uses for bank statements) and gets back the
normalized transaction list that the rest of the pipeline already consumes.

Output transactions match converter._df_to_transactions exactly:
    {'date' 'DD.MM.YYYY', 'doc', 'description', 'income', 'expenses', 'balance'}
"""

import os
import re
import base64

MODEL = "claude-sonnet-4-6"

_client = None


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("PDF AI extraction needs ANTHROPIC_API_KEY (not configured).")
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=key)
    return _client


_EXTRACT_TOOL = {
    "name": "extract_statement",
    "description": "Return every booked transaction plus the account metadata from a bank or credit-card statement.",
    "input_schema": {
        "type": "object",
        "properties": {
            "account_ref": {"type": "string", "description": "IBAN, account number, or card number if shown; else empty string."},
            "owner": {"type": "string", "description": "Account/card holder name if shown; else empty string."},
            "currency": {"type": "string", "description": "3-letter ISO currency of the statement, e.g. CHF."},
            "opening_balance": {"type": ["number", "null"], "description": "Previous balance (Saldo letzte Rechnung / Vorsaldo / previous balance). null if absent."},
            "closing_balance": {"type": ["number", "null"], "description": "New balance (Neuer Saldo / Endsaldo / new balance). null if absent."},
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "booking date as DD.MM.YYYY"},
                        "description": {"type": "string"},
                        "amount": {"type": "number", "description": "positive amount in the statement currency"},
                        "direction": {"type": "string", "enum": ["debit", "credit"]},
                    },
                    "required": ["date", "description", "amount", "direction"],
                },
            },
        },
        "required": ["currency", "transactions"],
    },
}

_SYSTEM = """You extract transactions from Swiss bank and credit-card statements of ANY issuer and layout (German, French, Italian or English) for bookkeeping import.

Rules:
- Extract EVERY booked transaction. Booking date as DD.MM.YYYY.
- amount: the amount actually booked in the statement's own currency, as a POSITIVE number (no sign, no thousands separators).
- direction:
    "debit"  = money out / a purchase or charge / amounts under sections like "Neue Transaktionen", "Belastung", "Soll", "Lastschrift".
    "credit" = money in / a refund / a payment made to a credit card / amounts under "Ihre Zahlungen", "Gutschrift", "Haben".
- Foreign-currency card transactions: use the final CHF (statement-currency) amount that was booked, NOT the original foreign amount.
- description: concise; include the merchant/counterparty and any reference.
- opening_balance = previous balance (Saldo letzte Rechnung / Vorsaldo). closing_balance = new balance (Neuer Saldo). null if not present.
- account_ref: IBAN if present, otherwise the account or card number shown; else empty string.
- Do NOT invent transactions. Ignore subtotal/total/summary lines and reward-point sections.
- A fee block whose booked amount is 0.00 because the bank waived the fee (e.g. UBS «Saldo DL-Preisabschluss 0.00» with the lines «E-Banking Inland CHF -2.40» and «Befreit aufgrund Transaktionsfreigrenze CHF 2.40») is NOT a transaction: nothing was booked. Only a fee block with a non-zero booked amount is one transaction of that amount.
- Call the extract_statement tool exactly once with the result."""


def extract_transactions_from_pdf(filepath):
    """Returns (transactions, meta, warnings). Raises ValueError on failure."""
    client = _get_client()
    with open(filepath, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()

    msg = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=_SYSTEM,
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_statement"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
                {"type": "text", "text": "Extract all transactions and the account metadata from this statement."},
            ],
        }],
    )

    payload = None
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "extract_statement":
            payload = block.input
            break
    if not payload:
        raise ValueError("AI extraction returned no structured data.")

    return _to_transactions(payload)


def _to_transactions(payload):
    transactions = []
    for t in (payload.get("transactions") or []):
        amt = t.get("amount")
        if amt is None:
            continue
        try:
            amt = abs(float(amt))
        except (TypeError, ValueError):
            continue
        if amt == 0:
            continue
        credit = t.get("direction") == "credit"
        transactions.append({
            "date": str(t.get("date", "")).strip(),
            "doc": "",
            "description": str(t.get("description", "")).strip(),
            "income": amt if credit else None,
            "expenses": None if credit else amt,
            "balance": None,
        })

    meta = {
        "account_ref": (payload.get("account_ref") or "").strip(),
        "owner": (payload.get("owner") or "").strip(),
        "currency": (payload.get("currency") or "CHF").strip().upper(),
        "opening_balance": payload.get("opening_balance"),
        "closing_balance": payload.get("closing_balance"),
    }
    warnings = [f"Transactions read from the PDF by AI ({MODEL}) — review the preview before importing."]
    transactions = _drop_waived_fees(transactions, meta, warnings)
    return transactions, meta, warnings


_FEE_RX = re.compile(r"preisabschluss|dienstleistungspreis|kontof[uü]hrung|geb[uü]hr|spesen|befreit|service charge|bank fee", re.I)


def _drop_waived_fees(transactions, meta, warnings):
    """v1.32.0 (BLX-79, ANDERRA 25.10.2026): the model read a waived UBS fee («Preisabschluss 0.00»: debit 2.40 +
    «Befreit aufgrund Transaktionsfreigrenze» 2.40) as a debit, so the entries missed the closing balance by 2.40
    and the desk booked a fee the bank never charged. With both balances known: when opening ± entries misses the
    closing balance by exactly ONE fee-like debit, that debit is dropped (with a warning); any other difference is
    reported as a warning for the person to check before importing."""
    ob, cb = meta.get("opening_balance"), meta.get("closing_balance")
    if ob is None or cb is None:
        return transactions
    try:
        expected = round(float(cb) - float(ob), 2)
    except (TypeError, ValueError):
        return transactions
    actual = round(sum((t.get("income") or 0) - (t.get("expenses") or 0) for t in transactions), 2)
    diff = round(expected - actual, 2)          # > 0: the entries carry too much debit
    if abs(diff) < 0.005:
        return transactions
    cands = [t for t in transactions if t.get("expenses") and _FEE_RX.search(t.get("description") or "")
             and abs(round(float(t["expenses"]) - diff, 2)) < 0.005]
    if len(cands) == 1:
        gone = cands[0]
        transactions = [t for t in transactions if t is not gone]
        warnings.append(f"Gebührenzeile «{(gone.get('description') or '')[:60]}» ({float(gone['expenses']):.2f}) entfernt: "
                        "der Auszug weist sie als erlassen aus (Anfangssaldo ± Bewegungen trifft den Schlusssaldo erst ohne sie).")
    else:
        warnings.append(f"Anfangssaldo ± Bewegungen ≠ Schlusssaldo (Differenz {diff:.2f}): Auszug unvollständig gelesen "
                        "oder eine Zeile doppelt. Vor dem Import prüfen.")
    return transactions
