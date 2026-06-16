"""
AI extraction of securities dividend / income vouchers for double-entry booking.

Sends a broker dividend voucher (UBS, PostFinance, Swissquote, IBKR, any layout/
language) to Claude and gets back the booking-relevant facts: the security, the
value date, the GROSS dividend, the Swiss Verrechnungssteuer (35% reclaim), any
foreign withholding tax, and the net amount actually received. When the active
client's chart of INCOME accounts is supplied it also suggests the income account
from that exact list. dividend_booking.py turns this into a composed Banana
transaction (Bank + VST-Guthaben debits / income credit).

Reuses the same forced-tool pattern as invoice_extract.py / ai_extract.py.
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
        raise ValueError("Dividend AI extraction needs ANTHROPIC_API_KEY (not configured).")
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=key)
    return _client


_EXTRACT_TOOL = {
    "name": "extract_dividend",
    "description": "Return the booking-relevant facts of a securities dividend / income voucher.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_dividend": {"type": "boolean",
                            "description": "true only if this is a dividend / securities income credit advice (Dividende, Ausschüttung, Coupon/Zins, Erträgnisabrechnung). false for buy/sell trade confirmations, custody statements, fee notes or general letters."},
            "doc_type": {"type": "string",
                         "description": "Short label: dividend, interest, capital_gain, trade_confirmation, custody_statement, fee, or other."},
            "security_name": {"type": "string", "description": "The security paying the income, e.g. 'Partners Group Holding AG'."},
            "isin": {"type": "string", "description": "ISIN (e.g. CH0024608827) if shown, else empty."},
            "valor": {"type": "string", "description": "Swiss Valoren-Nr. if shown, else empty."},
            "quantity": {"type": ["number", "null"], "description": "Number of shares/units the income was paid on, else null."},
            "value_date": {"type": "string", "description": "Value/payment date (Valuta / Gutschriftsdatum) as DD.MM.YYYY — this is the booking date. Use the booking/value date, not the ex-date."},
            "currency": {"type": "string", "description": "3-letter ISO settlement currency, e.g. CHF."},
            "gross_amount": {"type": "number", "description": "GROSS dividend / income before any tax (Bruttodividende / Transaction value / gross). Positive number, no thousands separators, in the settlement currency."},
            "swiss_withholding_tax": {"type": ["number", "null"], "description": "Swiss Verrechnungssteuer (35%) withheld — the RECLAIMABLE federal anticipatory tax on a Swiss-source (CH) dividend/interest. Positive number. null/0 if none or the issuer is foreign."},
            "foreign_withholding_tax": {"type": ["number", "null"], "description": "Foreign/source withholding tax withheld on a non-Swiss security (e.g. US 15%, DE Kapitalertragsteuer). Positive number. null/0 if none. This is generally NOT reclaimable as Swiss VST."},
            "net_amount": {"type": "number", "description": "Net amount actually credited to the account (Gutschrift / settlement amount), after all taxes. Positive number."},
            "issuer_country": {"type": "string", "description": "2-letter ISO country of the issuer/security if derivable (CH for a Swiss share, US, DE...), else empty."},
            "suggested_account": {"type": "string", "description": "If a chart of INCOME accounts is given in the system prompt, the SINGLE best-matching account NUMBER from that list for securities/dividend income. Empty string if no chart given or no good match."},
            "suggested_account_reason": {"type": "string", "description": "One short phrase explaining the account choice, else empty."},
        },
        "required": ["is_dividend", "doc_type", "security_name", "value_date", "currency", "gross_amount", "net_amount"],
    },
}

_SYSTEM_BASE = """You read securities dividend and income vouchers (broker credit advices) of any issuer, layout and language (DE/FR/IT/EN) and extract the facts needed to book the income double-entry.

Rules:
- gross_amount = the GROSS income before any tax (Bruttodividende, "Transaction value", gross dividend). Positive number, no thousands separators, in the settlement currency.
- swiss_withholding_tax = the Swiss Verrechnungssteuer (federal anticipatory tax, 35%) withheld on a Swiss-source security. This is RECLAIMABLE. On a typical Swiss voucher the "Taxes"/"Steuern" line of -35% of gross is this. null or 0 if absent or the security is foreign.
- foreign_withholding_tax = withholding tax on a NON-Swiss security (US, DE, etc.). null or 0 if none. Do not confuse with Swiss VST.
- net_amount = the amount actually credited after all taxes (Gutschrift / settlement amount in account currency).
- value_date = the value/payment/booking date as DD.MM.YYYY (NOT the ex-date). This becomes the booking date.
- A Swiss dividend usually satisfies: gross - swiss_withholding_tax = net. Sanity-check this; if the taxes line is exactly 35% of gross and the security is Swiss (ISIN starts CH), it is Swiss VST.
- is_dividend = false for buy/sell trade confirmations, plain custody/portfolio statements, and fee notes. Still fill what you can but set is_dividend false so it can be skipped.
- Call the extract_dividend tool exactly once."""


def _accounts_block(income_accounts):
    if not income_accounts:
        return ""
    lines = [f"  {a['account']}  {a['description']}" for a in income_accounts]
    return ("\n\nThis client's available INCOME accounts (choose suggested_account "
            "ONLY from this list, by best match for securities/dividend income):\n" + "\n".join(lines))


def extract_dividend(filepath, income_accounts=None):
    """
    Extract one dividend voucher. Returns the validated tool payload (dict).
    income_accounts: optional list of {account, description} to ground suggested_account.
    Raises ValueError on failure.
    """
    client = _get_client()
    with open(filepath, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()

    system = _SYSTEM_BASE + _accounts_block(income_accounts)

    msg = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system,
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_dividend"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
                {"type": "text", "text": "Extract this dividend/income voucher for double-entry booking."},
            ],
        }],
    )

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "extract_dividend":
            return dict(block.input)
    raise ValueError("AI extraction returned no structured data.")
