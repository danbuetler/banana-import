"""
Smart converter: reads any CSV/XLSX/PDF and maps columns to Banana format.
Banana output: tab-separated, columns Date | Doc | Description | Income | Expenses | Balance
"""

import re
import csv
import io
from datetime import datetime

import pandas as pd
import pdfplumber


DATE_PATTERNS = [
    '%d.%m.%Y', '%d.%m.%y', '%Y-%m-%d', '%d/%m/%Y',
    '%m/%d/%Y', '%d-%m-%Y', '%Y%m%d',
]

# Keywords that hint at which column role a header plays.
# income/expenses intentionally come before amount so that "Credit Amount" / "Debit Amount"
# match the specific role before the generic "amount" fallback.
ROLE_HINTS = {
    'date':        ['datum', 'date', 'buchungsdatum', 'valutadatum', 'buchungsdat',
                    'abschluss', 'buchung',
                    'started date', 'completed date', 'wertstellung'],
    'description': ['beschreibung', 'buchungstext', 'text', 'avisierungstext',
                    'description', 'verwendungszweck', 'details', 'memo',
                    'informationen', 'information',
                    'beschreibung 1', 'beschreibung 2', 'mittelung'],
    'income':      ['gutschrift', 'einnahme', 'income',
                    'credit amount', 'credit amt', 'credit',
                    'haben', 'eingang'],
    'expenses':    ['lastschrift', 'belastung', 'ausgabe', 'expenses',
                    'debit amount', 'debit amt', 'debit',
                    'soll', 'ausgang'],
    'amount':      ['betrag', 'amount', 'umsatz', 'netto'],
    'balance':     ['saldo', 'balance', 'kontostand', 'verfügbar'],
    'doc':         ['beleg', 'doc', 'belegnummer', 'buchungsnummer', 'referenz',
                    'ref', 'auftragsnummer'],
}

# Columns that look like account IDs or currency labels — never treat as amounts
_IDENTIFIER_RE = re.compile(r'account|konto|currency|währung|\bwhg\b|iban|nummer|number', re.I)


def _match_role(header):
    h = header.lower().strip()
    for role, keywords in ROLE_HINTS.items():
        for kw in keywords:
            if kw in h:  # keyword must appear in header, not the reverse (prevents false positives like "art" → "started date")
                # Don't assign account/currency/identifier columns to financial amount roles
                if role in ('income', 'expenses', 'amount') and _IDENTIFIER_RE.search(h):
                    return None
                return role
    return None


def _parse_date(raw):
    if not raw:
        return ''
    s = re.split(r'\s+', str(raw).strip())[0]
    for fmt in DATE_PATTERNS:
        try:
            return datetime.strptime(s, fmt).strftime('%d.%m.%Y')
        except ValueError:
            pass
    return str(raw).strip()


def _parse_amount(raw):
    if raw is None or str(raw).strip() in ('', '-', '+', 'nan'):
        return None
    s = str(raw).strip().replace("'", '').replace('’', '').replace('\xa0', '')
    # Determine decimal separator
    if ',' in s and '.' in s:
        if s.rfind('.') > s.rfind(','):
            s = s.replace(',', '')
        else:
            s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    s = s.replace('+', '')
    try:
        v = float(s)
        return v if v != 0 else None
    except ValueError:
        return None


def _df_to_transactions(df):
    """Map DataFrame columns to Banana transaction dicts using role hints."""
    col_roles = {}
    for col in df.columns:
        role = _match_role(str(col))
        if role and role not in col_roles:
            col_roles[role] = col

    # Fallback: if no date found, try first column
    if 'date' not in col_roles and len(df.columns) > 0:
        col_roles['date'] = df.columns[0]

    # If only one description-like column, use it
    if 'description' not in col_roles:
        for col in df.columns:
            if col not in col_roles.values():
                col_roles['description'] = col
                break

    transactions = []
    for _, row in df.iterrows():
        date_raw = row.get(col_roles.get('date', ''), '')
        date = _parse_date(date_raw)
        if not date:
            continue

        desc_col = col_roles.get('description', '')
        # Combine multiple description columns if mapped
        desc_parts = []
        for col in df.columns:
            if _match_role(str(col)) == 'description' and row.get(col):
                val = str(row[col]).strip()
                if val and val.lower() not in ('nan', ''):
                    desc_parts.append(val)
        description = ' | '.join(desc_parts) if desc_parts else str(row.get(desc_col, '')).strip()

        doc = ''
        if 'doc' in col_roles:
            doc = str(row.get(col_roles['doc'], '')).strip()
            doc = '' if doc.lower() == 'nan' else doc

        income = None
        expenses = None
        balance = None

        if 'income' in col_roles:
            income = _parse_amount(row.get(col_roles['income']))
        if 'expenses' in col_roles:
            expenses = _parse_amount(row.get(col_roles['expenses']))
        if 'balance' in col_roles:
            balance = _parse_amount(row.get(col_roles['balance']))

        # Single signed amount column
        if income is None and expenses is None and 'amount' in col_roles:
            amt = _parse_amount(row.get(col_roles['amount']))
            if amt is not None:
                if amt >= 0:
                    income = amt
                else:
                    expenses = abs(amt)

        transactions.append({
            'date': date,
            'doc': doc,
            'description': description,
            'income': income,
            'expenses': expenses,
            'balance': balance,
        })

    return transactions, col_roles


