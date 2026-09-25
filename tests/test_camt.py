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


def test_counterparty_from_related_parties():
    """DESK-14: the other party of a single entry comes from RltdPties (creditor on a debit,
    debtor on a credit; .08 Pty/Nm shape too); a batch entry carries none."""
    import camt_reader
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt><Stmt>
 <Id>S1</Id><FrToDt><FrDtTm>2026-09-01T00:00:00</FrDtTm><ToDtTm>2026-09-30T00:00:00</ToDtTm></FrToDt>
 <Acct><Id><IBAN>CH3800761000508314276</IBAN></Id><Ccy>CHF</Ccy><Ownr><Nm>Lindenmoos AG</Nm></Ownr></Acct>
 <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">100.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-09-01</Dt></Dt></Bal>
 <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">1052.60</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-09-30</Dt></Dt></Bal>
 <Ntry><Amt Ccy="CHF">47.40</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts><BookgDt><Dt>2026-09-22</Dt></BookgDt><ValDt><Dt>2026-09-22</Dt></ValDt>
  <NtryDtls><TxDtls><RltdPties><Dbtr><Pty><Nm>Lindenmoos AG</Nm></Pty></Dbtr><Cdtr><Pty><Nm>Hostpoint  AG</Nm></Pty></Cdtr></RltdPties><RmtInf><Ustrd>HP-2026-77</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
 <Ntry><Amt Ccy="CHF">1500.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts><BookgDt><Dt>2026-09-15</Dt></BookgDt><ValDt><Dt>2026-09-15</Dt></ValDt>
  <NtryDtls><TxDtls><RltdPties><Dbtr><Nm>Fitnesspark AG</Nm></Dbtr><Cdtr><Nm>Lindenmoos AG</Nm></Cdtr></RltdPties><RmtInf><Ustrd>LM-2026-0004</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
 <Ntry><Amt Ccy="CHF">500.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts><BookgDt><Dt>2026-09-20</Dt></BookgDt><ValDt><Dt>2026-09-20</Dt></ValDt><AddtlNtryInf>Sammelzahlung</AddtlNtryInf>
  <NtryDtls><Btch><NbOfTxs>2</NbOfTxs></Btch>
   <TxDtls><Amt Ccy="CHF">300.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><RltdPties><Cdtr><Nm>A AG</Nm></Cdtr></RltdPties><RmtInf><Ustrd>a</Ustrd></RmtInf></TxDtls>
   <TxDtls><Amt Ccy="CHF">200.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><RltdPties><Cdtr><Nm>B AG</Nm></Cdtr></RltdPties><RmtInf><Ustrd>b</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>"""
    st = camt_reader.parse_camt053(xml)[0]
    assert st["period_from"] == "2026-09-01" and st["period_to"] == "2026-09-30"
    e = {x["ref"] or x["description"]: x for x in st["entries"]}
    by_amount = {x["amount"]: x for x in st["entries"]}
    assert by_amount["-47.40"]["counterparty"] == "Hostpoint AG"     # creditor on a debit, .08 Pty/Nm, whitespace collapsed
    assert by_amount["1500.00"]["counterparty"] == "Fitnesspark AG"  # debtor on a credit, .04 Nm
    assert by_amount["-500.00"]["counterparty"] == "" and len(by_amount["-500.00"]["details"]) == 2   # batch: splits carry the parties
