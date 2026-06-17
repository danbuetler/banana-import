"""
CAMT.053 (ISO 20022) reader.

Parses a camt.053 bank-to-customer statement XML into a plain dict structure
for display: account, period, balances, and entries — plus a reconciliation
check (opening + sum of signed entries == stated closing).

Namespace / version agnostic (.02 / .04 / .08): every element is matched on its
local name only, so a missing or differing xmlns never breaks parsing.

This is the counterpart to camt_writer.build_camt053 (which goes the other way).
No third-party dependencies — stdlib ElementTree + decimal only.
"""

import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation


# --------------------------------------------------------------------------- #
# Namespace-agnostic element helpers
# --------------------------------------------------------------------------- #

def _local(tag):
    """'{urn:...}Stmt' -> 'Stmt'. Strips any XML namespace."""
    return tag.split('}')[-1]


def _find(el, name):
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _findall(el, name):
    if el is None:
        return []
    return [c for c in el if _local(c.tag) == name]


def _text(el, name):
    c = _find(el, name)
    return c.text.strip() if c is not None and c.text and c.text.strip() else None


def _dec(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal('0')


def _date(wrapper):
    """A date wrapper (BookgDt / ValDt / Bal>Dt) holds <Dt> (date) or <DtTm>."""
    if wrapper is None:
        return None
    d = _text(wrapper, 'Dt') or _text(wrapper, 'DtTm')
    return d.split('T')[0] if d else None


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def parse_camt053(source):
    """
    source: a file path, an open file, or XML bytes/str.
    Returns a list of statement dicts (one per <Stmt>), each:
        {account, currency, owner, period_from, period_to,
         opening, opening_date, closing, closing_date,
         balances: [{code, amount, date, ccy}],
         entries:  [{booking_date, value_date, cd_ind, amount, status,
                     ref, description, currency}],
         reconciliation: {opening, movements, expected_closing,
                          stated_closing, difference, ok} | None}
    Raises ValueError on anything that is not a readable camt.053.
    """
    try:
        if isinstance(source, (bytes, bytearray)):
            root = ET.fromstring(source)
        elif isinstance(source, str) and source.lstrip().startswith('<'):
            root = ET.fromstring(source)
        else:
            root = ET.parse(source).getroot()
    except ET.ParseError as e:
        raise ValueError(f'Not valid XML: {e}')

    # Accept either <Document> wrapping <BkToCstmrStmt>, or that element directly.
    bk = _find(root, 'BkToCstmrStmt')
    if bk is None and _local(root.tag) == 'BkToCstmrStmt':
        bk = root
    if bk is None:
        raise ValueError('Not a CAMT.053 bank statement (no <BkToCstmrStmt> element).')

    statements = [_parse_stmt(s) for s in _findall(bk, 'Stmt')]
    if not statements:
        raise ValueError('No <Stmt> found in the file.')
    return statements


# --------------------------------------------------------------------------- #
# Per-statement parsing
# --------------------------------------------------------------------------- #

def _parse_stmt(stmt):
    account, owner, currency = None, None, None
    acct = _find(stmt, 'Acct')
    if acct is not None:
        idel = _find(acct, 'Id')
        if idel is not None:
            account = _text(idel, 'IBAN')
            if not account:
                othr = _find(idel, 'Othr')
                account = _text(othr, 'Id') if othr is not None else None
        currency = _text(acct, 'Ccy')
        owner = _text(_find(acct, 'Ownr'), 'Nm')

    frto = _find(stmt, 'FrToDt')
    period_from = _text(frto, 'FrDt') if frto is not None else None
    period_to = _text(frto, 'ToDt') if frto is not None else None
    if period_from:
        period_from = period_from.split('T')[0]
    if period_to:
        period_to = period_to.split('T')[0]

    # Balances. OPBD/PRCD = opening anchor, CLBD = closing anchor.
    balances = []
    opening = closing = None
    opening_date = closing_date = None
    for bal in _findall(stmt, 'Bal'):
        tp = _find(bal, 'Tp')
        cdor = _find(tp, 'CdOrPrtry') if tp is not None else None
        code = (_text(cdor, 'Cd') or _text(cdor, 'Prtry')) if cdor is not None else None
        amt_el = _find(bal, 'Amt')
        amt = _dec(amt_el.text) if amt_el is not None else Decimal('0')
        ccy = amt_el.get('Ccy') if amt_el is not None else None
        signed = amt if _text(bal, 'CdtDbtInd') == 'CRDT' else -amt
        bdate = _date(_find(bal, 'Dt'))
        balances.append({'code': code, 'amount': f'{signed:.2f}', 'date': bdate, 'ccy': ccy})
        if code in ('OPBD', 'PRCD') and opening is None:
            opening, opening_date = signed, bdate
        elif code == 'CLBD':
            closing, closing_date = signed, bdate

    # Entries.
    entries = []
    movements = Decimal('0')
    for ntry in _findall(stmt, 'Ntry'):
        amt_el = _find(ntry, 'Amt')
        amt = _dec(amt_el.text) if amt_el is not None else Decimal('0')
        ccy = amt_el.get('Ccy') if amt_el is not None else None
        cd_ind = _text(ntry, 'CdtDbtInd') or 'CRDT'
        signed = amt if cd_ind == 'CRDT' else -amt
        movements += signed

        # .04: <Sts>BOOK</Sts>; .08: <Sts><Cd>BOOK</Cd></Sts>
        status = _text(ntry, 'Sts')
        if status is None:
            status = _text(_find(ntry, 'Sts'), 'Cd')

        # Collective booking: ≥2 TxDtls = a batch entry whose Ntry total is the
        # bank-side line and whose TxDtls are the individual beneficiary splits.
        # Surface the splits so the preview matches what Banana will import.
        txdtls_list = []
        has_batch = False
        for ntrydtls in _findall(ntry, 'NtryDtls'):
            if _find(ntrydtls, 'Btch') is not None:
                has_batch = True
            txdtls_list += _findall(ntrydtls, 'TxDtls')
        details = []
        if has_batch or len(txdtls_list) >= 2:
            for txd in txdtls_list:
                td_amt_el = _find(txd, 'Amt')
                td_amt = _dec(td_amt_el.text) if td_amt_el is not None else Decimal('0')
                td_ind = _text(txd, 'CdtDbtInd') or cd_ind
                td_signed = td_amt if td_ind == 'CRDT' else -td_amt
                td_rmt = _find(txd, 'RmtInf')
                td_desc = ' '.join(u.text.strip() for u in _findall(td_rmt, 'Ustrd')
                                   if u is not None and u.text)
                details.append({'cd_ind': td_ind, 'amount': f'{td_signed:.2f}',
                                'description': td_desc})

        # For a batch entry the parent (bank-side) label is AddtlNtryInf; the
        # beneficiary text lives in the splits.
        description = (_text(ntry, 'AddtlNtryInf') if details else None) or _entry_description(ntry)

        entries.append({
            'booking_date': _date(_find(ntry, 'BookgDt')),
            'value_date': _date(_find(ntry, 'ValDt')),
            'cd_ind': cd_ind,
            'amount': f'{signed:.2f}',
            'status': status,
            'ref': _text(ntry, 'NtryRef') or _text(ntry, 'AcctSvcrRef'),
            'description': description,
            'details': details,
            'currency': ccy,
        })

    reconciliation = None
    if opening is not None and closing is not None:
        expected = opening + movements
        diff = expected - closing
        reconciliation = {
            'opening': f'{opening:.2f}',
            'movements': f'{movements:.2f}',
            'expected_closing': f'{expected:.2f}',
            'stated_closing': f'{closing:.2f}',
            'difference': f'{diff:.2f}',
            'ok': abs(diff) <= Decimal('0.01'),
        }

    return {
        'account': account,
        'currency': currency,
        'owner': owner,
        'period_from': period_from,
        'period_to': period_to,
        'opening': f'{opening:.2f}' if opening is not None else None,
        'opening_date': opening_date,
        'closing': f'{closing:.2f}' if closing is not None else None,
        'closing_date': closing_date,
        'balances': balances,
        'entries': entries,
        'reconciliation': reconciliation,
    }


def _entry_description(ntry):
    """
    Collect human-readable remittance text. Preferred source is
    NtryDtls/TxDtls/RmtInf/Ustrd (possibly split across several Ustrd); falls
    back to an entry-level RmtInf, then AddtlNtryInf.
    """
    parts = []
    for ntrydtls in _findall(ntry, 'NtryDtls'):
        for txdtls in _findall(ntrydtls, 'TxDtls'):
            rmt = _find(txdtls, 'RmtInf')
            parts += [u.text.strip() for u in _findall(rmt, 'Ustrd') if u is not None and u.text]
    rmt = _find(ntry, 'RmtInf')
    parts += [u.text.strip() for u in _findall(rmt, 'Ustrd') if u is not None and u.text]
    if not parts:
        addtl = _text(ntry, 'AddtlNtryInf')
        if addtl:
            parts.append(addtl)
    return ' '.join(parts)