def _load_csv(filepath):
    """Load CSV with auto-detected separator, skipping metadata rows."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        raw = f.read()

    lines = raw.splitlines()

    # Count separators per line, find the line with the most consistent count
    for sep in [';', ',', '\t']:
        counts = [line.count(sep) for line in lines if line.strip()]
        if counts:
            max_c = max(counts)
            if max_c > 0:
                consistent = [i for i, c in enumerate(counts) if c == max_c]
                if len(consistent) > 3:
                    # Header is the first line with max count
                    all_lines = [l for l in lines if l.strip()]
                    header_idx = next(i for i, l in enumerate(all_lines) if l.count(sep) == max_c)
                    data = '\n'.join(all_lines[header_idx:])
                    df = pd.read_csv(io.StringIO(data), sep=sep, dtype=str, on_bad_lines='skip')
                    df.columns = [str(c).strip() for c in df.columns]
                    return df

    # Fallback: let pandas sniff it
    df = pd.read_csv(filepath, dtype=str, sep=None, engine='python', on_bad_lines='skip')
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _load_xlsx(filepath):
    df = pd.read_excel(filepath, dtype=str, header=None)
    # Find header row: first row where most cells are non-numeric strings
    for i, row in df.iterrows():
        non_empty = [str(v).strip() for v in row if str(v).strip() not in ('', 'nan')]
        numeric = sum(1 for v in non_empty if re.match(r'^-?[\d.,]+$', v))
        if len(non_empty) >= 2 and numeric < len(non_empty) / 2:
            df.columns = [str(v).strip() for v in row]
            df = df.iloc[i+1:].reset_index(drop=True)
            break
    return df


def _load_pdf(filepath):
    """Extract the largest table from each page and concatenate."""
    frames = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            best = max(tables, key=lambda t: sum(len(r) for r in t))
            if len(best) < 2:
                continue
            # Normalize multi-line headers (pdfplumber joins multi-line cells with \n)
            header = [re.sub(r'\s+', ' ', str(c)).strip() if c else '' for c in best[0]]
            rows = [[re.sub(r'\s+', ' ', str(c)).strip() if c else '' for c in r] for r in best[1:]]
            df = pd.DataFrame(rows, columns=header)
            frames.append(df)
    if not frames:
        return _load_pdf_positional(filepath)
    result = pd.concat(frames, ignore_index=True)
    result.columns = [str(c).strip() for c in result.columns]
    return result


def _load_pdf_positional(filepath):
    """
    Fallback for PDFs with no table objects (e.g. UBS e-banking exports).
    Uses word x-coordinates to reconstruct columns.
    """
    _DATE_RE = re.compile(r'^\d{1,2}[.\-/]\d{2}[.\-/]\d{4}$')
    _AMT_RE  = re.compile(r"^-?[\d'IOo]+[.,]\d{2}$")

    def _fix_ocr(s):
        return re.sub(r'O', '0', re.sub(r'(?<!\w)I', '1', s))

    transactions = []

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            if not words:
                continue

            # Group words by y-position (6pt grid)
            by_y = {}
            for w in words:
                y = round(w['top'] / 6) * 6
                by_y.setdefault(y, []).append(w)

            # Find header row containing both Belastung and Gutschrift
            belast_x = gutschr_x = header_y = None
            for y in sorted(by_y):
                row_map = {w['text'].lower(): w for w in by_y[y]}
                if 'belastung' in row_map and 'gutschrift' in row_map:
                    belast_x = row_map['belastung']['x0']
                    gutschr_x = row_map['gutschrift']['x0']
                    header_y = y
                    break

            if belast_x is None:
                continue

            col_mid = (belast_x + gutschr_x) / 2  # midpoint separates debit vs credit

            cur = None
            for y in (ky for ky in sorted(by_y) if ky > header_y):
                row = sorted(by_y[y], key=lambda w: w['x0'])

                # Skip column header words that leaked below header_y
                row_lower = {w['text'].lower() for w in row}
                if row_lower & {'belastung', 'gutschrift', 'valuta'}:
                    continue

                # Detect a date in the leftmost position
                left = [w for w in row if w['x0'] < 100]
                date_str = ''
                non_date = row
                if left:
                    # Handle split dates e.g. "31" + ".03.2025"
                    joined = ''.join(w['text'] for w in left[:2])
                    if _DATE_RE.match(joined):
                        date_str = joined
                        non_date = [w for w in row if w not in left[:2]]
                    elif _DATE_RE.match(left[0]['text']):
                        date_str = left[0]['text']
                        non_date = row[1:]

                if date_str:
                    # Only start a new transaction if there is an amount in the amount zone.
                    # Date-only rows are settlement-date continuation lines.
                    has_amount = any(
                        w['x0'] >= belast_x - 80 and w['x0'] < gutschr_x + 100
                        and _AMT_RE.match(_fix_ocr(w['text']))
                        for w in non_date
                    )
                    if has_amount:
                        if cur:
                            transactions.append(cur)
                        cur = {'date': date_str, 'desc': '', 'debit': '', 'credit': ''}
                        for w in non_date:
                            x, text = w['x0'], w['text']
                            if x >= belast_x - 80:   # amount zone
                                fixed = _fix_ocr(text)
                                if _AMT_RE.match(fixed) and x < gutschr_x + 100:
                                    if x < col_mid:
                                        cur['debit'] = fixed
                                    elif not cur['credit']:
                                        cur['credit'] = fixed
                            elif x < belast_x - 5:   # description zone
                                cur['desc'] = (cur['desc'] + ' ' + text).strip()
                    elif cur:
                        # Settlement date row or continuation — append description words
                        for w in non_date:
                            if w['x0'] < belast_x - 5:
                                cur['desc'] = (cur['desc'] + ' ' + w['text']).strip()
                elif cur and non_date:
                    # No date at left — continuation line
                    for w in non_date:
                        if w['x0'] < belast_x - 5:
                            cur['desc'] = (cur['desc'] + ' ' + w['text']).strip()

            if cur:
                transactions.append(cur)

    if not transactions:
        raise ValueError('No transactions found in PDF (tried table and positional parsers).')

    return pd.DataFrame([{
        'Abschluss':     t['date'],
        'Informationen': t['desc'],
        'Belastung':     t['debit'],
        'Gutschrift':    t['credit'],
    } for t in transactions])


def convert_to_banana(filepath):
    """
    Main entry point. Returns (banana_tsv_string, col_roles_dict, transaction_count, warnings).
    """
    warnings = []
    ext = filepath.rsplit('.', 1)[-1].lower()

    try:
        if ext == 'pdf':
            df = _load_pdf(filepath)
        elif ext in ('xls', 'xlsx', 'xlsm'):
            df = _load_xlsx(filepath)
        else:
            df = _load_csv(filepath)
    except Exception as e:
        raise ValueError(f'Could not read file: {e}')

    # Drop fully empty rows/cols
    df = df.dropna(how='all').reset_index(drop=True)
    df = df.loc[:, ~df.columns.str.match(r'^Unnamed')]

    if df.empty:
        raise ValueError('File appears to be empty or has no readable data.')

    transactions, col_roles = _df_to_transactions(df)

    if not transactions:
        warnings.append('No valid transactions found. Check that the file has a date column.')

    # Build ZKB-style semicolon CSV — readable by the Banana connector directly
    def _q(s):
        return '"' + str(s).replace('"', '""') + '"'

    def _amt(v):
        return f'{v:.2f}' if v is not None else ''

    lines = ['"Datum";"Buchungstext";"Belastung";"Gutschrift"']
    for t in transactions:
        lines.append(';'.join([
            _q(t['date']),
            _q(t['description']),
            _q(_amt(t['expenses'])),
            _q(_amt(t['income'])),
        ]))

    tsv = '\n'.join(lines)
    return tsv, col_roles, len(transactions), warnings
