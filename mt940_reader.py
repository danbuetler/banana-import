"""
MT940 / SWIFT bank-statement reader.

Parses an MT940 statement (the SWIFT :20:/:25:/:60F:/:61:/:86:/:62F: format
many banks still export — UBS, CS, etc.) into the same normalized transaction
list the rest of the converter speaks:

    {'date' 'DD.MM.YYYY', 'doc', 'description', 'income', 'expenses', 'balance'}

plus an authoritative meta dict drawn straight from the statement
(:25: account, :60F: opening, :62F: closing, currency). MT940 states its own
opening and closing book balances, so we pass them through rather than deriving
them — and we keep an explicit ":61: Balance brought forward" line as a normal
booking, exactly as the bank modeled it (opening stays at :60F:).

Pure stdlib (re only). Feeds both the Banana and Odoo CAMT.053 writers unchanged.
"""

import re

MT940_EXTS = ('mt940', 'sta', '940')

# A :61: statement line: value-date, optional entry-date, D/C mark (with optional
# R reversal prefix), optional funds-code letter, amount (comma decimal),
# N + 3-char transaction-type code, customer ref, optional //bank-ref.
_L61_RE = re.compile(
    r'^(\d{6})(\d{4})?(R?[CD])([A-Z])?(\d[\d.,]*)N([A-Z]{3})([^/]*)(?://(.*))?$'
)
# A balance field (:60F:/:62F:/:60M:/:62M:): D/C mark, YYMMDD date, 3-char ccy, amount.
_BAL_RE = re.compile(r'^([CD])(\d{6})([A-Z]{3})([\d.,]+)$')
# :86: leading structured code like 'Z04?' / 'F81?' / 'K25?' — strip it, keep the text.
_CODE_PREFIX_RE = re.compile(r'^[A-Z]?\d{0,2}\?')


def looks_like_mt940(filepath):
    """Content sniff so a SWIFT statement uploaded as .txt is still recognized."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(4096)
    except OSError:
        return False
    if '{1:' in head and '{4:' in head:
        return True
    return bool(re.search(r'^:20:', head, re.M) and re.search(r'^:61:', head, re.M))


def _amount(raw):
    """MT940 amount → float. Comma is the decimal separator; no thousands grouping."""
    return float(raw.replace(',', '.'))


def _split_fields(body):
    """Group lines into (tag, [lines]) pairs; non-tag lines extend the prior field."""
    fields = []
    for ln in body.splitlines():
        m = re.match(r'^:(\w+):(.*)$', ln)
        if m:
            fields.append((m.group(1), [m.group(2)]))
        elif fields:
            fields[-1][1].append(ln)
    return fields


def _finalize_description(pending):
    """If no :86: supplied a description, fall back to the :61: narrative lines."""
    if not pending.get('description'):
        parts = []
        for n in pending.get('_narr', []):
            if n.lower() not in ' '.join(parts).lower():
                parts.append(n)
        pending['description'] = ' — '.join(parts)
    pending.pop('_narr', None)


def parse_mt940(filepath):
    """
    Read an MT940 file. Returns (transactions, meta, warnings).

    transactions: list of {date, doc, description, income, expenses, balance}
    meta: {account_ref, owner, currency, opening_balance, closing_balance}
    """
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        raw = f.read()

    # Unwrap the SWIFT application block {4: ... -} if present.
    block4 = re.search(r'\{4:\s*(.*?)\s*-\}', raw, re.S)
    body = block4.group(1) if block4 else raw

    meta = {'account_ref': '', 'owner': '', 'currency': '',
            'opening_balance': None, 'closing_balance': None}
    transactions = []
    warnings = []
    pending = None  # current :61: dict awaiting its :86:

    def flush():
        if pending is not None:
            _finalize_description(pending)
            transactions.append(pending)

    for tag, content in _split_fields(body):
        head = content[0].strip()
        extra = [c.strip() for c in content[1:] if c.strip()]

        if tag == '25':
            # Account id — may carry "/currency" or extra qualifiers; keep the token.
            meta['account_ref'] = head.split('/')[0].strip() if '/' in head else head

        elif tag.startswith('60'):          # :60F: / :60M: opening balance
            b = _BAL_RE.match(head)
            if b:
                meta['currency'] = b.group(3)
                amt = _amount(b.group(4))
                meta['opening_balance'] = amt if b.group(1) == 'C' else -amt

        elif tag.startswith('62'):          # :62F: / :62M: closing balance
            b = _BAL_RE.match(head)
            if b:
                if not meta['currency']:
                    meta['currency'] = b.group(3)
                amt = _amount(b.group(4))
                meta['closing_balance'] = amt if b.group(1) == 'C' else -amt

        elif tag == '61':
            flush()
            m = _L61_RE.match(head)
            if not m:
                warnings.append(f'Skipped an unparseable :61: line: {head[:60]}')
                pending = None
                continue
            vd, _ed, mark, _fund, amount, _typ, cref, bref = m.groups()
            date = f'{vd[4:6]}.{vd[2:4]}.{2000 + int(vd[0:2])}'
            val = _amount(amount)
            is_credit = mark.endswith('C')
            pending = {
                'date': date,
                'doc': (bref or cref or '').strip()[:35],
                'description': '',
                'income': val if is_credit else None,
                'expenses': None if is_credit else val,
                'balance': None,
                '_narr': list(extra),
            }

        elif tag == '86':
            if pending is None:
                continue
            name = _CODE_PREFIX_RE.sub('', head).strip()
            parts = [name] if name else []
            for e in extra + pending.get('_narr', []):
                if e.lower() not in ' '.join(parts).lower():
                    parts.append(e)
            pending['description'] = ' — '.join(parts)
            pending.pop('_narr', None)

    flush()

    if not transactions:
        warnings.append('No :61: transaction lines found — is this a valid MT940 file?')

    return transactions, meta, warnings
