import json
import os
import shutil
import subprocess
import uuid
import sqlite3
import threading
from datetime import datetime
from functools import wraps
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, send_file, g
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from flasgger import Swagger
from PIL import Image
import pytesseract
from pdf2image import convert_from_path

load_dotenv()

app = Flask(__name__, static_folder='static', static_url_path='')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24).hex())
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_UPLOAD_MB', 50)) * 1024 * 1024

CORS(app, supports_credentials=True)

swagger_config = {
    "headers": [],
    "specs": [{"endpoint": "apispec", "route": "/apispec.json"}],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/apidocs",
}
swagger_template = {
    "info": {
        "title": "DocVault API",
        "description": "Document management with OCR",
        "version": "1.0.0",
    }
}
swagger = Swagger(app, config=swagger_config, template=swagger_template)

DATA_DIR = Path(os.environ.get('DATA_DIR', 'data'))
DB_DIR = DATA_DIR / 'database'
TAILSCALE_STATE_DIR = DATA_DIR / 'tailscale'
UPLOAD_DIR = Path(os.environ.get('UPLOAD_DIR', 'uploads'))
THUMB_DIR = UPLOAD_DIR / 'thumbnails'
DB_PATH = DB_DIR / 'docvault.db'

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'tiff', 'tif', 'bmp', 'webp'}
THUMB_SIZE = (300, 300)

