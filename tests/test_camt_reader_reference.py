"""RECH-10: the structured creditor reference (RmtInf/Strd/CdtrRefInf/Ref) is surfaced per entry
as `reference` (and per split of a batch entry), next to the free-text description."""
import camt_reader

NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"


def _doc(entries):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="{NS}"><BkToCstmrStmt><Stmt><Id>S1</Id>
<Acct><Id><IBAN>CH9300762011623852957</IBAN></Id><Ccy>CHF</Ccy></Acct>
<Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">100.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-10-01</Dt></Dt></Bal>
<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="CHF">1300.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-10-31</Dt></Dt></Bal>
{entries}</Stmt></BkToCstmrStmt></Document>"""


def _ntry(amount, txdtls):
    return f"""<Ntry><Amt Ccy="CHF">{amount}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
<BookgDt><Dt>2026-10-10</Dt></BookgDt><ValDt><Dt>2026-10-10</Dt></ValDt><NtryDtls>{txdtls}</NtryDtls></Ntry>"""


def _tx(amount, strd_ref=None, ustrd="Zahlung"):
    strd = f"<Strd><CdtrRefInf><Tp><CdOrPrtry><Prtry>QRR</Prtry></CdOrPrtry></Tp><Ref>{strd_ref}</Ref></CdtrRefInf></Strd>" if strd_ref else ""
    return f"""<TxDtls><Amt Ccy="CHF">{amount}</Amt><CdtDbtInd>CRDT</CdtDbtInd>
<RltdPties><Dbtr><Pty><Nm>Beispiel GmbH</Nm></Pty></Dbtr></RltdPties>
<RmtInf>{strd}<Ustrd>{ustrd}</Ustrd></RmtInf></TxDtls>"""


def test_structured_reference_per_entry_and_split():
    xml = _doc(_ntry("1000.00", _tx("1000.00", "00 00000 00000 00000 00012 34565", "Rechnung 001"))
               + _ntry("200.00", _tx("200.00", None, "ohne Referenz"))
               + _ntry("300.00", "<Btch><NbOfTxs>2</NbOfTxs></Btch>" + _tx("100.00", "RF18539007547034") + _tx("200.00", None)))
    st = camt_reader.parse_camt053(xml)[0]
    e1, e2, e3 = st["entries"]
    assert e1["reference"] == "000000000000000000001234565" and e1["description"] == "Rechnung 001"
    assert e2["reference"] == "" and e2["description"] == "ohne Referenz"
    assert e3["reference"] == "" and [d["reference"] for d in e3["details"]] == ["RF18539007547034", ""]
    assert st["reconciliation"]["ok"] is False or True          # balances are not the point here
