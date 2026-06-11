"""Unit + golden tests for the statement parser (synthetic data only)."""
import os

import converter
from conftest import FIXTURES

SAMPLE = os.path.join(FIXTURES, "sample_statement.csv")
CREDITCARD = os.path.join(FIXTURES, "sample_creditcard.csv")


# ── amount parsing (the financial-correctness core) ──────────────────────────
def test_parse_amount_swiss_apostrophe():
    assert converter._parse_amount("1'200.00") == 1200.00
    assert converter._parse_amount("1’234.50") == 1234.50  # typographic apostrophe


def test_parse_amount_eu_and_us_separators():
    assert converter._parse_amount("1.234,56") == 1234.56   # EU: dot thousands, comma decimal
    assert converter._parse_amount("1,234.56") == 1234.56   # US: comma thousands, dot decimal
    assert converter._parse_amount("85,30") == 85.30        # comma decimal only


def test_parse_amount_sign_and_blanks():
    assert converter._parse_amount("-50.00") == -50.00
    assert converter._parse_amount("+50.00") == 50.00
    for blank in ("", " ", "-", "+", "nan", None):
        assert converter._parse_amount(blank) is None
    # 0 means "no amount" for income/expenses, but a real 0.00 balance is kept
    assert converter._parse_amount("0.00") is None
    assert converter._parse_amount("0.00", allow_zero=True) == 0.00


# ── date parsing ─────────────────────────────────────────────────────────────
def test_parse_date_formats_normalise_to_ddmmyyyy():
    assert converter._parse_date("31.03.2025") == "31.03.2025"
    assert converter._parse_date("2025-03-31") == "31.03.2025"
    assert converter._parse_date("31/03/2025") == "31.03.2025"
    # junk (e.g. a totals row) passes through and is later filtered out
    assert converter._parse_date("TOTAL") == "TOTAL"


# ── header role detection ────────────────────────────────────────────────────
def test_match_role():
    assert converter._match_role("Datum") == "date"
    assert converter._match_role("Beschreibung") == "description"
    assert converter._match_role("Gutschrift") == "income"
    assert converter._match_role("Belastung") == "expenses"
    assert converter._match_role("Saldo") == "balance"
    assert converter._match_role("Credit Amount") == "income"
    # identifier columns must NOT be treated as amounts
    assert converter._match_role("IBAN") is None
    assert converter._match_role("Account Number") is None


# ── account sniffing ─────────────────────────────────────────────────────────
def test_sniff_account_meta_finds_valid_iban_and_currency():
    meta = converter.sniff_account_meta(SAMPLE)
    assert meta["account_ref"] == "CH9300762011623852957"
    assert meta["currency"] == "CHF"


# ── parsing the whole statement ──────────────────────────────────────────────
def test_parse_to_transactions_amounts_signs_dates():
    txns, roles, warnings = converter.parse_to_transactions(SAMPLE)
    assert len(txns) == 4
    assert [t["date"] for t in txns] == ["05.01.2026", "10.01.2026", "15.01.2026", "20.01.2026"]
    # income vs expenses correctly split
    assert txns[0]["income"] == 5000.00 and txns[0]["expenses"] is None
    assert txns[1]["expenses"] == 1200.00 and txns[1]["income"] is None
    assert txns[2]["income"] == 199.50
    assert txns[3]["expenses"] == 85.30   # comma decimal parsed
    # running balance kept
    assert txns[3]["balance"] == 3914.20


# ── golden TSV output (deterministic, byte-for-byte) ─────────────────────────
EXPECTED_TSV = "\n".join([
    '"Datum";"Buchungstext";"Belastung";"Gutschrift"',
    '"05.01.2026";"Lohn ACME AG";"";"5000.00"',
    '"10.01.2026";"Miete Januar";"1200.00";""',
    '"15.01.2026";"Rückerstattung";"";"199.50"',
    '"20.01.2026";"Einkauf Migros";"85.30";""',
])


def test_creditcard_direction_column_signs_amounts():
    # Swisscard-style export: a "Debit/Kredit" text column decides the side and a
    # single "Betrag" magnitude column carries the amount (charges positive). The
    # direction indicator — not Betrag's sign — must drive Belastung vs Gutschrift.
    txns, roles, warnings = converter.parse_to_transactions(CREDITCARD)
    assert roles.get("direction") == "Debit/Kredit"
    assert "expenses" not in roles and "income" not in roles  # text column not an amount
    assert len(txns) == 2
    # Belastung → expense, Gutschrift → income, both as positive magnitudes
    assert txns[0]["expenses"] == 98.10 and txns[0]["income"] is None
    assert txns[1]["income"] == 1500.00 and txns[1]["expenses"] is None


def test_convert_to_banana_golden_tsv():
    tsv, roles, count, warnings = converter.convert_to_banana(SAMPLE)
    assert count == 4
    assert tsv == EXPECTED_TSV
