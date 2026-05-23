import os
import uuid
from flask import Flask, render_template, request, jsonify, send_file
from converter import convert_to_banana

APP_VERSION = "1.4.0"
BUILD_DATE = "2026-05-23"

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB
UPLOAD_DIR = '/tmp/banana'
os.makedirs(UPLOAD_DIR, exist_ok=True)

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

    session_id = str(uuid.uuid4())
    orig_name = os.path.splitext(f.filename)[0]
    ext = os.path.splitext(f.filename)[1].lower().lstrip('.')
    upload_path = os.path.join(UPLOAD_DIR, f'{session_id}.{ext}')
    f.save(upload_path)

    try:
        tsv, col_roles, count, warnings = convert_to_banana(upload_path)

        out_path = os.path.join(UPLOAD_DIR, f'{session_id}_banana.txt')
        with open(out_path, 'w', encoding='utf-8') as fh:
            fh.write(tsv)

        SESSIONS[session_id] = {
            'path': out_path,
            'filename': f'banana_{orig_name}.txt',
        }

        preview = tsv.strip().split('\n')[:21]

        # Human-readable column mapping summary
        mapping = {role: col for role, col in col_roles.items()}

        return jsonify({
            'session_id': session_id,
            'count': count,
            'warnings': warnings,
            'mapping': mapping,
            'preview': preview,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if os.path.exists(upload_path):
            os.remove(upload_path)


@app.route('/download/<session_id>')
def download(session_id):
    if session_id not in SESSIONS:
        return 'Session expired', 404
    s = SESSIONS[session_id]
    return send_file(s['path'], as_attachment=True,
                     download_name=s['filename'], mimetype='text/plain')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8500, debug=False)
