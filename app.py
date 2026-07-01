import os
import re
import sys
import time
import uuid
from xml.dom import minidom
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
import converter
from converter import convert_to_banana, parse_to_transactions
import camt_writer
import odoo_camt_writer
import ai_extract
import camt_reader
import camt_xlsx
import mt940_reader
import banana_live
import invoice_extract
import invoice_booking
import dividend_extract
import dividend_booking
import portfolio_extract
import portfolio_booking

APP_VERSION = "1.16.2"
BUILD_DATE = "2026-07-01"

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB
UPLOAD_DIR = '/tmp/banana'
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Startup config check: warn (don't crash) — CSV/XLSX conversion works without a
# key; only PDF extraction needs it. See TOOLS_SECURITY_BACKLOG.md.
if not os.environ.get("ANTHROPIC_API_KEY"):
    sys.stderr.write("[CONFIG WARNING] banana-import: ANTHROPIC_API_KEY not set — "
                     "PDF extraction will be unavailable (CSV/XLSX still work).\n")

SESSIONS = {}
SESSION_TTL_SECONDS = 3600  # downloads expire after 1h; files are removed on eviction

# Upload allowlist for /convert — mirrors what the converter actually accepts:
# bank/credit-card statements as CSV/TSV/TXT, Excel, PDF, and MT940/SWIFT files.
# Reject anything else with a clean 400 (see file-to-md app.py for the pattern).
CONVERT_ALLOWED_EXTS = {
    'csv', 'tsv', 'txt',
    'xls', 'xlsx', 'xlsm',
    'pdf',
    'mt940', 'sta', '940',
}


def _evict_expired_sessions():
    cutoff = time.time() - SESSION_TTL_SECONDS
    for sid in [k for k, v in SESSIONS.items() if v.get('created', 0) < cutoff]:
        s = SESSIONS.pop(sid, None)
        if s:
            try:
                os.remove(s['path'])
            except OSError:
                pass


@app.route('/')
def index():
    return render_template('index.html', version=APP_VERSION, build_date=BUILD_DATE)


@app.route('/api/version')
def version():
    return jsonify({'version': APP_VERSION, 'build_date': BUILD_DATE})


@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400

    _evict_expired_sessions()
    output_format = request.form.get('output_format', 'camt053')
    session_id = str(uuid.uuid4())
    orig_name = os.path.splitext(secure_filename(f.filename))[0] or 'statement'
    ext = os.path.splitext(f.filename)[1].lower().lstrip('.')
    if ext not in CONVERT_ALLOWED_EXTS:
        return jsonify({
            'error': f'Unsupported file type ".{ext}". '
                     f'Supported: {", ".join(sorted(CONVERT_ALLOWED_EXTS))}'
        }), 400
    upload_path = os.path.join(UPLOAD_DIR, f'{session_id}.{ext}')
    f.save(upload_path)

    try:
        if output_format in ('camt053', 'camt053_odoo'):
            return _convert_camt(session_id, orig_name, upload_path, output_format)
        return _convert_tsv(session_id, orig_name, upload_path)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if os.path.exists(upload_path):
            os.remove(upload_path)


def _convert_tsv(session_id, orig_name, upload_path):
    tsv, col_roles, count, warnings = convert_to_banana(upload_path)

    out_path = os.path.join(UPLOAD_DIR, f'{session_id}_banana.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(tsv)

    SESSIONS[session_id] = {
        'path': out_path,
        'filename': f'banana_{orig_name}.txt',
        'mimetype': 'text/plain',
        'created': time.time(),
    }

    return jsonify({
        'session_id': session_id,
        'format': 'banana_tsv',
        'count': count,
        'warnings': warnings,
        'mapping': {role: col for role, col in col_roles.items()},
        'preview': tsv.strip().split('\n')[:21],
    })


def _camt_filename(prefix, account_ref, currency, from_date, to_date, fallback):
    """Build a self-describing CAMT filename: <prefix>_<token>_<currency>_<from>_<to>.xml
    Token precedence (per Daniel): last 5 digits of the IBAN → else the full
    account number → else the source-name slug (bank name isn't in the statement).
    currency + period make the files sortable."""
    ref = (account_ref or '').strip()
    if re.match(r'^[A-Za-z]{2}\d{2}', ref.replace(' ', '')):
        digits = re.sub(r'\D', '', ref)        # IBAN → last 5 digits
        token = digits[-5:]
    elif re.sub(r'[^A-Za-z0-9]', '', ref):
        token = re.sub(r'[^A-Za-z0-9]', '', ref)   # proprietary account number (full)
    else:
        token = re.sub(r'[^A-Za-z0-9]+', '-', fallback).strip('-') or 'statement'
    parts = [prefix, token, (currency or 'CHF').upper()]
    period = '_'.join(d for d in (from_date, to_date) if d)
    if period:
        parts.append(period)
    return '_'.join(parts) + '.xml'


def _convert_camt(session_id, orig_name, upload_path, output_format='camt053'):
    is_odoo = output_format == 'camt053_odoo'
    ext = upload_path.rsplit('.', 1)[-1].lower()
    extra = []

    # 1) Transactions + source-derived metadata.
    #    PDFs (any issuer/layout) go through AI extraction; CSV/XLSX use the
    #    deterministic heuristic parser + header sniffer.
    if ext in mt940_reader.MT940_EXTS or (ext == 'txt' and mt940_reader.looks_like_mt940(upload_path)):
        # MT940 states its own :60F:/:62F: opening & closing book balances,
        # account and currency — pass them through as authoritative.
        transactions, mt_meta, warnings = mt940_reader.parse_mt940(upload_path)
        src_meta = {'account_ref': mt_meta['account_ref'], 'owner': mt_meta['owner'],
                    'currency': mt_meta['currency'],
                    'opening_balance': mt_meta['opening_balance'],
                    'closing_balance': mt_meta['closing_balance']}
        col_roles = {}
    elif ext == 'pdf':
        if not ai_extract.available():
            raise ValueError('PDF extraction needs ANTHROPIC_API_KEY configured on the server.')
        transactions, src_meta, warnings = ai_extract.extract_transactions_from_pdf(upload_path)
        col_roles = {}
    else:
        transactions, col_roles, warnings = parse_to_transactions(upload_path)
        sniff = converter.sniff_account_meta(upload_path)
        src_meta = {'account_ref': sniff['account_ref'], 'owner': sniff['owner'],
                    'currency': sniff['currency'], 'opening_balance': None, 'closing_balance': None}

    if not transactions:
        raise ValueError('No transactions found in the file.')

    # 2) Form fields override source-derived values. Account id is OPTIONAL —
    #    the destination account is chosen during Banana import.
    form_iban = request.form.get('iban', '').strip()
    account_ref = form_iban or src_meta.get('account_ref', '')
    if account_ref and not form_iban:
        extra.append(f"Account auto-detected from file: {account_ref}")
    if re.match(r'^[A-Za-z]{2}\d{2}', account_ref.replace(' ', '')):
        ok, _ = camt_writer.validate_iban(account_ref)
        if not ok:
            raise ValueError('That looks like an IBAN but fails the checksum — please check it.')

    currency = (request.form.get('currency') or src_meta.get('currency') or 'CHF').strip().upper()
    owner_name = request.form.get('owner_name', '').strip() or src_meta.get('owner', '')

    opening_balance = src_meta.get('opening_balance')
    ob_raw = request.form.get('opening_balance', '').strip()
    if ob_raw:
        try:
            opening_balance = float(ob_raw.replace("'", '').replace('’', '').replace(',', '.'))
        except ValueError:
            raise ValueError('Opening balance is not a valid number.')

    meta = {'account_ref': account_ref, 'currency': currency, 'owner_name': owner_name,
            'opening_balance': opening_balance, 'closing_balance': src_meta.get('closing_balance')}
    if is_odoo:
        xml_str, camt_warnings = odoo_camt_writer.build_camt053_odoo(transactions, meta)
        result_format = 'camt053_odoo'
        prefix = 'camt053_odoo'
    else:
        xml_str, camt_warnings = camt_writer.build_camt053(transactions, meta)
        result_format = 'camt053'
        prefix = 'camt053'

    # Self-describing, sortable filename from the statement's own metadata —
    # account/IBAN (identifies the bank), currency and period — instead of the
    # uploaded source filename. Falls back to the source name if metadata is thin.
    summ = camt_writer.summarize(transactions, meta)
    out_filename = _camt_filename(prefix, account_ref, currency,
                                  summ.get('opening_date'), summ.get('closing_date'), orig_name)

    out_path = os.path.join(UPLOAD_DIR, f'{session_id}_camt.xml')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(xml_str)

    SESSIONS[session_id] = {
        'path': out_path,
        'filename': out_filename,
        'mimetype': 'application/xml',
        'created': time.time(),
    }

    # Parse the just-generated XML back into a readable, reconciled view so the
    # UI can show the same entry table as the CAMT reader (easier to reconcile
    # than raw XML). Works for both the Banana and Odoo variants.
    try:
        statements = camt_reader.parse_camt053(xml_str)
    except Exception:
        statements = []

    return jsonify({
        'session_id': session_id,
        'format': result_format,
        'count': len(transactions),
        'warnings': extra + warnings + camt_warnings,
        'mapping': {role: col for role, col in col_roles.items()},
        'statements': statements,
        'iban': account_ref,
        'summary': summ,
    })


@app.route('/read', methods=['POST'])
def read_camt():
    """Parse an existing CAMT.053 XML into a readable view + reconciliation,
    and stage an .xlsx export (Daniel's house layout) for download."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400

    _evict_expired_sessions()
    data = f.read()
    try:
        statements = camt_reader.parse_camt053(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Could not read the file: {e}'}), 500

    session_id = str(uuid.uuid4())
    orig_name = os.path.splitext(secure_filename(f.filename))[0] or 'statement'
    out_path = os.path.join(UPLOAD_DIR, f'{session_id}_read.xlsx')
    with open(out_path, 'wb') as fh:
        fh.write(camt_xlsx.build_xlsx(statements, orig_name))

    SESSIONS[session_id] = {
        'path': out_path,
        'filename': f'{orig_name}.xlsx',
        'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'created': time.time(),
    }

    return jsonify({
        'session_id': session_id,
        'format': 'read',
        'count': sum(len(s['entries']) for s in statements),
        'statements': statements,
    })


# --------------------------------------------------------------------------- #
# Invoices → Banana (AP / Kreditoren) — double-entry accrual bookings
# --------------------------------------------------------------------------- #

@app.route('/invoices/clients')
def invoices_clients():
    """List the client .ac2 files currently open in Banana (for the client picker)."""
    if not banana_live.available():
        return jsonify({'available': False, 'files': [],
                        'error': 'BANANA_TOKEN not set. Open Banana, enable its webserver, '
                                 'and add BANANA_TOKEN to banana-import/.env.'})
    try:
        return jsonify({'available': True, 'files': banana_live.list_open_files()})
    except banana_live.BananaUnavailable as e:
        return jsonify({'available': False, 'files': [], 'error': str(e)})


@app.route('/invoices/extract', methods=['POST'])
def invoices_extract():
    """Extract dropped AP invoices and propose double-entry bookings against the
    selected client's live chart of accounts + learned vendor map."""
    client_file = (request.form.get('client_file') or '').strip()
    if not client_file:
        return jsonify({'error': 'Select a client (an open Banana file) first.'}), 400
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return jsonify({'error': 'No invoice PDFs uploaded.'}), 400
    if not invoice_extract.available():
        return jsonify({'error': 'Invoice extraction needs ANTHROPIC_API_KEY configured on the server.'}), 400

    try:
        profile = banana_live.get_client_profile(client_file)
    except banana_live.BananaUnavailable as e:
        return jsonify({'error': str(e)}), 400

    slug = invoice_booking.client_slug(client_file)
    vmap = invoice_booking.load_vendor_map(slug)

    _evict_expired_sessions()
    rows = []
    for f in files:
        name = secure_filename(f.filename)
        if os.path.splitext(name)[1].lower() != '.pdf':
            rows.append({'filename': f.filename, 'vendor': f.filename, 'is_invoice': False,
                         'amount': None, 'account_debit': '', 'account_credit': profile['ap_account'],
                         'date': '', 'doc': '', 'description': '', 'vatcode': '', 'currency': '',
                         'account_source': 'none', 'warnings': ['Not a PDF — skipped.']})
            continue
        tmp = os.path.join(UPLOAD_DIR, f'{uuid.uuid4()}.pdf')
        f.save(tmp)
        try:
            r = invoice_booking.process_invoice(tmp, profile, vmap)
            r['filename'] = f.filename
        except Exception as e:  # noqa: BLE001 — one bad PDF shouldn't sink the batch
            r = {'filename': f.filename, 'vendor': f.filename, 'is_invoice': False,
                 'amount': None, 'account_debit': '', 'account_credit': profile['ap_account'],
                 'date': '', 'doc': '', 'description': '', 'vatcode': '', 'currency': '',
                 'account_source': 'none', 'warnings': [f'Extraction failed: {e}']}
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        rows.append(r)

    return jsonify({
        'rows': rows,
        'client_file': client_file,
        'ap_account': profile['ap_account'],
        'expense_accounts': [{'account': a['account'], 'description': a['description']}
                             for a in profile['expense_accounts']],
        'input_vat_codes': [{'code': c['code'], 'rate': c['rate'], 'description': c.get('description', '')}
                            for c in profile['input_vat_codes']],
    })


@app.route('/invoices/export', methods=['POST'])
def invoices_export():
    """Build the Banana transactions import file from the (reviewed) rows and learn
    the confirmed vendor→account+VAT mappings for next time."""
    data = request.get_json(force=True, silent=True) or {}
    client_file = (data.get('client_file') or '').strip()
    rows = data.get('rows') or []
    if not client_file:
        return jsonify({'error': 'Missing client file.'}), 400

    # Coerce amounts that came back from the editable table as strings.
    for r in rows:
        amt = r.get('amount')
        if isinstance(amt, str):
            try:
                r['amount'] = float(amt.replace("'", '').replace('’', '').replace(',', '.').strip())
            except (ValueError, AttributeError):
                r['amount'] = None

    # Learn confirmed mappings (bookable, non-skipped rows with a vendor + account).
    slug = invoice_booking.client_slug(client_file)
    learned = 0
    for r in rows:
        if (not r.get('skip') and r.get('is_invoice', True)
                and r.get('account_debit') and (r.get('vendor') or '').strip()):
            invoice_booking.learn_vendor(slug, r['vendor'], r['account_debit'], r.get('vatcode', ''))
            learned += 1

    tsv, included, skipped = invoice_booking.export_banana_tsv(rows)
    if included == 0:
        return jsonify({'error': 'No bookable rows to export (need date, amount and an expense account).',
                        'skipped': skipped}), 400

    _evict_expired_sessions()
    session_id = str(uuid.uuid4())
    out_path = os.path.join(UPLOAD_DIR, f'{session_id}_invoices.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(tsv)
    SESSIONS[session_id] = {
        'path': out_path,
        'filename': f'kreditoren_{slug}.txt',
        'mimetype': 'text/plain',
        'created': time.time(),
    }
    return jsonify({
        'session_id': session_id,
        'included': included,
        'skipped': skipped,
        'learned': learned,
        'preview': tsv.split('\n'),
    })


# --------------------------------------------------------------------------- #
# Dividends → Banana — composed double-entry securities-income bookings
# (Debit bank net + Debit Verrechnungssteuer-Guthaben / Credit income gross)
# --------------------------------------------------------------------------- #

@app.route('/dividends/profile')
def dividends_profile():
    """Return the client's bank/custody (asset) accounts + auto-detected VST account
    so the UI can populate the bank dropdown before extraction."""
    client_file = (request.args.get('client_file') or '').strip()
    if not client_file:
        return jsonify({'error': 'No client file given.'}), 400
    try:
        profile = banana_live.get_client_profile(client_file)
    except banana_live.BananaUnavailable as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({
        'asset_accounts': [{'account': a['account'], 'description': a['description']}
                           for a in profile['asset_accounts']],
        'wht_account': profile.get('wht_account', ''),
    })


@app.route('/dividends/extract', methods=['POST'])
def dividends_extract():
    """Extract dropped dividend vouchers and propose composed bookings against the
    selected client's live chart (income + asset accounts, VST-Guthaben)."""
    client_file = (request.form.get('client_file') or '').strip()
    if not client_file:
        return jsonify({'error': 'Select a client (an open Banana file) first.'}), 400
    bank_account = (request.form.get('bank_account') or '').strip()
    vst_account = (request.form.get('vst_account') or '').strip()
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return jsonify({'error': 'No dividend voucher PDFs uploaded.'}), 400
    if not dividend_extract.available():
        return jsonify({'error': 'Dividend extraction needs ANTHROPIC_API_KEY configured on the server.'}), 400

    try:
        profile = banana_live.get_client_profile(client_file)
    except banana_live.BananaUnavailable as e:
        return jsonify({'error': str(e)}), 400

    slug = dividend_booking.client_slug(client_file)
    smap = dividend_booking.load_security_map(slug)
    eff_wht = vst_account or profile.get('wht_account', '')

    _evict_expired_sessions()
    rows = []
    for f in files:
        name = secure_filename(f.filename)
        if os.path.splitext(name)[1].lower() != '.pdf':
            rows.append({'filename': f.filename, 'security': f.filename, 'is_dividend': False,
                         'gross': None, 'net': None, 'swiss_wht': 0.0, 'foreign_wht': 0.0,
                         'income_account': '', 'bank_account': bank_account,
                         'wht_account': eff_wht, 'date': '', 'doc': '',
                         'description': '', 'currency': '', 'isin': '', 'valor': '',
                         'balances': False, 'account_source': 'none',
                         'warnings': ['Not a PDF — skipped.']})
            continue
        tmp = os.path.join(UPLOAD_DIR, f'{uuid.uuid4()}.pdf')
        f.save(tmp)
        try:
            r = dividend_booking.process_dividend(tmp, profile, smap, bank_account, vst_account)
            r['filename'] = f.filename
        except Exception as e:  # noqa: BLE001 — one bad PDF shouldn't sink the batch
            r = {'filename': f.filename, 'security': f.filename, 'is_dividend': False,
                 'gross': None, 'net': None, 'swiss_wht': 0.0, 'foreign_wht': 0.0,
                 'income_account': '', 'bank_account': bank_account,
                 'wht_account': eff_wht, 'date': '', 'doc': '',
                 'description': '', 'currency': '', 'isin': '', 'valor': '',
                 'balances': False, 'account_source': 'none',
                 'warnings': [f'Extraction failed: {e}']}
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        rows.append(r)

    return jsonify({
        'rows': rows,
        'client_file': client_file,
        'bank_account': bank_account,
        'wht_account': eff_wht,
        'income_accounts': [{'account': a['account'], 'description': a['description']}
                            for a in profile['income_accounts']],
        'asset_accounts': [{'account': a['account'], 'description': a['description']}
                           for a in profile['asset_accounts']],
    })


@app.route('/dividends/export', methods=['POST'])
def dividends_export():
    """Build the Banana transactions import file from the (reviewed) dividend rows
    and learn the confirmed security→income-account mappings for next time."""
    data = request.get_json(force=True, silent=True) or {}
    client_file = (data.get('client_file') or '').strip()
    rows = data.get('rows') or []
    if not client_file:
        return jsonify({'error': 'Missing client file.'}), 400

    # Coerce amounts that came back from the editable table as strings.
    for r in rows:
        for k in ('gross', 'net', 'swiss_wht', 'foreign_wht'):
            v = r.get(k)
            if isinstance(v, str):
                try:
                    r[k] = float(v.replace("'", '').replace('’', '').replace(',', '.').strip())
                except (ValueError, AttributeError):
                    r[k] = None if k in ('gross', 'net') else 0.0
        # Re-derive the balance flag from the (possibly edited) amounts.
        g, n = r.get('gross'), r.get('net')
        sw = r.get('swiss_wht') or 0.0
        r['balances'] = (g is not None and n is not None
                         and abs((n + sw) - g) <= dividend_booking.BALANCE_TOL)

    # Learn confirmed mappings (bookable, non-skipped rows with a security + income account).
    slug = dividend_booking.client_slug(client_file)
    learned = 0
    for r in rows:
        if (not r.get('skip') and r.get('is_dividend', True)
                and r.get('income_account') and (r.get('security') or '').strip()):
            dividend_booking.learn_security(slug, r.get('isin'), r['security'], r['income_account'])
            learned += 1

    tsv, included, skipped = dividend_booking.export_banana_tsv(rows)
    if included == 0:
        return jsonify({'error': 'No bookable vouchers to export (need date, gross/net, a bank '
                                 'account and an income account, and must reconcile).',
                        'skipped': skipped}), 400

    _evict_expired_sessions()
    session_id = str(uuid.uuid4())
    out_path = os.path.join(UPLOAD_DIR, f'{session_id}_dividends.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(tsv)
    SESSIONS[session_id] = {
        'path': out_path,
        'filename': f'dividends_{slug}.txt',
        'mimetype': 'text/plain',
        'created': time.time(),
    }
    return jsonify({
        'session_id': session_id,
        'included': included,
        'skipped': skipped,
        'learned': learned,
        'preview': tsv.split('\n'),
    })


# --------------------------------------------------------------------------- #
# Portfolio → Banana — year-end securities revaluation (to cost or market)
# --------------------------------------------------------------------------- #

def _detect_securities_account(asset_accounts):
    """Best-guess the listed-securities account (BClass-1): 'Listed shares' /
    Wertschriften / Titel / securities. Avoid 'treasury'/'own' shares."""
    for a in asset_accounts:
        d = a["description"].lower()
        if ("securities" in d or "wertschrift" in d or "titel" in d
                or ("shares" in d and "treasury" not in d and "own" not in d)
                or "aktien" in d):
            return a["account"]
    return ""


def _detect_pl_account(pl_accounts, kind):
    """Best-guess the gain ('revenue'/Ertrag/Kursgewinn) or loss ('expenses'/
    Aufwand/Kursverlust) financial-result account. kind = 'gain' | 'loss'."""
    gain_kw = ("financial revenue", "finanzertrag", "kursgewinn", "wertschriftenertrag", "wertberichtigung")
    loss_kw = ("financial expenses", "finanzaufwand", "kursverlust")
    kws = gain_kw if kind == "gain" else loss_kw
    for a in pl_accounts:
        d = a["description"].lower()
        if any(k in d for k in kws):
            return a["account"]
    return ""


@app.route('/portfolio/profile')
def portfolio_profile():
    """Securities (asset) accounts WITH live balances + P&L accounts + detected
    defaults so the UI can pick the account to revalue and show its book value."""
    client_file = (request.args.get('client_file') or '').strip()
    if not client_file:
        return jsonify({'error': 'No client file given.'}), 400
    try:
        profile = banana_live.get_client_profile(client_file)
    except banana_live.BananaUnavailable as e:
        return jsonify({'error': str(e)}), 400
    asset_accounts = [{'account': a['account'], 'description': a['description'], 'balance': a.get('balance')}
                      for a in profile['asset_accounts']]
    pl_accounts = [{'account': a['account'], 'description': a['description']}
                   for a in profile['income_accounts']]
    return jsonify({
        'asset_accounts': asset_accounts,
        'pl_accounts': pl_accounts,
        'securities_default': _detect_securities_account(profile['asset_accounts']),
        'gain_default': _detect_pl_account(pl_accounts, 'gain'),
        'loss_default': _detect_pl_account(pl_accounts, 'loss'),
    })


@app.route('/portfolio/extract', methods=['POST'])
def portfolio_extract_route():
    """Extract the equity positions (cost + market value) from a Statement of assets."""
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'error': 'No statement of assets uploaded.'}), 400
    f = request.files['file']
    if os.path.splitext(secure_filename(f.filename))[1].lower() != '.pdf':
        return jsonify({'error': 'Please upload the Statement of assets as a PDF.'}), 400
    if not portfolio_extract.available():
        return jsonify({'error': 'Portfolio extraction needs ANTHROPIC_API_KEY configured on the server.'}), 400

    _evict_expired_sessions()
    tmp = os.path.join(UPLOAD_DIR, f'{uuid.uuid4()}.pdf')
    f.save(tmp)
    try:
        payload = portfolio_extract.extract_portfolio(tmp)
    except Exception as e:  # noqa: BLE001
        return jsonify({'error': f'Extraction failed: {e}'}), 400
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    positions = portfolio_booking.normalize_positions(payload)
    return jsonify({
        'positions': positions,
        'as_of_date': (payload.get('as_of_date') or '').strip(),
        'reporting_currency': (payload.get('reporting_currency') or 'CHF').strip().upper(),
        'total_cost_value': payload.get('total_cost_value'),
        'total_market_value': payload.get('total_market_value'),
    })


@app.route('/portfolio/export', methods=['POST'])
def portfolio_export():
    """Compute the revaluation booking from the reviewed positions + chosen accounts
    and build the Banana transactions import file. Re-reads the securities account's
    live book value so the change is authoritative."""
    data = request.get_json(force=True, silent=True) or {}
    client_file = (data.get('client_file') or '').strip()
    positions = data.get('positions') or []
    basis = (data.get('basis') or 'cost').lower()
    securities_account = (data.get('securities_account') or '').strip()
    gain_account = (data.get('gain_account') or '').strip()
    loss_account = (data.get('loss_account') or '').strip()
    as_of_date = (data.get('as_of_date') or '').strip()
    description = (data.get('description') or '').strip()

    if not client_file:
        return jsonify({'error': 'Missing client file.'}), 400
    if not securities_account:
        return jsonify({'error': 'Pick the securities account to revalue.'}), 400

    # Authoritative current book value: re-read it live (don't trust the client).
    try:
        accounts = banana_live.get_accounts(client_file)
    except banana_live.BananaUnavailable as e:
        return jsonify({'error': str(e)}), 400
    current_book = next((a.get('balance') for a in accounts if a['account'] == securities_account), None)

    # Coerce position amounts that came back from the editable table as strings.
    for p in positions:
        for k in ('cost_value', 'market_value'):
            v = p.get(k)
            if isinstance(v, str):
                try:
                    p[k] = float(v.replace("'", '').replace('’', '').replace(',', '.').strip())
                except (ValueError, AttributeError):
                    p[k] = None

    result = portfolio_booking.compute_revaluation(
        positions, basis=basis, current_book=current_book,
        securities_account=securities_account, gain_account=gain_account,
        loss_account=loss_account, as_of_date=as_of_date, description=description)

    tsv, included = portfolio_booking.export_banana_tsv(result['booking'])
    if included == 0:
        return jsonify({'error': 'Nothing to book — ' + ('; '.join(result['warnings']) or 'no change vs the current book value.'),
                        'result': result}), 400

    _evict_expired_sessions()
    session_id = str(uuid.uuid4())
    out_path = os.path.join(UPLOAD_DIR, f'{session_id}_portfolio.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(tsv)
    slug = dividend_booking.client_slug(client_file)
    SESSIONS[session_id] = {
        'path': out_path,
        'filename': f'revaluation_{slug}.txt',
        'mimetype': 'text/plain',
        'created': time.time(),
    }
    return jsonify({
        'session_id': session_id,
        'result': result,
        'preview': tsv.split('\n'),
    })


@app.route('/download/<session_id>')
def download(session_id):
    _evict_expired_sessions()
    if session_id not in SESSIONS:
        return 'Session expired', 404
    s = SESSIONS[session_id]
    return send_file(s['path'], as_attachment=True,
                     download_name=s['filename'],
                     mimetype=s.get('mimetype', 'text/plain'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8500, debug=False)
