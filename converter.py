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

import mt940_reader


DATE_PATTERNS = [
    '%d.%m.%Y', '%d.%m.%y', '%Y-%m-%d', '%d/%m/%Y',
    '%m/%d/%Y', '%d-%m-%Y', '%Y%m%d',
]

# Keywords that hint at which column role a header plays.
# income/expenses intentionally come before amount so that "Credit Amount" / "Debit Amount"
# match the specific role before the generic "amount" fallback.
ROLE_HINTS = {
    'date':        ['datum', 'date', 'buchungsdatum', 'valutadatum', 'buchungsdat',
                    'abschluss',
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


# Belastung/Gutschrift-style direction indicators (credit-card exports). The column
# of these tokens decides debit vs credit; a separate magnitude column carries the amount.
_DEBIT_TOKENS = {'belastung', 'debit', 'soll', 'lastschrift', 'dr', 'withdrawal', 'auszahlung'}
_CREDIT_TOKENS = {'gutschrift', 'kredit', 'credit', 'haben', 'cr', 'einzahlung', 'deposit'}


def _direction_of(val):
    """Return 'debit', 'credit', or None for a direction-indicator cell value."""
    t = str(val).strip().lower()
    if t in _DEBIT_TOKENS:
        return 'debit'
    if t in _CREDIT_TOKENS:
        return 'credit'
    return None


def _detect_direction_column(df, exclude):
    """Find a column whose values are predominantly Belastung/Gutschrift-style
    direction indicators (e.g. a credit-card "Debit/Kredit" column). Returns the
    column name or None. Detected by content, not header — the header "Debit/Kredit"
    matches both debit and credit keywords and would otherwise be mis-read as an amount."""
    for col in df.columns:
        if col in exclude:
            continue
        vals = [str(v).strip() for v in df[col]
                if str(v).strip() and str(v).strip().lower() != 'nan']
        if not vals:
            continue
        hits = sum(1 for v in vals if _direction_of(v))
        if hits / len(vals) >= 0.8:
            return col
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


def _parse_amount(raw, allow_zero=False):
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
        # 0 means "no amount" for income/expenses, but a 0.00 balance is real.
        return v if (v != 0 or allow_zero) else None
    except ValueError:
        return None


def _row_description(df, row):
    """Join every description-role column for a row (e.g. UBS Description1/2/3)."""
    parts = []
    for col in df.columns:
        if _match_role(str(col)) == 'description':
            val = str(row.get(col, '')).strip()
            if val and val.lower() not in ('nan', ''):
                parts.append(val)
    return ' | '.join(parts)


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

    # Detect a Belastung/Gutschrift-style direction column (credit-card exports).
    # Its values decide debit vs credit; a single magnitude column ('Betrag') carries
    # the amount. Stop it (and its header, e.g. "Debit/Kredit") from acting as an amount.
    direction_col = _detect_direction_column(
        df, exclude={col_roles.get('date'), col_roles.get('description')})
    if direction_col:
        col_roles['direction'] = direction_col
        for r in ('expenses', 'income', 'amount'):
            if col_roles.get(r) == direction_col:
                del col_roles[r]

    # If only one description-like column, use it
    if 'description' not in col_roles:
        for col in df.columns:
            if col not in col_roles.values():
                col_roles['description'] = col
                break

    # Batch bookings (UBS collective / standing orders): a parent row carries the
    # Debit/Credit total + a Transaction no.; the individual beneficiaries follow as
    # detail rows with a BLANK date, a value in the "Individual amount" column, and
    # the SAME transaction no. Capture those as nested details on the parent so the
    # CAMT writer can expand them — dropping them would lose every payee/reference.
    indiv_col = next((c for c in df.columns
                      if re.search(r'individual\s*amount|einzelbetrag|teilbetrag',
                                   str(c), re.I)), None)
    txno_col = next((c for c in df.columns
                     if re.search(r'transaction\s*no|transaktionsnummer', str(c), re.I)), None)

    transactions = []
    last_parent = None
    for _, row in df.iterrows():
        date_raw = row.get(col_roles.get('date', ''), '')
        # A blank pandas cell stringifies to 'nan'; treat it as no date so batch
        # detail rows (blank date) are recognised, not mistaken for junk rows.
        date = '' if str(date_raw).strip().lower() in ('', 'nan', 'nat') else _parse_date(date_raw)

        # Detail line of a collective booking → attach to the most recent parent.
        if not date and indiv_col is not None and last_parent is not None:
            indiv = _parse_amount(row.get(indiv_col))
            same_txn = (txno_col is None or not last_parent.get('_txno')
                        or str(row.get(txno_col, '')).strip() == last_parent['_txno'])
            if indiv is not None and same_txn:
                last_parent.setdefault('details', []).append({
                    'description': _row_description(df, row),
                    'income': abs(indiv) if indiv > 0 else None,
                    'expenses': abs(indiv) if indiv < 0 else None,
                })
                continue

        if not date:
            continue
        # Skip junk/total rows (e.g. "TOTAL OF COLUMN"): a real date has no letters.
        if any(c.isalpha() for c in date):
            continue

        desc_col = col_roles.get('description', '')
        description = _row_description(df, row) or str(row.get(desc_col, '')).strip()

        doc = ''
        if 'doc' in col_roles:
            doc = str(row.get(col_roles['doc'], '')).strip()
            doc = '' if doc.lower() == 'nan' else doc

        income = None
        expenses = None
        balance = None

        if 'income' in col_roles:
            income = _parse_amount(row.get(col_roles['income']))
            if income is not None and income < 0:
                income = abs(income)
        if 'expenses' in col_roles:
            expenses = _parse_amount(row.get(col_roles['expenses']))
            if expenses is not None and expenses < 0:
                expenses = abs(expenses)
        if 'balance' in col_roles:
            balance = _parse_amount(row.get(col_roles['balance']), allow_zero=True)

        # Magnitude column + direction indicator (credit-card exports): the
        # Belastung/Gutschrift column decides the side, |Betrag| the amount.
        if income is None and expenses is None and 'direction' in col_roles and 'amount' in col_roles:
            amt = _parse_amount(row.get(col_roles['amount']))
            direction = _direction_of(row.get(col_roles['direction']))
            if amt is not None and direction:
                if direction == 'debit':
                    expenses = abs(amt)
                else:
                    income = abs(amt)

        # Single signed amount column
        if income is None and expenses is None and 'amount' in col_roles:
            amt = _parse_amount(row.get(col_roles['amount']))
            if amt is not None:
                if amt >= 0:
                    income = amt
                else:
                    expenses = abs(amt)

        txn = {
            'date': date,
            'doc': doc,
            'description': description,
            'income': income,
            'expenses': expenses,
            'balance': balance,
        }
        if txno_col is not None:
            txn['_txno'] = str(row.get(txno_col, '')).strip()
        transactions.append(txn)
        last_parent = txn

    # Internal grouping key — not part of the public transaction shape.
    for t in transactions:
        t.pop('_txno', None)

    return transactions, col_roles


def _load_csv(filepath):
    """Load CSV with auto-detected separator, skipping metadata rows."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        raw = f.read()

    lines = raw.splitlines()
    all_lines = [l for l in lines if l.strip()]

    # Count fields per line using proper CSV parsing (respects quoting so embedded
    # delimiters inside quoted fields don't inflate the count — fixes UBS exports)
    for sep in [';', ',', '\t']:
        field_counts = []
        nonempty_counts = []
        for line in all_lines:
            try:
                row = next(csv.reader([line], delimiter=sep, quotechar='"'), [])
            except Exception:
                row = line.split(sep)
            field_counts.append(len(row))
            nonempty_counts.append(sum(1 for c in row if c.strip()))

        if not field_counts:
            continue

        max_c = max(field_counts)
        if max_c < 2:
            continue

        # The header + data rows span the widest field count. Metadata rows padded
        # with trailing delimiters (e.g. Sygnum's "Balance: CHF 19671.6;;;") also
        # reach that width, so additionally require ≥2 non-empty cells to exclude
        # single-label padding — without this the first padded metadata line would
        # be picked as the header. (≥2 also keeps the original "header + at least
        # one data row" rule that fixes short UBS exports.)
        consistent = [i for i, c in enumerate(field_counts)
                      if c == max_c and nonempty_counts[i] >= 2]
        if len(consistent) >= 2:
            header_idx = consistent[0]
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


def parse_to_transactions(filepath):
    """
    Read any CSV/XLSX/PDF and return (transactions, col_roles, warnings).

    transactions: list of dicts, each
        {'date', 'doc', 'description', 'income', 'expenses', 'balance'}.
    Shared by both the Banana-TSV path (convert_to_banana) and the
    CAMT.053 path (camt_writer.build_camt053). Pure parsing — no output format.
    """
    warnings = []
    ext = filepath.rsplit('.', 1)[-1].lower()

    # MT940 / SWIFT statements have their own field grammar (not a column table),
    # so they get a dedicated reader. Detected by extension, or by content sniff
    # for SWIFT files uploaded as .txt. col_roles is empty — there are no columns.
    if ext in mt940_reader.MT940_EXTS or (ext == 'txt' and mt940_reader.looks_like_mt940(filepath)):
        transactions, _meta, mt_warnings = mt940_reader.parse_mt940(filepath)
        if not transactions:
            mt_warnings.append('No valid transactions found in the MT940 file.')
        return transactions, {}, mt_warnings

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

    return transactions, col_roles, warnings


_IBAN_RE = re.compile(r'\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b')
_ACCTNO_RE = re.compile(r'\b(\d{2,}-\d+-\d+)\b')   # PostFinance-style, e.g. 1777097-31-1
_CCY_RE = re.compile(r'\b(CHF|EUR|USD|GBP)\b')
_ACCT_LABELS = ('account', 'konto', 'kontonummer', 'account number', 'iban', 'compte')
_HEADER_STOP = ('booking date', 'date', 'datum', 'buchungsdatum', 'valuta', 'value date')
_DATEISH_RE = re.compile(r'^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$')


def _iban_ok(cand):
    """mod-97 check so transaction references like 'DA00...' are not mistaken for an IBAN."""
    s = cand.replace(' ', '').upper()
    if not re.fullmatch(r'[A-Z]{2}\d{2}[A-Z0-9]{11,30}', s):
        return False
    rearranged = s[4:] + s[:4]
    return int(''.join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def sniff_account_meta(filepath):
    """
    Best-effort extraction of account id / owner / currency from a CSV/TXT
    header block (the metadata rows banks put ABOVE the transactions, which the
    parser otherwise discards). Only the lines before the transaction table are
    scanned, so payment references inside bookings are not mistaken for the
    account. Returns {'account_ref','owner','currency'} (empty when not found).
    Never raises.
    """
    out = {'account_ref': '', 'owner': '', 'currency': ''}
    if filepath.rsplit('.', 1)[-1].lower() not in ('csv', 'txt'):
        return out
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.read(8192).splitlines()[:40]
    except OSError:
        return out

    # Keep only the metadata block: stop at the column-header row or first data row.
    header_lines = []
    for line in lines:
        row = next(csv.reader([line]), [])
        first = row[0].strip().lower() if row else ''
        if first in _HEADER_STOP or _DATEISH_RE.match(first):
            break
        header_lines.append((row, line))

    text = '\n'.join(l for _, l in header_lines)

    # Prefer a checksum-valid IBAN in the header. Tolerate space-grouped IBANs
    # (e.g. "CH93 0076 2011 ...", the common Swiss print format); the mod-97 check
    # rejects any accidental run that isn't a real IBAN.
    for cand in re.findall(r'[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}', text.upper()):
        norm = re.sub(r'\s+', '', cand)
        if _iban_ok(norm):
            out['account_ref'] = norm
            break

    # Otherwise a labelled account line (PostFinance:
    # Account,"Current account,1777097-31-1,3W AG, Baar").
    for row, _ in header_lines:
        if row and row[0].strip().lower() in _ACCT_LABELS:
            payload = ','.join(c for c in row[1:] if c)
            am = _ACCTNO_RE.search(payload)
            if am:
                if not out['account_ref']:
                    out['account_ref'] = am.group(1)
                owner = payload[am.end():].strip(' ,;')
                if owner:
                    out['owner'] = owner
            break

    # Label-and-value-in-one-cell account lines (Sygnum: "Account no.: 84.010.965.184.8").
    # Only used when no IBAN/labelled account was found above.
    if not out['account_ref']:
        am = re.search(r'account\s*(?:no|number|nr)\.?\s*[:.]?\s*([0-9][0-9.\-\s]{4,})', text, re.I)
        if am:
            out['account_ref'] = am.group(1).strip().rstrip('.').strip()

    cm = _CCY_RE.search(text)
    if cm:
        out['currency'] = cm.group(1)

    return out


def convert_to_banana(filepath):
    """
    Main entry point. Returns (banana_tsv_string, col_roles_dict, transaction_count, warnings).
    """
    transactions, col_roles, warnings = parse_to_transactions(filepath)

    # Build ZKB-style semicolon CSV — readable by the Banana connector directly
    def _neutralize(s):
        # CSV/formula-injection guard for USER-derived text (date/description):
        # prefix a leading = + - @ with an apostrophe so spreadsheets treat it as
        # literal text. Amount columns are app-formatted numerics, left untouched.
        s = str(s)
        if s and s[0] in ('=', '+', '-', '@'):
            return "'" + s
        return s

    def _q(s):
        return '"' + str(s).replace('"', '""') + '"'

    def _amt(v):
        return f'{v:.2f}' if v is not None else ''

    lines = ['"Datum";"Buchungstext";"Belastung";"Gutschrift"']
    for t in transactions:
        lines.append(';'.join([
            _q(_neutralize(t['date'])),
            _q(_neutralize(t['description'])),
            _q(_amt(t['expenses'])),
            _q(_amt(t['income'])),
        ]))

    tsv = '\n'.join(lines)
    return tsv, col_roles, len(transactions), warnings
