"""Tests for collective-booking (Sammelbuchung) handling.

A UBS-style export books a collective payment as a parent row (Debit/Credit
total + Transaction no.) followed by beneficiary detail rows (blank date, an
"Individual amount", the same Transaction no.). The parser must attach those
details to the parent (not drop them), and the CAMT writer must emit one
<TxDtls> per beneficiary inside a batch <Ntry> so Banana expands the entry
into individually-codable rows. The reader must surface the splits again.
"""
import os

import converter
import camt_writer
import camt_reader
from conftest import FIXTURES

SAMPLE = os.path.join(FIXTURES, "sample_batch.csv")
META = {"account_ref": "CH28 0027 3273 1743 3301 A", "currency": "CHF",
        "opening_balance": None}


def test_parser_captures_details():
    txns, _roles, warnings = converter.parse_to_transactions(SAMPLE)
    assert warnings == []
    # 6 top-level transactions (the detail rows are nested, not separate).
    assert len(txns) == 6

    by_amt = {t.get("expenses") or t.get("income"): t for t in txns}
    multi = by_amt[3395.00]
    assert len(multi["details"]) == 2
    split_sum = sum(d["expenses"] for d in multi["details"])
    assert split_sum == 3395.00  # 2952.00 + 443.00

    standing = by_amt[1246.55]
    assert len(standing["details"]) == 1
    assert "Cembra" in standing["details"][0]["description"]


def test_details_do_not_double_count_balances():
    # Opening 9650.15 + sum of the 6 PARENT nets must equal stated closing 1324.07.
    txns, _r, _w = converter.parse_to_transactions(SAMPLE)
    xml, _warn = camt_writer.build_camt053(txns, META)
    stmt = camt_reader.parse_camt053(xml)[0]
    assert stmt["reconciliation"]["ok"] is True
    assert stmt["reconciliation"]["difference"] == "0.00"


def test_writer_emits_batch_txdtls():
    txns, _r, _w = converter.parse_to_transactions(SAMPLE)
    xml, _warn = camt_writer.build_camt053(txns, META)
    # 6 entries. Only the multi-beneficiary collective (2 splits) is a real batch:
    # 1 <Btch> + 1 <AddtlNtryInf>. TxDtls = 2 (multi) + 1 (single-item standing
    # order, emitted as a plain entry) + 4 (the ordinary entries) = 7.
    assert xml.count("<Ntry>") == 6
    assert xml.count("<TxDtls>") == 7
    assert xml.count("<Btch>") == 1
    assert xml.count("<AddtlNtryInf>") == 1


def test_reader_surfaces_splits():
    txns, _r, _w = converter.parse_to_transactions(SAMPLE)
    xml, _warn = camt_writer.build_camt053(txns, META)
    stmt = camt_reader.parse_camt053(xml)[0]
    # Only the multi-beneficiary collective surfaces as split sub-rows.
    batched = [e for e in stmt["entries"] if e["details"]]
    assert len(batched) == 1
    multi = batched[0]
    assert multi["amount"] == "-3395.00"
    assert [d["amount"] for d in multi["details"]] == ["-2952.00", "-443.00"]
    # The single-beneficiary standing order is one plain row carrying BOTH labels.
    standing = next(e for e in stmt["entries"] if e["amount"] == "-1246.55")
    assert standing["details"] == []
    assert "Various standing orders" in standing["description"]
    assert "Cembra" in standing["description"]
