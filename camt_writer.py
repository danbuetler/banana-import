"""
CAMT.053 (ISO 20022) writer.

Takes the normalized transaction list produced by converter.parse_to_transactions
and emits a camt.053.001.04 XML string that Banana Accounting Plus (and Odoo)
import natively. No third-party dependencies — stdlib ElementTree + decimal only.

camt.053.001.04 is the Swiss Payment Standards (SIX) dominant variant. To switch
to .08, change CAMT_NS and the few elements noted in comments (Sts becomes a
<Cd> choice in .08; here in .04 it is a simple code).
"""

import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CAMT_NS = 'urn:iso:std:iso:20022:tech:xsd:camt.053.001.04'

_USTRD_MAX = 140   # max length per RmtInf/Ustrd occurrence (ISO 20022)
_REF_MAX = 35      # max length for NtryRef / MsgId / Id


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def validate_iban(raw):
    """
    Validate an IBAN via the ISO 13616 mod-97 check.
    Returns (ok: bool, normalized: str) where normalized is upper-cased, spaces
    stripped. Used both server-side here and mirrored client-side in the UI.
    """
    if not raw:
        return False, ''
    s = re.sub(r'\s+', '', str(raw)).upper()
    if not re.fullmatch(r'[A-Z]{2}\d{2}[A-Z0-9]{11,30}', s):
        return False, s
    # Move the four initial characters to the end, map letters A=10..Z=35,
    # then the whole number mod 97 must equal 1.
    rearranged = s[4:] + s[:4]
    digits = ''.join(str(int(ch, 36)) for ch in rearranged)
    return (int(digits) % 97 == 1), s


def _iso_date(raw):
    """'DD.MM.YYYY' (or already-ISO) -> 'YYYY-MM-DD'. Best-effort passthrough."""
    s = str(raw).strip()
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d.%m.%y'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return s


def _date_obj(raw):
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d.%m.%y'):
        try:
            return datetime.strptime(str(raw).strip(), fmt).date()
        except ValueError:
            pass
    return None


def _dec(v):
    try:
        return Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal('0.00')


def _amt(v):
    """Unsigned 2-decimal string. The sign is carried by CdtDbtInd, never here."""
    return f'{abs(_dec(v)):.2f}'


def _net(t):
    """Signed net of a transaction: credit (income) minus debit (expenses)."""
    return _dec(t.get('income') or 0) - _dec(t.get('expenses') or 0)


def _msg_id(prefix='BININ'):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    return f'{prefix}{stamp}-{uuid.uuid4().hex[:6]}'[:_REF_MAX]


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def order_chronological(transactions):
    """
    Return transactions oldest-first. Many banks (e.g. PostFinance) export
    newest-first, which would flip OPBD/CLBD. Reverse the whole list when the
    parseable dates run descending (preserves correct intra-day order).
    """
    parseable = [d for d in (_date_obj(t['date']) for t in transactions) if d]
    if len(parseable) >= 2 and parseable[0] > parseable[-1]:
        return list(reversed(transactions))
    return transactions


def _derive_balances(transactions, meta):
    """
    Return (opening, opening_date, closing, closing_date, warnings).
    OPBD/CLBD are mandatory in the standard, so we always produce both.

    Transactions must be chronological (oldest first). The running balance
    column is often sparse (banks print it only on some rows), so we anchor:
    a balance shown after row k implies opening = balance_k - net(rows 0..k).
    Any anchor yields the same opening when the column is consistent.
    """
    warnings = []
    op_date, cl_date = transactions[0]['date'], transactions[-1]['date']
    nets = [_net(t) for t in transactions]
    total = sum(nets, Decimal('0.00'))

    implied_openings = []
    cum = Decimal('0.00')
    for t, n in zip(transactions, nets):
        cum += n
        if t.get('balance') is not None:
            implied_openings.append(_dec(t['balance']) - cum)

    if implied_openings:
        opening = implied_openings[0]
        if any((o - opening).copy_abs() > Decimal('0.01') for o in implied_openings):
            warnings.append('The running-balance column is internally inconsistent; '
                            'using the first balance as the anchor. Verify before importing.')
        closing = opening + total
    elif meta.get('opening_balance') is not None:
        opening = _dec(meta['opening_balance'])
        closing = opening + total
        warnings.append('Balances computed from the opening balance you entered; '
                        'verify against your bank statement.')
    else:
        opening = Decimal('0.00')
        closing = total
        warnings.append('No balance column and no opening balance entered; opening '
                        'balance assumed 0.00. Verify before importing.')

    return opening, op_date, closing, cl_date, warnings