# In-memory progress tracking for OCR processing
processing_status = {}
processing_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)
    TAILSCALE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    # Migrate DB from old location if needed
    old_db = DATA_DIR / 'docvault.db'
    if old_db.exists() and not DB_PATH.exists():
        shutil.move(str(old_db), str(DB_PATH))
        # Also move WAL/SHM files if they exist
        for suffix in ('-wal', '-shm'):
            old_extra = DATA_DIR / f'docvault.db{suffix}'
            if old_extra.exists():
                shutil.move(str(old_extra), str(DB_DIR / f'docvault.db{suffix}'))

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            ocr_text TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            upload_date TEXT NOT NULL,
            modified_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL COLLATE NOCASE
        );
        CREATE TABLE IF NOT EXISTS document_tags (
            document_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (document_id, tag_id),
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS allowed_users (
            login TEXT PRIMARY KEY,
            display_name TEXT DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
            added_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    conn.close()


def row_to_dict(row):
    return dict(row) if row else None


def get_document_tags(db, document_id):
    rows = db.execute("""
        SELECT t.id, t.name FROM tags t
        JOIN document_tags dt ON dt.tag_id = t.id
        WHERE dt.document_id = ?
        ORDER BY t.name
    """, (document_id,)).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def document_to_json(db, row):
    d = row_to_dict(row)
    if d is None:
        return None
    d['tags'] = get_document_tags(db, d['id'])
    return d


def ensure_tags(db, tag_names):
    """Ensure tags exist and return their IDs."""
    tag_ids = []
    for name in tag_names:
        name = name.strip()
        if not name:
            continue
        existing = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if existing:
            tag_ids.append(existing['id'])
        else:
            cur = db.execute("INSERT INTO tags (name) VALUES (?)", (name,))
            tag_ids.append(cur.lastrowid)
    return tag_ids


def set_document_tags(db, document_id, tag_names):
    db.execute("DELETE FROM document_tags WHERE document_id = ?", (document_id,))
    if tag_names:
        tag_ids = ensure_tags(db, tag_names)
        for tid in tag_ids:
            db.execute("INSERT OR IGNORE INTO document_tags (document_id, tag_id) VALUES (?, ?)",
                       (document_id, tid))


def cleanup_orphan_tags(db):
    db.execute("""
        DELETE FROM tags WHERE id NOT IN (
            SELECT DISTINCT tag_id FROM document_tags
        )
    """)


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def update_progress(doc_id, status, progress=0, message=''):
    with processing_lock:
        processing_status[doc_id] = {
            'status': status,
            'progress': progress,
            'message': message,
        }


def run_ocr_image(image):
    """Run OCR on a PIL Image and return extracted text."""
    try:
        return pytesseract.image_to_string(image).strip()
    except Exception:
        return ''


def run_ocr(doc_id, file_path, file_type):
    """Run OCR in a background thread. Updates processing_status as it goes."""
    try:
        update_progress(doc_id, 'processing', 0, 'Starting OCR...')
        text_parts = []

        if file_type == 'pdf':
            update_progress(doc_id, 'processing', 5, 'Converting PDF to images...')
            images = convert_from_path(str(file_path), dpi=200)
            total = len(images)
            for i, img in enumerate(images):
                pct = int(10 + (80 * i / max(total, 1)))
                update_progress(doc_id, 'processing', pct, f'Running OCR on page {i + 1} of {total}...')
                page_text = run_ocr_image(img)
                if page_text:
                    text_parts.append(page_text)
        else:
            update_progress(doc_id, 'processing', 10, 'Running OCR on image...')
            img = Image.open(file_path)
            page_text = run_ocr_image(img)
            if page_text:
                text_parts.append(page_text)

        ocr_text = '\n\n'.join(text_parts)
        update_progress(doc_id, 'processing', 90, 'Saving OCR results...')

        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        now = datetime.utcnow().isoformat()
        conn.execute("UPDATE documents SET ocr_text = ?, modified_date = ? WHERE id = ?",
                     (ocr_text, now, doc_id))
        conn.commit()
        conn.close()

        update_progress(doc_id, 'complete', 100, 'OCR complete')
    except Exception as e:
        update_progress(doc_id, 'error', 0, f'OCR failed: {str(e)}')


def generate_thumbnail(file_path, file_type, stored_filename):
    """Generate a thumbnail and save it to the thumbnails directory."""
    thumb_path = THUMB_DIR / f"{stored_filename}.png"
    try:
        if file_type == 'pdf':
            images = convert_from_path(str(file_path), dpi=72, first_page=1, last_page=1)
            if images:
                img = images[0]
            else:
                return
        else:
            img = Image.open(file_path)

        img.thumbnail(THUMB_SIZE)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        img.save(str(thumb_path), 'PNG')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_current_user():
    """Determine the current user from Tailscale headers or env-var bypass.

    Tailscale Serve proxies from 127.0.0.1 and injects identity headers.
    It also strips those headers from incoming requests to prevent spoofing.
    If AUTH_BYPASS=true is set, all requests get full admin access (dev mode).
    """
    # Environment variable bypass for development
    if os.environ.get('AUTH_BYPASS', '').lower() in ('true', '1', 'yes'):
        return {
            'login': 'dev',
            'display_name': 'Dev Admin',
            'role': 'admin',
            'auth_method': 'bypass',
        }

    remote_addr = request.environ.get('REMOTE_ADDR', '')
    ts_login = request.headers.get('Tailscale-User-Login', '')
    ts_name = request.headers.get('Tailscale-User-Name', '')

    if remote_addr == '127.0.0.1' and ts_login:
        # Request came from Tailscale Serve — check allowed_users
        db = get_db()
        row = db.execute("SELECT * FROM allowed_users WHERE login = ?", (ts_login,)).fetchone()
        if not row:
            return None  # User not allowed
        return {
            'login': row['login'],
            'display_name': ts_name or row['display_name'] or row['login'],
            'role': row['role'],
            'auth_method': 'tailscale',
        }

    # No Tailscale headers and no bypass — deny access
    return None


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if user is None:
            return jsonify({'error': 'Access denied. Your Tailscale account is not in the allowed users list.'}), 403
        g.user = user
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if user is None:
            return jsonify({'error': 'Access denied. Your Tailscale account is not in the allowed users list.'}), 403
        if user['role'] != 'admin':
            return jsonify({'error': 'Admin access required.'}), 403
        g.user = user
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    db.commit()


# ---------------------------------------------------------------------------
# Routes — Static
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


# ---------------------------------------------------------------------------
# Routes — Documents
# ---------------------------------------------------------------------------

@app.route('/api/documents', methods=['GET'])
@require_auth
def list_documents():
    """List documents with optional filtering and pagination
    ---
    parameters:
      - name: page
        in: query
        type: integer
        default: 1
      - name: per_page
        in: query
        type: integer
        default: 20
      - name: tag
        in: query
        type: string
        description: Filter by tag name
      - name: q
        in: query
        type: string
        description: Search query
      - name: sort
        in: query
        type: string
        enum: [upload_date, title, modified_date]
        default: upload_date
      - name: order
        in: query
        type: string
        enum: [asc, desc]
        default: desc
    responses:
      200:
        description: Paginated list of documents
    """
    db = get_db()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(100, max(1, request.args.get('per_page', 20, type=int)))
    tag = request.args.get('tag', '').strip()
    q = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'upload_date')
    order = request.args.get('order', 'desc')

    if sort not in ('upload_date', 'title', 'modified_date'):
        sort = 'upload_date'
    if order not in ('asc', 'desc'):
        order = 'desc'

    conditions = []
    params = []

    if q:
        conditions.append("(d.title LIKE ? OR d.ocr_text LIKE ? OR d.notes LIKE ?)")
        like = f'%{q}%'
        params.extend([like, like, like])

    if tag:
        conditions.append("""d.id IN (
            SELECT dt.document_id FROM document_tags dt
            JOIN tags t ON t.id = dt.tag_id WHERE t.name = ?
        )""")
        params.append(tag)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ''

    total = db.execute(f"SELECT COUNT(*) FROM documents d {where}", params).fetchone()[0]
    offset = (page - 1) * per_page
    rows = db.execute(
        f"SELECT d.* FROM documents d {where} ORDER BY d.{sort} {order} LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()

    documents = [document_to_json(db, r) for r in rows]

    return jsonify({
        'documents': documents,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, -(-total // per_page)),
    })


@app.route('/api/documents', methods=['POST'])
@require_auth
def upload_document():
    """Upload a new document and start OCR processing
    ---
    consumes:
      - multipart/form-data
    parameters:
      - name: file
        in: formData
        type: file
        required: true
        description: The document file (PDF, PNG, JPG, etc.)
      - name: title
        in: formData
        type: string
        required: false
        description: Document title (defaults to filename)
      - name: tags
        in: formData
        type: string
        required: false
        description: Comma-separated list of tags
      - name: notes
        in: formData
        type: string
        required: false
        description: User notes about the document
    responses:
      201:
        description: Document created, OCR processing started
      400:
        description: Invalid request
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '' or not file.filename:
        return jsonify({'error': 'No file selected'}), 400

    if not allowed_file(file.filename):
        return jsonify({'error': f'File type not allowed. Allowed: {", ".join(sorted(ALLOWED_EXTENSIONS))}'}), 400

    original_filename = secure_filename(file.filename)
    ext = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else 'bin'
    stored_filename = f"{uuid.uuid4().hex}.{ext}"
    file_path = UPLOAD_DIR / stored_filename

    file.save(str(file_path))
    file_size = file_path.stat().st_size
    file_type = ext if ext != 'jpeg' else 'jpg'

    title = request.form.get('title', '').strip() or original_filename.rsplit('.', 1)[0]
    tags_raw = request.form.get('tags', '').strip()
    tag_names = [t.strip() for t in tags_raw.split(',') if t.strip()] if tags_raw else []
    notes = request.form.get('notes', '').strip()

    now = datetime.utcnow().isoformat()
    db = get_db()
    cur = db.execute(
        """INSERT INTO documents (title, original_filename, stored_filename, file_type, file_size, notes, upload_date, modified_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, original_filename, stored_filename, file_type, file_size, notes, now, now)
    )
    doc_id = cur.lastrowid

    if tag_names:
        set_document_tags(db, doc_id, tag_names)

    db.commit()

    # Generate thumbnail
    generate_thumbnail(file_path, file_type, stored_filename)

    # Start OCR in background
    update_progress(doc_id, 'pending', 0, 'Queued for OCR')
    thread = threading.Thread(target=run_ocr, args=(doc_id, file_path, file_type), daemon=True)
    thread.start()

    doc = document_to_json(db, db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())
    doc['processing_status'] = 'pending'

    return jsonify(doc), 201


@app.route('/api/documents/<int:doc_id>', methods=['GET'])
@require_auth
def get_document(doc_id):
    """Get a single document by ID
    ---
    parameters:
      - name: doc_id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: Document details
      404:
        description: Document not found
    """
    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Document not found'}), 404
    return jsonify(document_to_json(db, row))


@app.route('/api/documents/<int:doc_id>', methods=['PUT'])
@require_auth
def update_document(doc_id):
    """Update document metadata
    ---
    parameters:
      - name: doc_id
        in: path
        type: integer
        required: true
      - name: body
        in: body
        schema:
          type: object
          properties:
            title:
              type: string
            tags:
              type: array
              items:
                type: string
            notes:
              type: string
    responses:
      200:
        description: Updated document
      404:
        description: Document not found
    """
    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Document not found'}), 404

    data = request.get_json(force=True)
    now = datetime.utcnow().isoformat()

    title = data.get('title', row['title']).strip()
    notes = data.get('notes', row['notes']).strip()

    db.execute("UPDATE documents SET title = ?, notes = ?, modified_date = ? WHERE id = ?",
               (title, notes, now, doc_id))

    if 'tags' in data:
        tag_names = data['tags'] if isinstance(data['tags'], list) else [t.strip() for t in data['tags'].split(',')]
        set_document_tags(db, doc_id, tag_names)
        cleanup_orphan_tags(db)

    db.commit()

    updated = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return jsonify(document_to_json(db, updated))


@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])
@require_auth
def delete_document(doc_id):
    """Delete a document and its file
    ---
    parameters:
      - name: doc_id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: Document deleted
      404:
        description: Document not found
    """
    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Document not found'}), 404

    # Delete file
    file_path = UPLOAD_DIR / row['stored_filename']
    if file_path.exists():
        file_path.unlink()

    # Delete thumbnail
    thumb_path = THUMB_DIR / f"{row['stored_filename']}.png"
    if thumb_path.exists():
        thumb_path.unlink()

    db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    cleanup_orphan_tags(db)
    db.commit()

    # Clean up progress
    with processing_lock:
        processing_status.pop(doc_id, None)

    return jsonify({'message': 'Document deleted'})


