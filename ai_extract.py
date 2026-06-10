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
    for t in payload.get("transactions", []):
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
    return transactions, meta, warnings
