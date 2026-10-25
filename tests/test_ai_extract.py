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


def test_waived_fee_is_dropped_when_it_explains_the_balance_gap():
    """BLX-79: a UBS «Preisabschluss 0.00» block read as a 2.40 debit is dropped when opening ± entries misses the
    closing balance by exactly that fee; any other gap only warns."""
    payload = {"currency": "CHF", "opening_balance": 0.0, "closing_balance": 104080.08, "transactions": [
        {"date": "05.08.2026", "amount": 100000.00, "direction": "credit", "description": "Capital Release"},
        {"date": "31.08.2026", "amount": 2.40, "direction": "debit", "description": "Saldo DL-Preisabschluss 01.08.2026-31.08.2026 - E-Banking Inland"},
        {"date": "15.09.2026", "amount": 4080.08, "direction": "credit", "description": "Vergütung Trivionis"}]}
    txns, _meta, warnings = ai_extract._to_transactions(payload)
    assert [t["description"][:7] for t in txns] == ["Capital", "Vergütu"]
    assert any("erlassen" in w for w in warnings)
    # a gap that no single fee explains stays in the file, with a warning
    payload["closing_balance"] = 104082.68          # gap 5.00, the fee is 2.40: nothing dropped
    txns, _meta, warnings = ai_extract._to_transactions(payload)
    assert len(txns) == 3 and any("Differenz 5.00" in w for w in warnings)
    # no balances known: untouched, no warning about the gap
    payload["closing_balance"] = None
    txns, _meta, warnings = ai_extract._to_transactions(payload)
    assert len(txns) == 3 and not any("Differenz" in w for w in warnings)