@app.route('/api/documents/<int:doc_id>/file', methods=['GET'])
@require_auth
def download_file(doc_id):
    """Download the original document file
    ---
    parameters:
      - name: doc_id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: The document file
      404:
        description: Document not found
    """
    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Document not found'}), 404

    file_path = UPLOAD_DIR / row['stored_filename']
    if not file_path.exists():
        return jsonify({'error': 'File not found on disk'}), 404

    return send_file(str(file_path), download_name=row['original_filename'])


@app.route('/api/documents/<int:doc_id>/thumbnail', methods=['GET'])
@require_auth
def get_thumbnail(doc_id):
    """Get the document thumbnail image
    ---
    parameters:
      - name: doc_id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: Thumbnail PNG image
      404:
        description: Thumbnail not found
    """
    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Document not found'}), 404

    thumb_path = THUMB_DIR / f"{row['stored_filename']}.png"
    if not thumb_path.exists():
        # Return a placeholder
        img = Image.new('RGB', (300, 300), color=(220, 220, 220))
        buf = BytesIO()
        img.save(buf, 'PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')

    return send_file(str(thumb_path), mimetype='image/png')


@app.route('/api/documents/<int:doc_id>/status', methods=['GET'])
@require_auth
def get_processing_status(doc_id):
    """Get the OCR processing status for a document
    ---
    parameters:
      - name: doc_id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: Processing status
        schema:
          type: object
          properties:
            status:
              type: string
              enum: [pending, processing, complete, error]
            progress:
              type: integer
              minimum: 0
              maximum: 100
            message:
              type: string
    """
    with processing_lock:
        info = processing_status.get(doc_id)

    if info is None:
        # Not in progress tracking — check if document exists
        db = get_db()
        row = db.execute("SELECT id, ocr_text FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Document not found'}), 404
        return jsonify({'status': 'complete', 'progress': 100, 'message': 'OCR complete'})

    return jsonify(info)


@app.route('/api/documents/<int:doc_id>/reocr', methods=['POST'])
@require_auth
def reocr_document(doc_id):
    """Re-run OCR on an existing document
    ---
    parameters:
      - name: doc_id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: OCR re-processing started
      404:
        description: Document not found
    """
    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Document not found'}), 404

    file_path = UPLOAD_DIR / row['stored_filename']
    if not file_path.exists():
        return jsonify({'error': 'File not found on disk'}), 404

    update_progress(doc_id, 'pending', 0, 'Queued for re-OCR')
    thread = threading.Thread(target=run_ocr, args=(doc_id, file_path, row['file_type']), daemon=True)
    thread.start()

    return jsonify({'message': 'OCR re-processing started', 'status': 'pending'})


# ---------------------------------------------------------------------------
# Routes — Tags
# ---------------------------------------------------------------------------

@app.route('/api/tags', methods=['GET'])
@require_auth
def list_tags():
    """List all tags with document counts
    ---
    responses:
      200:
        description: List of tags
    """
    db = get_db()
    rows = db.execute("""
        SELECT t.id, t.name, COUNT(dt.document_id) as doc_count
        FROM tags t
        LEFT JOIN document_tags dt ON dt.tag_id = t.id
        GROUP BY t.id
        ORDER BY t.name
    """).fetchall()
    return jsonify([{"id": r["id"], "name": r["name"], "doc_count": r["doc_count"]} for r in rows])


@app.route('/api/tags/<int:tag_id>', methods=['DELETE'])
@require_auth
def delete_tag(tag_id):
    """Delete a tag (removes from all documents)
    ---
    parameters:
      - name: tag_id
        in: path
        type: integer
        required: true
    responses:
      200:
        description: Tag deleted
      404:
        description: Tag not found
    """
    db = get_db()
    row = db.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if not row:
        return jsonify({'error': 'Tag not found'}), 404

    db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    db.commit()
    return jsonify({'message': 'Tag deleted'})


# ---------------------------------------------------------------------------
# Routes — Search
# ---------------------------------------------------------------------------

@app.route('/api/search', methods=['GET'])
@require_auth
def search_documents():
    """Search documents by text and/or tags
    ---
    parameters:
      - name: q
        in: query
        type: string
        description: Search query (matches title, OCR text, notes)
      - name: tags
        in: query
        type: string
        description: Comma-separated tag names to filter by
      - name: page
        in: query
        type: integer
        default: 1
      - name: per_page
        in: query
        type: integer
        default: 20
    responses:
      200:
        description: Search results
    """
    db = get_db()
    q = request.args.get('q', '').strip()
    tags_raw = request.args.get('tags', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(100, max(1, request.args.get('per_page', 20, type=int)))

    conditions = []
    params = []

    if q:
        conditions.append("(d.title LIKE ? OR d.ocr_text LIKE ? OR d.notes LIKE ?)")
        like = f'%{q}%'
        params.extend([like, like, like])

    if tags_raw:
        tag_list = [t.strip() for t in tags_raw.split(',') if t.strip()]
        if tag_list:
            placeholders = ','.join('?' * len(tag_list))
            conditions.append(f"""d.id IN (
                SELECT dt.document_id FROM document_tags dt
                JOIN tags t ON t.id = dt.tag_id
                WHERE t.name IN ({placeholders})
                GROUP BY dt.document_id
                HAVING COUNT(DISTINCT t.id) = ?
            )""")
            params.extend(tag_list)
            params.append(len(tag_list))

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ''
    total = db.execute(f"SELECT COUNT(*) FROM documents d {where}", params).fetchone()[0]
    offset = (page - 1) * per_page

    rows = db.execute(
        f"SELECT d.* FROM documents d {where} ORDER BY d.upload_date DESC LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()

    documents = [document_to_json(db, r) for r in rows]

    return jsonify({
        'documents': documents,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, -(-total // per_page)),
        'query': q,
        'tags': tags_raw,
    })


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    """Get current user info
    ---
    responses:
      200:
        description: Current user info
      403:
        description: Access denied
    """
    user = get_current_user()
    if user is None:
        return jsonify({'error': 'Access denied. Your Tailscale account is not in the allowed users list.'}), 403
    return jsonify(user)


# ---------------------------------------------------------------------------
# Routes — Admin: User Management
# ---------------------------------------------------------------------------

@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_list_users():
    """List all allowed users
    ---
    responses:
      200:
        description: List of allowed users
    """
    db = get_db()
    rows = db.execute("SELECT * FROM allowed_users ORDER BY login").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/admin/users', methods=['POST'])
@require_admin
def admin_add_user():
    """Add an allowed user
    ---
    parameters:
      - name: body
        in: body
        schema:
          type: object
          required: [login]
          properties:
            login:
              type: string
            display_name:
              type: string
            role:
              type: string
              enum: [admin, user]
    responses:
      201:
        description: User added
      400:
        description: Invalid request
      409:
        description: User already exists
    """
    data = request.get_json(force=True)
    login = data.get('login', '').strip().lower()
    if not login:
        return jsonify({'error': 'Login (email) is required'}), 400

    role = data.get('role', 'user').strip().lower()
    if role not in ('admin', 'user'):
        return jsonify({'error': 'Role must be admin or user'}), 400

    display_name = data.get('display_name', '').strip()
    now = datetime.utcnow().isoformat()

    db = get_db()
    existing = db.execute("SELECT login FROM allowed_users WHERE login = ?", (login,)).fetchone()
    if existing:
        return jsonify({'error': 'User already exists'}), 409

    db.execute("INSERT INTO allowed_users (login, display_name, role, added_date) VALUES (?, ?, ?, ?)",
               (login, display_name, role, now))
    db.commit()

    return jsonify({'login': login, 'display_name': display_name, 'role': role, 'added_date': now}), 201


@app.route('/api/admin/users/<path:login>', methods=['PUT'])
@require_admin
def admin_update_user(login):
    """Update an allowed user's role
    ---
    parameters:
      - name: login
        in: path
        type: string
        required: true
      - name: body
        in: body
        schema:
          type: object
          properties:
            role:
              type: string
              enum: [admin, user]
            display_name:
              type: string
    responses:
      200:
        description: User updated
      404:
        description: User not found
    """
    db = get_db()
    row = db.execute("SELECT * FROM allowed_users WHERE login = ?", (login,)).fetchone()
    if not row:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json(force=True)
    role = data.get('role', row['role']).strip().lower()
    if role not in ('admin', 'user'):
        return jsonify({'error': 'Role must be admin or user'}), 400

    display_name = data.get('display_name', row['display_name']).strip()

    db.execute("UPDATE allowed_users SET role = ?, display_name = ? WHERE login = ?",
               (role, display_name, login))
    db.commit()

    updated = db.execute("SELECT * FROM allowed_users WHERE login = ?", (login,)).fetchone()
    return jsonify(dict(updated))


@app.route('/api/admin/users/<path:login>', methods=['DELETE'])
@require_admin
def admin_delete_user(login):
    """Remove an allowed user
    ---
    parameters:
      - name: login
        in: path
        type: string
        required: true
    responses:
      200:
        description: User removed
      404:
        description: User not found
    """
    db = get_db()
    row = db.execute("SELECT * FROM allowed_users WHERE login = ?", (login,)).fetchone()
    if not row:
        return jsonify({'error': 'User not found'}), 404

    db.execute("DELETE FROM allowed_users WHERE login = ?", (login,))
    db.commit()
    return jsonify({'message': f'User {login} removed'})


# ---------------------------------------------------------------------------
# Routes — Admin: Tailscale Management
# ---------------------------------------------------------------------------

def _tailscale_status():
    """Get Tailscale status from CLI."""
    try:
        result = subprocess.run(
            ['tailscale', 'status', '--json'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return None


def _tailscale_serve_status():
    """Get Tailscale serve status from CLI."""
    try:
        result = subprocess.run(
            ['tailscale', 'serve', 'status', '--json'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return None


@app.route('/api/admin/tailscale/status', methods=['GET'])
@require_admin
def admin_tailscale_status():
    """Get Tailscale connection status
    ---
    responses:
      200:
        description: Tailscale status info
    """
    status = _tailscale_status()
    serve_status = _tailscale_serve_status()

    if status is None:
        return jsonify({
            'installed': False,
            'backend_state': 'NoState',
            'hostname': get_setting('tailscale_hostname', ''),
            'fqdn': '',
            'tailscale_ip': '',
            'serve_active': False,
        })

    backend_state = status.get('BackendState', 'NoState')
    self_status = status.get('Self', {})
    dns_name = self_status.get('DNSName', '').rstrip('.')
    tailscale_ips = self_status.get('TailscaleIPs', [])

    return jsonify({
        'installed': True,
        'backend_state': backend_state,
        'hostname': get_setting('tailscale_hostname', ''),
        'fqdn': dns_name,
        'tailscale_ip': tailscale_ips[0] if tailscale_ips else '',
        'serve_active': serve_status is not None and bool(serve_status),
    })


@app.route('/api/admin/tailscale/enable', methods=['POST'])
@require_admin
def admin_tailscale_enable():
    """Enable Tailscale with given hostname
    ---
    parameters:
      - name: body
        in: body
        schema:
          type: object
          required: [hostname]
          properties:
            hostname:
              type: string
    responses:
      200:
        description: Tailscale enable initiated, may include login URL
    """
    data = request.get_json(force=True)
    hostname = data.get('hostname', '').strip()
    if not hostname:
        return jsonify({'error': 'Hostname is required'}), 400

    # Save hostname setting
    set_setting('tailscale_hostname', hostname)
    set_setting('tailscale_enabled', 'true')

    # Ensure tailscaled is running
    try:
        subprocess.run(['pgrep', '-x', 'tailscaled'], capture_output=True, check=True)
    except subprocess.CalledProcessError:
        # Start tailscaled
        subprocess.Popen(
            ['tailscaled', '--state=/app/data/tailscale/tailscaled.state',
             '--socket=/var/run/tailscale/tailscaled.sock'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        import time
        time.sleep(2)  # Give tailscaled time to start

    # Run tailscale up
    result = subprocess.run(
        ['tailscale', 'up', f'--hostname={hostname}'],
        capture_output=True, text=True, timeout=30
    )

    # Check if we need to authenticate
    combined = result.stdout + result.stderr
    login_url = ''
    for line in combined.split('\n'):
        line = line.strip()
        if line.startswith('https://'):
            login_url = line
            break

    if login_url:
        return jsonify({'status': 'needs_auth', 'login_url': login_url})

    # Already authenticated — start serve
    _start_tailscale_serve()

    return jsonify({'status': 'running'})


def _start_tailscale_serve():
    """Start tailscale serve to proxy HTTPS to Flask."""
    # Reset any existing serve config
    subprocess.run(['tailscale', 'serve', 'reset'], capture_output=True, timeout=10)
    # Start serve in background mode
    subprocess.run(
        ['tailscale', 'serve', '--bg', '5000'],
        capture_output=True, text=True, timeout=15
    )


@app.route('/api/admin/tailscale/start-serve', methods=['POST'])
@require_admin
def admin_tailscale_start_serve():
    """Start Tailscale Serve after authentication is complete
    ---
    responses:
      200:
        description: Serve started
    """
    status = _tailscale_status()
    if not status or status.get('BackendState') != 'Running':
        return jsonify({'error': 'Tailscale is not connected yet'}), 400

    _start_tailscale_serve()
    return jsonify({'status': 'serve_started'})


@app.route('/api/admin/tailscale/disable', methods=['POST'])
@require_admin
def admin_tailscale_disable():
    """Disable Tailscale
    ---
    responses:
      200:
        description: Tailscale disabled
    """
    set_setting('tailscale_enabled', 'false')

    # Stop serve
    subprocess.run(['tailscale', 'serve', 'reset'], capture_output=True, timeout=10)
    # Disconnect
    subprocess.run(['tailscale', 'down'], capture_output=True, timeout=10)

    return jsonify({'message': 'Tailscale disabled'})


# ---------------------------------------------------------------------------
# Routes — Admin: System Info
# ---------------------------------------------------------------------------

@app.route('/api/admin/system', methods=['GET'])
@require_admin
def admin_system_info():
    """Get system information
    ---
    responses:
      200:
        description: System info
    """
    db = get_db()
    doc_count = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    tag_count = db.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    user_count = db.execute("SELECT COUNT(*) FROM allowed_users").fetchone()[0]

    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    # Calculate upload directory size
    upload_size = sum(f.stat().st_size for f in UPLOAD_DIR.rglob('*') if f.is_file())

    ts_status = _tailscale_status()

    return jsonify({
        'document_count': doc_count,
        'tag_count': tag_count,
        'user_count': user_count,
        'db_size': db_size,
        'upload_size': upload_size,
        'tailscale_connected': ts_status is not None and ts_status.get('BackendState') == 'Running',
    })


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)
