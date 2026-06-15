"""
AI extraction of Swiss AP (Kreditoren) invoices for double-entry accrual booking.

Sends a supplier-invoice PDF to Claude and gets back the booking-relevant facts
(vendor, dates, amounts, VAT, QR reference) PLUS — when the active client's chart
of expense accounts is supplied — a suggested expense account chosen from that
exact list. The booker (invoice_booking.py) then overrides the account from the
per-client learned vendor map and turns it into a Banana transaction.

Reuses the same forced-tool pattern as ai_extract.py (bank statements).
"""

import os
import json
import base64

MODEL = "claude-sonnet-4-6"

_client = None


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("Invoice AI extraction needs ANTHROPIC_API_KEY (not configured).")
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=key)
    return _client


_EXTRACT_TOOL = {
    "name": "extract_invoice",
    "description": "Return the booking-relevant facts of a supplier (accounts-payable) invoice.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_invoice": {"type": "boolean",
                           "description": "true only if this is a payable invoice/bill (Rechnung/Faktura/Beitragsrechnung). false for account statements (Kontoauszug), balance confirmations, reminders without an amount due, or general letters."},
            "doc_type": {"type": "string",
                         "description": "Short label: invoice, account_statement, reminder, balance_confirmation, or other."},
            "vendor_name": {"type": "string", "description": "The supplier/creditor issuing the invoice (who gets paid)."},
            "vendor_vat_no": {"type": "string", "description": "Supplier's Swiss VAT number (CHE-...) if shown, else empty."},
            "invoice_number": {"type": "string", "description": "Invoice/Faktura/Rechnungs-Nr. if shown, else empty."},
            "invoice_date": {"type": "string", "description": "Invoice date (Rechnungsdatum) as DD.MM.YYYY. This is the accrual booking date."},
            "due_date": {"type": "string", "description": "Payable-by date (zahlbar bis / Fälligkeit) as DD.MM.YYYY, else empty."},
            "currency": {"type": "string", "description": "3-letter ISO currency, e.g. CHF."},
            "total_gross": {"type": "number", "description": "Total amount payable INCLUDING VAT (Rechnungsbetrag / zahlbar). Plain number, no thousands separators."},
            "net_amount": {"type": ["number", "null"], "description": "Net amount before VAT if shown, else null."},
            "vat_amount": {"type": ["number", "null"], "description": "VAT/MWST amount if shown. 0 or null if the invoice has no VAT (e.g. social insurance, BVG/pension, AHV)."},
            "vat_rate": {"type": ["number", "null"], "description": "VAT percentage as a number, e.g. 8.1, 2.6, 3.8. null if no VAT."},
            "reference": {"type": "string", "description": "QR-bill / ESR payment reference (Referenz) if shown, else empty."},
            "creditor_iban": {"type": "string", "description": "Creditor IBAN / QR-IBAN from the payment part if shown, else empty."},
            "description": {"type": "string", "description": "Concise booking text: what the expense is for + period if relevant (e.g. 'Swisscom Mobile-Abo Februar 2026', 'BVG Beiträge Q1 2026')."},
            "suggested_account": {"type": "string", "description": "If a chart of expense accounts is given in the system prompt, the SINGLE best-matching account NUMBER from that list for this expense. Empty string if no chart given or no good match."},
            "suggested_account_reason": {"type": "string", "description": "One short phrase explaining the account choice, else empty."},
        },
        "required": ["is_invoice", "doc_type", "vendor_name", "invoice_date", "currency", "total_gross"],
    },
}

_SYSTEM_BASE = """You read Swiss supplier invoices (Kreditoren / accounts payable) of any layout and language (DE/FR/IT/EN) and extract the facts needed to book them for GAAP-compliant accrual accounting.

Rules:
- total_gross = the amount actually payable, INCLUDING VAT (Rechnungsbetrag, "zahlbar bis", QR-bill amount). Positive number, no thousands separators.
- invoice_date = Rechnungsdatum/Faktura-Datum as DD.MM.YYYY (this becomes the booking date). due_date = "zahlbar bis"/Fälligkeit if present.
- VAT: if the invoice shows MWST/VAT, set vat_amount and vat_rate (e.g. 8.1). Many Swiss invoices have NO VAT — social insurance (SVA, AHV/IV/EO), pension funds (BVG, Pensionskasse), and pure fees: set vat_amount 0 and vat_rate null in that case. Do NOT invent VAT.
- description: a concise booking text a bookkeeper would use, including the period if the invoice covers one.
- is_invoice = false for account statements (Kontoauszug), balance confirmations/audits, payment reminders without a fresh amount, and letters. Still fill what you can but set is_invoice false so it can be skipped.
- Call the extract_invoice tool exactly once."""


def _accounts_block(expense_accounts):
    if not expense_accounts:
        return ""
    lines = [f"  {a['account']}  {a['description']}" for a in expense_accounts]
    return ("\n\nThis client's available EXPENSE accounts (choose suggested_account "
            "ONLY from this list, by best match to the vendor/expense):\n" + "\n".join(lines))


def extract_invoice(filepath, expense_accounts=None):
    """
    Extract one AP invoice. Returns the validated tool payload (dict).
    expense_accounts: optional list of {account, description} to ground suggested_account.
    Raises ValueError on failure.
    """
    client = _get_client()
    with open(filepath, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()

    system = _SYSTEM_BASE + _accounts_block(expense_accounts)

    msg = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system,
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_invoice"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
                {"type": "text", "text": "Extract this supplier invoice for accounts-payable booking."},
            ],
        }],
    )

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "extract_invoice":
            return dict(block.input)
    raise ValueError("AI extraction returned no structured data.")
