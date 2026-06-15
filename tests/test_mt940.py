"""Tests for the MT940 / SWIFT reader and its CAMT.053 output.

Covers: field parsing (dates, signs, refs, descriptions), authoritative
:60F:/:62F: balances flowing through to OPBD/CLBD, reconciliation, and the
content sniff for SWIFT files uploaded with a .txt extension.
"""
import os
import xml.etree.ElementTree as ET

import converter
import camt_writer
import mt940_reader
from conftest import FIXTURES

NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.04"
SAMPLE = os.path.join(FIXTURES, "sample_statement.mt940")


def _q(tag):
    return f"{{{NS}}}{tag}"


def test_parse_fields():
    txns, meta, warnings = mt940_reader.parse_mt940(SAMPLE)
    assert not warnings
    assert meta["account_ref"] == "CH220024724731130060Q"
    assert meta["currency"] == "USD"
    assert meta["opening_balance"] == 1000.0
    assert meta["closing_balance"] == 3188.25
    assert len(txns) == 3

    # value-date YYMMDD -> DD.MM.YYYY, sign by D/C mark, // bank ref captured
    assert txns[0]["date"] == "05.01.2026"
    assert txns[0]["income"] == 2500.50 and txns[0]["expenses"] is None
    assert txns[0]["doc"] == "ZD81054ZD7776614"
    assert "ACME CORP" in txns[0]["description"]
    assert txns[1]["expenses"] == 300.25 and txns[1]["income"] is None
    assert txns[2]["expenses"] == 12.0  # "12," -> 12.00


def test_parse_to_transactions_routes_mt940():
    # the shared entry point must recognize .mt940 and delegate to the reader
    txns, col_roles, _ = converter.parse_to_transactions(SAMPLE)
    assert len(txns) == 3
    assert col_roles == {}


def test_camt_balances_and_reconciliation():
    txns, meta, _ = mt940_reader.parse_mt940(SAMPLE)
    cmeta = {"currency": meta["currency"], "account_ref": meta["account_ref"],
             "owner_name": "", "opening_balance": meta["opening_balance"],
             "closing_balance": meta["closing_balance"]}
    xml, _ = camt_writer.build_camt053(txns, cmeta)
    root = ET.fromstring(xml)

    bals = {}
    for b in root.findall(f".//{_q('Bal')}"):
        code = b.find(f"{_q('Tp')}/{_q('CdOrPrtry')}/{_q('Cd')}").text
        bals[code] = b.find(_q("Amt")).text
    assert bals["OPBD"] == "1000.00"
    assert bals["CLBD"] == "3188.25"

    # opening + net(entries) must equal closing: 1000 + 2500.50 - 300.25 - 12 = 3188.25
    net = 0.0
    for e in root.findall(f".//{_q('Ntry')}"):
        amt = float(e.find(_q("Amt")).text)
        net += amt if e.find(_q("CdtDbtInd")).text == "CRDT" else -amt
    assert round(1000.0 + net, 2) == 3188.25
    # the :25: account is a checksum-valid IBAN, so it is emitted as <IBAN>
    assert root.find(f".//{_q('IBAN')}").text == "CH220024724731130060Q"


def test_content_sniff_detects_swift(tmp_path):
    src = open(SAMPLE).read()
    p = tmp_path / "statement.txt"
    p.write_text(src)
    assert mt940_reader.looks_like_mt940(str(p)) is True
    # a real CSV must not be mistaken for MT940
    csv_path = tmp_path / "x.txt"
    csv_path.write_text("Datum;Buchungstext;Betrag\n01.01.2026;Test;10.00\n")
    assert mt940_reader.looks_like_mt940(str(csv_path)) is False
