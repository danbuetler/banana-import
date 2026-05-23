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

# Keywords that hint at which column role a header plays
ROLE_HINTS = {
    'date':        ['datum', 'date', 'buchungsdatum', 'valutadatum', 'buchungsdat',
                    'started date', 'completed date', 'wertstellung'],
    'description': ['beschreibung', 'buchungstext', 'text', 'avisierungstext',
                    'description', 'verwendungszweck', 'details', 'memo',
                    'beschreibung 1', 'beschreibung 2', 'mittelung'],
    'amount':      ['betrag', 'amount', 'umsatz', 'netto'],
    'income':      ['gutschrift', 'einnahme', 'income', 'credit', 'haben', 'eingang'],
    'expenses':    ['lastschrift', 'belastung', 'ausgabe', 'expenses', 'debit',
                    'soll', 'ausgang'],
    'balance':     ['saldo', 'balance', 'kontostand', 'verfügbar'],
    'doc':         ['beleg', 'doc', 'belegnummer', 'buchungsnummer', 'referenz',
                    'ref', 'auftragsnummer'],
}


def _match_role(header):
    h = header.lower().strip()
    for role, keywords in ROLE_HINTS.items():
        for kw in keywords:
            if kw in h or h in kw:
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
            header = [str(c).strip() if c else '' for c in best[0]]
            rows = [[str(c).strip() if c else '' for c in r] for r in best[1:]]
            df = pd.DataFrame(rows, columns=header)
            frames.append(df)
    if not frames:
        raise ValueError('No tables found in PDF.')
    result = pd.concat(frames, ignore_index=True)
    result.columns = [str(c).strip() for c in result.columns]
    return result


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

    # Build Banana TSV
    lines = ['Date\tDoc\tDescription\tIncome\tExpenses\tBalance']
    for t in transactions:
        def fmt(v):
            if v is None:
                return ''
            return f'{v:.2f}'
        lines.append('\t'.join([
            t['date'], t['doc'], t['description'],
            fmt(t['income']), fmt(t['expenses']), fmt(t['balance']),
        ]))

    tsv = '\n'.join(lines)
    return tsv, col_roles, len(transactions), warnings
