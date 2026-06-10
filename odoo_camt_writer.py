"""
CAMT.053 (ISO 20022) writer — Odoo variant.

Kept SEPARATE from camt_writer.py (the Banana-tested writer) on purpose: Odoo
Enterprise's `account_bank_statement_import_camt` parser is stricter than Banana,
so its quirks live here and the proven Banana output never changes.

Key difference vs the Banana writer: every <Ntry> carries a <BkTxCd> (Bank
Transaction Code). Odoo does `entry.xpath('ns:BkTxCd', namespaces=ns)[0]` with no
guard (account_journal.py, _parse_bank_statement_file_camt), so a missing element
raises `IndexError: list index out of range` and the whole import aborts. BkTxCd
is also mandatory (1..1) in the ISO 20022 schema. We additionally emit
AcctSvcrRef and a TxDtls/Refs block to mirror what real Swiss-bank camt.053 files
contain, since Odoo's parser is tested against those.

Shared balance/date/IBAN logic is imported from camt_writer — do NOT reimplement.
"""

import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from camt_writer import (
    CAMT_NS, _USTRD_MAX, _REF_MAX,
    validate_iban, _iso_date, _date_obj, _amt, _msg_id, _sub,
    order_chronological, _derive_balances, _add_balance, summarize,  # noqa: F401  (summarize re-exported)
)


def _add_bktxcd(parent, cd_ind):
    """
    BankTransactionCodeStructure4 with a generic ISO 20022 Domn block.
    Required by Odoo's CAMT importer (unguarded xpath[0]) and mandatory in the
    schema. PMNT = Payments; RCDT/ICDT = received/issued credit transfer; the
    OTHR sub-family keeps it generic since bank-statement rows carry no richer
    transaction taxonomy here.
    """
    bktxcd = _sub(parent, 'BkTxCd')
    domn = _sub(bktxcd, 'Domn')
    _sub(domn, 'Cd', 'PMNT')
    fmly = _sub(domn, 'Fmly')
    _sub(fmly, 'Cd', 'RCDT' if cd_ind == 'CRDT' else 'ICDT')
    _sub(fmly, 'SubFmlyCd', 'OTHR')


def build_camt053_odoo(transactions, meta):
    """
    Same contract as camt_writer.build_camt053, but emits an Odoo-compatible
    document. Returns (xml_string, warnings).
    """
    if not transactions:
        raise ValueError('No transactions to write.')

    transactions = order_chronological(transactions)
    warnings = []
    currency = (meta.get('currency') or 'CHF').strip().upper()
    account_ref = (meta.get('account_ref') or meta.get('iban') or '').strip()
    iban_ok, iban_norm = validate_iban(account_ref)

    opening, op_date, closing, cl_date, bal_warn = _derive_balances(transactions, meta)
    warnings.extend(bal_warn)

    date_objs = [d for d in (_date_obj(t['date']) for t in transactions) if d]
    from_dt = min(date_objs).strftime('%Y-%m-%d') if date_objs else _iso_date(op_date)
    to_dt = max(date_objs).strftime('%Y-%m-%d') if date_objs else _iso_date(cl_date)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

    # Default namespace via a literal xmlns attribute -> clean output, no ns0: prefixes.
    doc = ET.Element('Document')
    doc.set('xmlns', CAMT_NS)
    bk = _sub(doc, 'BkToCstmrStmt')

    grphdr = _sub(bk, 'GrpHdr')
    _sub(grphdr, 'MsgId', _msg_id())
    _sub(grphdr, 'CreDtTm', now)

    stmt = _sub(bk, 'Stmt')
    _sub(stmt, 'Id', f'STMT-{uuid.uuid4().hex[:8]}')
    _sub(stmt, 'CreDtTm', now)
    frto = _sub(stmt, 'FrToDt')
    _sub(frto, 'FrDt', from_dt)
    _sub(frto, 'ToDt', to_dt)

    acct = _sub(stmt, 'Acct')
    acct_id = _sub(acct, 'Id')
    if iban_ok:
        _sub(acct_id, 'IBAN', iban_norm)
    elif account_ref:
        _sub(_sub(acct_id, 'Othr'), 'Id', account_ref[:34])
    else:
        _sub(_sub(acct_id, 'Othr'), 'Id', 'NOTPROVIDED')
    _sub(acct, 'Ccy', currency)
    if meta.get('owner_name'):
        _sub(_sub(acct, 'Ownr'), 'Nm', str(meta['owner_name'])[:140])

    _add_balance(stmt, 'OPBD', opening, op_date, currency)
    _add_balance(stmt, 'CLBD', closing, cl_date, currency)

    for idx, t in enumerate(transactions, 1):
        if t.get('income') is not None:
            cd_ind, amount = 'CRDT', t['income']
        elif t.get('expenses') is not None:
            cd_ind, amount = 'DBIT', t['expenses']
        else:
            warnings.append(f"Skipped a row with no amount (date {t.get('date', '?')}).")
            continue

        # ReportEntry4 order: NtryRef, Amt, CdtDbtInd, Sts, BookgDt, ValDt,
        # AcctSvcrRef, BkTxCd, NtryDtls
        ntry = _sub(stmt, 'Ntry')
        doc_ref = str(t.get('doc') or '').strip()
        ntry_ref = doc_ref[:_REF_MAX] if (doc_ref and doc_ref.lower() != 'nan') else f'NTRY-{idx}'
        _sub(ntry, 'NtryRef', ntry_ref)
        amt = _sub(ntry, 'Amt', _amt(amount))
        amt.set('Ccy', currency)
        _sub(ntry, 'CdtDbtInd', cd_ind)
        _sub(ntry, 'Sts', 'BOOK')
        _sub(_sub(ntry, 'BookgDt'), 'Dt', _iso_date(t['date']))
        _sub(_sub(ntry, 'ValDt'), 'Dt', _iso_date(t['date']))
        _sub(ntry, 'AcctSvcrRef', ntry_ref)
        _add_bktxcd(ntry, cd_ind)   # <-- the element Odoo requires

        # EntryTransaction4 order: Refs, BkTxCd, RmtInf
        txdtls = _sub(_sub(ntry, 'NtryDtls'), 'TxDtls')
        refs = _sub(txdtls, 'Refs')
        _sub(refs, 'EndToEndId', 'NOTPROVIDED')
        _add_bktxcd(txdtls, cd_ind)
        desc = str(t.get('description') or '').strip()
        if desc:
            rmtinf = _sub(txdtls, 'RmtInf')
            for i in range(0, len(desc), _USTRD_MAX):
                _sub(rmtinf, 'Ustrd', desc[i:i + _USTRD_MAX])

    body = ET.tostring(doc, encoding='unicode')
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + body
    return xml, warnings
