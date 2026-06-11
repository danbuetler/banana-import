import os
import re
import sys
import uuid
from xml.dom import minidom
from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
import converter
from converter import convert_to_banana, parse_to_transactions
import camt_writer
import odoo_camt_writer
import ai_extract

APP_VERSION = "1.9.7"
BUILD_DATE = "2026-06-11"

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

    output_format = request.form.get('output_format', 'camt053')
    session_id = str(uuid.uuid4())
    orig_name = os.path.splitext(secure_filename(f.filename))[0] or 'statement'
    ext = os.path.splitext(f.filename)[1].lower().lstrip('.')
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
    }

    return jsonify({
        'session_id': session_id,
        'format': 'banana_tsv',
        'count': count,
        'warnings': warnings,
        'mapping': {role: col for role, col in col_roles.items()},
        'preview': tsv.strip().split('\n')[:21],
    })


def _convert_camt(session_id, orig_name, upload_path, output_format='camt053'):
    is_odoo = output_format == 'camt053_odoo'
    ext = upload_path.rsplit('.', 1)[-1].lower()
    extra = []

    # 1) Transactions + source-derived metadata.
    #    PDFs (any issuer/layout) go through AI extraction; CSV/XLSX use the
    #    deterministic heuristic parser + header sniffer.
    if ext == 'pdf':
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
        out_filename = f'camt053_odoo_{orig_name}.xml'
        result_format = 'camt053_odoo'
    else:
        xml_str, camt_warnings = camt_writer.build_camt053(transactions, meta)
        out_filename = f'camt053_{orig_name}.xml'
        result_format = 'camt053'

    out_path = os.path.join(UPLOAD_DIR, f'{session_id}_camt.xml')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(xml_str)

    SESSIONS[session_id] = {
        'path': out_path,
        'filename': out_filename,
        'mimetype': 'application/xml',
    }

    pretty = minidom.parseString(xml_str).toprettyxml(indent='  ')
    preview = [ln for ln in pretty.split('\n') if ln.strip()][:30]

    return jsonify({
        'session_id': session_id,
        'format': result_format,
        'count': len(transactions),
        'warnings': extra + warnings + camt_warnings,
        'mapping': {role: col for role, col in col_roles.items()},
        'preview': preview,
        'iban': account_ref,
        'summary': camt_writer.summarize(transactions, meta),
    })


@app.route('/download/<session_id>')
def download(session_id):
    if session_id not in SESSIONS:
        return 'Session expired', 404
    s = SESSIONS[session_id]
    return send_file(s['path'], as_attachment=True,
                     download_name=s['filename'],
                     mimetype=s.get('mimetype', 'text/plain'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8500, debug=False)
