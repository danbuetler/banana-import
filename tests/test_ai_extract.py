"""Tests for the AI (PDF) extraction payload → transactions conversion.

Guards the financial-correctness path that turns Claude's tool-use JSON into
booking rows. A model can legitimately return valid JSON with a null/absent
transactions field — that must degrade gracefully, never crash mid-statement.
"""
import ai_extract


def test_null_transactions_does_not_crash():
    # Claude returned valid JSON but transactions is null (no rows found).
    txns, meta, warnings = ai_extract._to_transactions({"transactions": None, "currency": "CHF"})
    assert txns == []
    assert meta["currency"] == "CHF"


def test_missing_transactions_key_does_not_crash():
    txns, _meta, _warnings = ai_extract._to_transactions({"currency": "CHF"})
    assert txns == []


def test_amount_sign_follows_direction():
    payload = {
        "currency": "CHF",
        "transactions": [
            {"date": "01.02.2026", "description": "Income", "amount": "100.00", "direction": "credit"},
            {"date": "02.02.2026", "description": "Expense", "amount": "-40.00", "direction": "debit"},
        ],
    }
    txns, _meta, _warnings = ai_extract._to_transactions(payload)
    assert txns[0]["income"] == 100.0 and txns[0]["expenses"] is None
    assert txns[1]["expenses"] == 40.0 and txns[1]["income"] is None
