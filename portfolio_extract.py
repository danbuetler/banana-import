"""
AI extraction of a securities "Statement of assets" (custody/portfolio statement)
for a year-end revaluation booking.

Sends a broker portfolio statement (UBS "Statement of assets", or any custody
statement) to Claude and gets back, per equity position, the acquisition COST
value and the year-end MARKET value (both in the statement's reporting currency,
e.g. CHF) plus the as-of date. portfolio_booking.py sums these and books the
change against the securities account's current book value in Banana.

Liquidity / cash accounts are skipped — those are reconciled via the bank
statement (CAMT) path, not revalued here.

Reuses the same forced-tool pattern as dividend_extract.py / invoice_extract.py.
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
        raise ValueError("Portfolio AI extraction needs ANTHROPIC_API_KEY (not configured).")
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=key)
    return _client


_EXTRACT_TOOL = {
    "name": "extract_portfolio",
    "description": "Return the equity positions of a securities statement of assets, with acquisition cost and year-end market value in the reporting currency.",
    "input_schema": {
        "type": "object",
        "properties": {
            "as_of_date": {"type": "string", "description": "Valuation date of the statement (Statement of assets as of ...) as DD.MM.YYYY."},
            "reporting_currency": {"type": "string", "description": "Reporting currency the cost_value/market_value are stated in (e.g. CHF)."},
            "positions": {
                "type": "array",
                "description": "One entry per EQUITY / securities position (shares, funds, bonds). Do NOT include cash/liquidity accounts.",
                "items": {
                    "type": "object",
                    "properties": {
                        "security_name": {"type": "string", "description": "Security name, e.g. 'Partners Group Holding AG'."},
                        "isin": {"type": "string", "description": "ISIN if shown, else empty."},
                        "valor": {"type": "string", "description": "Swiss Valoren-Nr. if shown, else empty."},
                        "quantity": {"type": ["number", "null"], "description": "Number of shares/units held."},
                        "position_currency": {"type": "string", "description": "The security's own trading currency (e.g. CHF, USD)."},
                        "cost_value": {"type": ["number", "null"], "description": "Acquisition cost VALUE in the REPORTING currency (the 'Cost value' column — already converted to CHF for foreign positions). Positive number, no thousands separators."},
                        "market_value": {"type": ["number", "null"], "description": "Year-end MARKET value in the reporting currency (the 'Market value' column). Positive number."},
                        "unrealized_pl": {"type": ["number", "null"], "description": "Unrealized P/L in the reporting currency if shown (market_value - cost_value), else null."},
                    },
                    "required": ["security_name", "cost_value", "market_value"],
                },
            },
            "total_cost_value": {"type": ["number", "null"], "description": "Total acquisition cost value of all equities in the reporting currency, if a total is shown; else null."},
            "total_market_value": {"type": ["number", "null"], "description": "Total market value of all equities in the reporting currency (e.g. 'Total Equity investments'), else null."},
        },
        "required": ["as_of_date", "reporting_currency", "positions"],
    },
}

_SYSTEM = """You read a securities custody / portfolio "Statement of assets" of any broker and language (DE/FR/IT/EN) and extract the EQUITY positions needed to revalue the securities holding for a year-end booking.

Rules:
- One entry per equity / securities position (shares, funds, bonds). DO NOT include liquidity/cash accounts (e.g. 'Liquidity - Accounts', current accounts) — those are not revalued here.
- cost_value = the position's acquisition COST value in the REPORTING currency (the 'Cost value' / 'Einstandswert' column). On UBS statements this is already converted to the reporting currency (CHF) even for foreign-currency positions — take that reporting-currency figure, NOT quantity × foreign cost price.
- market_value = the position's year-end MARKET value in the reporting currency (the 'Market value' / 'Kurswert' column).
- unrealized_pl = market_value - cost_value in the reporting currency, if the statement states it.
- as_of_date = the valuation date ("Statement of assets as of ...") as DD.MM.YYYY.
- All amounts as positive plain numbers, no thousands separators.
- If the statement shows totals (e.g. 'Total Equity investments'), fill total_cost_value / total_market_value so the sum can be cross-checked.
- Call the extract_portfolio tool exactly once."""


def extract_portfolio(filepath):
    """Extract the equity positions from a statement of assets. Returns the validated
    tool payload (dict). Raises ValueError on failure."""
    client = _get_client()
    with open(filepath, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()

    msg = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_SYSTEM,
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_portfolio"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
                {"type": "text", "text": "Extract the equity positions (cost value + market value in the reporting currency) from this statement of assets."},
            ],
        }],
    )

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "extract_portfolio":
            return dict(block.input)
    raise ValueError("AI extraction returned no structured data.")