def _add_balance(stmt, code, amount, date, currency):
    """Append an OPBD/CLBD <Bal> element (CashBalance3: Tp, Amt, CdtDbtInd, Dt)."""
    bal = _sub(stmt, 'Bal')
    cdor = _sub(_sub(bal, 'Tp'), 'CdOrPrtry')
    _sub(cdor, 'Cd', code)
    amt = _sub(bal, 'Amt', _amt(amount))
    amt.set('Ccy', currency)
    _sub(bal, 'CdtDbtInd', 'CRDT' if _dec(amount) >= 0 else 'DBIT')
    _sub(_sub(bal, 'Dt'), 'Dt', _iso_date(date))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def build_camt053(transactions, meta):
    """
    transactions: exact output of converter.parse_to_transactions, each dict
        {'date' 'DD.MM.YYYY', 'doc', 'description', 'income', 'expenses', 'balance'}
    meta: {'iban', 'currency'='CHF', 'owner_name'='', 'opening_balance': float|None}

    Returns (xml_string, warnings).
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

    # Default namespace via a literal xmlns attribute → clean output with no ns0: prefixes.
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
        # AccountIdentification4Choice -> IBAN
        _sub(acct_id, 'IBAN', iban_norm)
    else:
        # AccountIdentification4Choice -> Othr (proprietary account number, e.g. PostFinance)
        _sub(_sub(acct_id, 'Othr'), 'Id', account_ref[:34])
        warnings.append(f"No IBAN given; account identified by number '{account_ref}'. "
                        "Banana may ask you to pick the destination account on import.")
    _sub(acct, 'Ccy', currency)
    if meta.get('owner_name'):
        _sub(_sub(acct, 'Ownr'), 'Nm', str(meta['owner_name'])[:140])

    _add_balance(stmt, 'OPBD', opening, op_date, currency)
    _add_balance(stmt, 'CLBD', closing, cl_date, currency)

    for t in transactions:
        if t.get('income') is not None:
            cd_ind, amount = 'CRDT', t['income']
        elif t.get('expenses') is not None:
            cd_ind, amount = 'DBIT', t['expenses']
        else:
            warnings.append(f"Skipped a row with no amount (date {t.get('date', '?')}).")
            continue

        # ReportEntry4 order: NtryRef, Amt, CdtDbtInd, Sts, BookgDt, ValDt, NtryDtls
        ntry = _sub(stmt, 'Ntry')
        doc_ref = str(t.get('doc') or '').strip()
        if doc_ref and doc_ref.lower() != 'nan':
            _sub(ntry, 'NtryRef', doc_ref[:_REF_MAX])
        amt = _sub(ntry, 'Amt', _amt(amount))
        amt.set('Ccy', currency)
        _sub(ntry, 'CdtDbtInd', cd_ind)
        _sub(ntry, 'Sts', 'BOOK')   # .08: wrap as <Sts><Cd>BOOK</Cd></Sts>
        _sub(_sub(ntry, 'BookgDt'), 'Dt', _iso_date(t['date']))
        _sub(_sub(ntry, 'ValDt'), 'Dt', _iso_date(t['date']))  # no separate value date available

        txdtls = _sub(_sub(ntry, 'NtryDtls'), 'TxDtls')
        desc = str(t.get('description') or '').strip()
        if desc:
            rmtinf = _sub(txdtls, 'RmtInf')
            for i in range(0, len(desc), _USTRD_MAX):
                _sub(rmtinf, 'Ustrd', desc[i:i + _USTRD_MAX])

    body = ET.tostring(doc, encoding='unicode')
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + body
    return xml, warnings


def summarize(transactions, meta):
    """Lightweight summary for the UI preview (period, balances, counts)."""
    transactions = order_chronological(transactions)
    opening, op_date, closing, cl_date, _ = _derive_balances(transactions, meta)
    return {
        'opening_balance': f'{opening:.2f}',
        'closing_balance': f'{closing:.2f}',
        'opening_date': _iso_date(op_date),
        'closing_date': _iso_date(cl_date),
        'currency': (meta.get('currency') or 'CHF').strip().upper(),
    }
