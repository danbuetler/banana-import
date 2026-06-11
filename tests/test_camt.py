"""Tests for CAMT.053 output — sign (CdtDbtInd), amounts, balances, IBAN.

XML carries timestamps/UUIDs so we parse and assert structure, not byte-equality.
"""
import os
import xml.etree.ElementTree as ET

import converter
import camt_writer
from conftest import FIXTURES

NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.04"
SAMPLE = os.path.join(FIXTURES, "sample_statement.csv")


def _q(tag):
    return f"{{{NS}}}{tag}"


def _build():
    txns, _, _ = converter.parse_to_transactions(SAMPLE)
    meta = {"currency": "CHF", "account_ref": "CH9300762011623852957", "owner_name": "Test AG"}
    xml, warnings = camt_writer.build_camt053(txns, meta)
    return ET.fromstring(xml), warnings


def test_iban_validation():
    ok, norm = camt_writer.validate_iban("CH93 0076 2011 6238 5295 7")
    assert ok is True
    assert norm == "CH9300762011623852957"
    # a transaction reference that merely looks IBAN-ish must fail mod-97
    assert camt_writer.validate_iban("CH00 0000 0000 0000 0000 0")[0] is False


def test_entries_have_correct_sign_and_amount():
    root, _ = _build()
    entries = root.findall(f".//{_q('Ntry')}")
    got = [(e.find(_q("CdtDbtInd")).text, e.find(_q("Amt")).text) for e in entries]
    # chronological order, sign carried by CdtDbtInd, amount always unsigned 2dp
    assert got == [
        ("CRDT", "5000.00"),  # Lohn (income)
        ("DBIT", "1200.00"),  # Miete (expense)
        ("CRDT", "199.50"),   # Rückerstattung
        ("DBIT", "85.30"),    # Einkauf
    ]
    # every amount is in CHF
    assert all(e.find(_q("Amt")).get("Ccy") == "CHF" for e in entries)


def test_opening_and_closing_balances():
    root, _ = _build()
    bals = {}
    for b in root.findall(f".//{_q('Bal')}"):
        code = b.find(f"{_q('Tp')}/{_q('CdOrPrtry')}/{_q('Cd')}").text
        bals[code] = (b.find(_q("Amt")).text, b.find(_q("CdtDbtInd")).text)
    # opening derived from the running-balance anchor = 0.00; closing = net = 3914.20
    assert bals["OPBD"] == ("0.00", "CRDT")
    assert bals["CLBD"] == ("3914.20", "CRDT")


def test_iban_emitted():
    root, _ = _build()
    assert root.find(f".//{_q('IBAN')}").text == "CH9300762011623852957"


def test_order_chronological_reverses_newest_first():
    # bank exported newest-first -> writer must flip to oldest-first
    newest_first = [
        {"date": "20.01.2026", "income": None, "expenses": 10.0, "balance": None, "description": "b", "doc": ""},
        {"date": "05.01.2026", "income": 100.0, "expenses": None, "balance": None, "description": "a", "doc": ""},
    ]
    ordered = camt_writer.order_chronological(newest_first)
    assert [t["date"] for t in ordered] == ["05.01.2026", "20.01.2026"]


def test_derive_balances_from_entered_opening():
    txns = [
        {"date": "01.02.2026", "income": 200.0, "expenses": None, "balance": None, "description": "x", "doc": ""},
        {"date": "02.02.2026", "income": None, "expenses": 50.0, "balance": None, "description": "y", "doc": ""},
    ]
    opening, _, closing, _, warnings = camt_writer._derive_balances(txns, {"opening_balance": 1000.0})
    assert str(opening) == "1000.00"
    assert str(closing) == "1150.00"   # 1000 + 200 - 50
    assert warnings  # warns that balances were computed from the entered opening
