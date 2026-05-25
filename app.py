import os
import uuid
import sqlite3
from pathlib import Path
from markupsafe import Markup
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, send_from_directory, abort, g)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DRAWINGS_DIR = DATA_DIR / "drawings"
DOCUMENTS_DIR = DATA_DIR / "documents"
DB_PATH = DATA_DIR / "plm.db"

ALLOWED_EXT = {
    'pdf', 'dwg', 'dxf', 'step', 'stp', 'iges', 'igs', 'stl',
    'png', 'jpg', 'jpeg', 'gif', 'tif', 'tiff', 'bmp', 'svg',
    'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'csv', 'zip',
}

app = Flask(__name__)
app.secret_key = 'plm-local-2024-dune-atms'
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB

for _d in [DATA_DIR, DRAWINGS_DIR, DOCUMENTS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'Active',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS drawings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    number      TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    revision    TEXT NOT NULL DEFAULT 'A',
    status      TEXT NOT NULL DEFAULT 'In Work',
    description TEXT DEFAULT '',
    filename    TEXT,
    orig_name   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    number      TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    doc_type    TEXT NOT NULL DEFAULT 'Technical Note',
    revision    TEXT NOT NULL DEFAULT 'A',
    status      TEXT NOT NULL DEFAULT 'Draft',
    description TEXT DEFAULT '',
    filename    TEXT,
    orig_name   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    description TEXT DEFAULT '',
    priority    TEXT NOT NULL DEFAULT 'Medium',
    status      TEXT NOT NULL DEFAULT 'Open',
    start_date  TEXT,
    finish_date TEXT,
    parent_id   INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    drawing_id  INTEGER REFERENCES drawings(id) ON DELETE SET NULL,
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS task_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    # Column migrations
    for tbl, col, typedef in [
        ('tasks',     'parent_id',  'INTEGER REFERENCES tasks(id) ON DELETE CASCADE'),
        ('drawings',  'project_id', 'INTEGER REFERENCES projects(id) ON DELETE SET NULL'),
        ('documents', 'project_id', 'INTEGER REFERENCES projects(id) ON DELETE SET NULL'),
        ('tasks',     'project_id', 'INTEGER REFERENCES projects(id) ON DELETE SET NULL'),
        ('tasks',     'start_date', 'TEXT'),
    ]:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
        if col not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typedef}")
    # Rename due_date -> finish_date for existing databases
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if 'due_date' in cols and 'finish_date' not in cols:
        conn.execute("ALTER TABLE tasks RENAME COLUMN due_date TO finish_date")
    elif 'finish_date' not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN finish_date TEXT")
    # Seed a default project for any pre-existing records with no project_id
    orphans = any(
        conn.execute(f"SELECT 1 FROM {t} WHERE project_id IS NULL LIMIT 1").fetchone()
        for t in ('drawings', 'documents', 'tasks')
    )
    if orphans:
        existing = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        if existing:
            default_id = existing[0]
        else:
            conn.execute("INSERT INTO projects(name) VALUES('Default Project')")
            default_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for tbl in ('drawings', 'documents', 'tasks'):
            conn.execute(f"UPDATE {tbl} SET project_id=? WHERE project_id IS NULL", (default_id,))
    conn.commit()
    conn.close()


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()


init_db()


@app.context_processor
def inject_globals():
    from datetime import date
    projects = get_db().execute("SELECT * FROM projects ORDER BY name").fetchall()
    return {'now': date.today().isoformat(), 'all_projects': projects}


# ── Template filters (badges) ─────────────────────────────────────────────────

PRIORITY_COLORS = {
    # Tiered priorities
    'Critical T1': 'bg-red-200 text-red-900 border border-red-400',
    'Critical T2': 'bg-red-100 text-red-700 border border-red-200',
    'Critical T3': 'bg-red-50 text-red-600 border border-red-100',
    'High T1':     'bg-orange-200 text-orange-900 border border-orange-400',
    'High T2':     'bg-orange-100 text-orange-700 border border-orange-200',
    'High T3':     'bg-orange-50 text-orange-600 border border-orange-100',
    'Medium T1':   'bg-yellow-200 text-yellow-900 border border-yellow-400',
    'Medium T2':   'bg-yellow-100 text-yellow-700 border border-yellow-200',
    'Medium T3':   'bg-yellow-50 text-yellow-600 border border-yellow-100',
    'Low T1':      'bg-gray-200 text-gray-700 border border-gray-400',
    'Low T2':      'bg-gray-100 text-gray-500 border border-gray-200',
    'Low T3':      'bg-gray-50 text-gray-400 border border-gray-100',
    # Legacy (no tier) — kept for backward compatibility
    'Critical': 'bg-red-100 text-red-700 border border-red-200',
    'High':     'bg-orange-100 text-orange-700 border border-orange-200',
    'Medium':   'bg-yellow-100 text-yellow-700 border border-yellow-200',
    'Low':      'bg-gray-100 text-gray-500 border border-gray-200',
}
STATUS_COLORS = {
    'In Work':   'bg-blue-100 text-blue-700',
    'Released':  'bg-green-100 text-green-700',
    'Obsolete':  'bg-gray-100 text-gray-400',
    'Draft':     'bg-gray-100 text-gray-600',
    'In Review': 'bg-amber-100 text-amber-700',
    'Open':      'bg-gray-100 text-gray-600',
    'In Progress': 'bg-blue-100 text-blue-700',
    'Done':      'bg-green-100 text-green-700',
}


@app.template_filter('priority_badge')
def priority_badge(p):
    cls = PRIORITY_COLORS.get(p, 'bg-gray-100 text-gray-500')
    return Markup(f'<span class="inline-flex items-center text-xs font-semibold px-2 py-0.5 rounded whitespace-nowrap {cls}">{p}</span>')


@app.template_filter('status_badge')
def status_badge(s):
    cls = STATUS_COLORS.get(s, 'bg-gray-100 text-gray-600')
    return Markup(f'<span class="inline-flex items-center text-xs font-medium px-2 py-0.5 rounded {cls}">{s}</span>')


# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed_file(fn: str) -> bool:
    return '.' in fn and fn.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def _safe_dir_name(name: str) -> str:
    invalid = r'\/:*?"<>|'
    return ''.join(c for c in name if c not in invalid).strip() or 'unassigned'


def _proj_name(db, pid):
    if pid:
        r = db.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
        return r[0] if r else None
    return None


def drawing_dir(project_name) -> Path:
    safe = _safe_dir_name(project_name) if project_name else 'unassigned'
    d = DRAWINGS_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def document_dir(project_name) -> Path:
    safe = _safe_dir_name(project_name) if project_name else 'unassigned'
    d = DOCUMENTS_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def move_file(src_dir: Path, dest_dir: Path, filename: str):
    src = src_dir / filename
    if src.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
        src.rename(dest_dir / filename)


def migrate_files():
    """Move files into per-project name subfolders (handles flat and old ID-based layouts)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    for row in conn.execute(
        "SELECT d.project_id, d.filename, p.name AS project_name "
        "FROM drawings d LEFT JOIN projects p ON d.project_id=p.id "
        "WHERE d.filename IS NOT NULL"
    ):
        dest_file = drawing_dir(row['project_name']) / row['filename']
        if dest_file.exists():
            continue
        flat = DRAWINGS_DIR / row['filename']
        if flat.exists():
            flat.rename(dest_file); continue
        if row['project_id']:
            old = DRAWINGS_DIR / str(row['project_id']) / row['filename']
            if old.exists():
                old.rename(dest_file)
    for row in conn.execute(
        "SELECT d.project_id, d.filename, p.name AS project_name "
        "FROM documents d LEFT JOIN projects p ON d.project_id=p.id "
        "WHERE d.filename IS NOT NULL"
    ):
        dest_file = document_dir(row['project_name']) / row['filename']
        if dest_file.exists():
            continue
        flat = DOCUMENTS_DIR / row['filename']
        if flat.exists():
            flat.rename(dest_file); continue
        if row['project_id']:
            old = DOCUMENTS_DIR / str(row['project_id']) / row['filename']
            if old.exists():
                old.rename(dest_file)
    conn.close()


def next_number(prefix: str, table: str) -> str:
    db = get_db()
    n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + 1
    while True:
        cand = f"{prefix}-{n:03d}"
        if not db.execute(f"SELECT 1 FROM {table} WHERE number=?", (cand,)).fetchone():
            return cand
        n += 1


def save_file(f, dest: Path):
    orig = secure_filename(f.filename)
    ext = orig.rsplit('.', 1)[-1].lower() if '.' in orig else ''
    stored = f"{uuid.uuid4().hex}.{ext}" if ext else uuid.uuid4().hex
    f.save(str(dest / stored))
    return stored, orig


def delete_file(dest: Path, filename):
    if filename:
        p = dest / filename
        if p.exists():
            p.unlink()


migrate_files()


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('tasks'))


# ── Projects ─────────────────────────────────────────────────────────────────

@app.route('/projects/new', methods=['GET', 'POST'])
def project_new():
    db = get_db()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Project name is required.', 'error')
        else:
            db.execute(
                "INSERT INTO projects(name,description,status) VALUES(?,?,?)",
                (name, request.form.get('description', ''),
                 request.form.get('status', 'Active'))
            )
            db.commit()
            flash(f'Project "{name}" created.', 'success')
            return redirect(url_for('tasks'))
    return render_template('project_form.html', project=None)


@app.route('/projects/<int:id>/edit', methods=['GET', 'POST'])
def project_edit(id):
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id=?", (id,)).fetchone()
    if not project:
        abort(404)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Project name is required.', 'error')
        else:
            db.execute(
                "UPDATE projects SET name=?,description=?,status=?,"
                "updated_at=datetime('now','localtime') WHERE id=?",
                (name, request.form.get('description', ''),
                 request.form.get('status', 'Active'), id)
            )
            db.commit()
            flash('Project updated.', 'success')
            return redirect(url_for('tasks'))
    return render_template('project_form.html', project=project)


@app.route('/projects/<int:id>/delete', methods=['POST'])
def project_delete(id):
    get_db().execute("DELETE FROM projects WHERE id=?", (id,))
    get_db().commit()
    flash('Project deleted.', 'success')
    return redirect(url_for('tasks'))


# ── Drawings ──────────────────────────────────────────────────────────────────

@app.route('/drawings')
def drawings():
    db = get_db()
    q       = request.args.get('q', '').strip()
    sf      = request.args.get('status', '')
    pj_name = request.args.get('project_name', '')
    sql = "SELECT * FROM drawings"
    params, where = [], []
    if pj_name:
        pj_row = db.execute("SELECT id FROM projects WHERE name=?", (pj_name,)).fetchone()
        if pj_row:
            where.append("project_id=?"); params.append(pj_row[0])
    if q:
        where.append("(number LIKE ? OR title LIKE ? OR description LIKE ?)")
        params += [f'%{q}%'] * 3
    if sf:
        where.append("status=?"); params.append(sf)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY number"
    rows = db.execute(sql, params).fetchall()
    return render_template('drawings.html', drawings=rows, q=q,
                           status_filter=sf, project_filter=pj_name)


@app.route('/drawings/new', methods=['GET', 'POST'])
def drawing_new():
    db = get_db()
    auto = next_number('DWG', 'drawings')
    if request.method == 'POST':
        number = request.form.get('number', '').strip() or auto
        title  = request.form.get('title', '').strip()
        if not title:
            flash('Title is required.', 'error')
            return render_template('drawing_form.html', drawing=None, auto=auto)
        fn = orig = None
        f = request.files.get('file')
        if f and f.filename and allowed_file(f.filename):
            pid = request.form.get('project_id') or None
            fn, orig = save_file(f, drawing_dir(_proj_name(db, int(pid) if pid else None)))
        try:
            db.execute(
                "INSERT INTO drawings(project_id,number,title,revision,status,description,filename,orig_name) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (request.form.get('project_id') or None,
                 number, title, request.form.get('revision', 'A'),
                 request.form.get('status', 'In Work'),
                 request.form.get('description', ''), fn, orig)
            )
            db.commit()
            flash(f'Drawing {number} created.', 'success')
            return redirect(url_for('drawings'))
        except sqlite3.IntegrityError:
            flash(f'Number "{number}" already exists.', 'error')
    pj_name = request.args.get('project_name', '')
    default_project = ''
    if pj_name:
        pj_row = db.execute("SELECT id FROM projects WHERE name=?", (pj_name,)).fetchone()
        if pj_row:
            default_project = str(pj_row[0])
    return render_template('drawing_form.html', drawing=None, auto=auto,
                           default_project=default_project)


@app.route('/drawings/<int:id>/edit', methods=['GET', 'POST'])
def drawing_edit(id):
    db = get_db()
    row = db.execute("SELECT * FROM drawings WHERE id=?", (id,)).fetchone()
    if not row:
        abort(404)
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        if not title:
            flash('Title is required.', 'error')
            return render_template('drawing_form.html', drawing=row, auto=None, default_project='')
        fn, orig = row['filename'], row['orig_name']
        old_pid = row['project_id']
        new_pid_str = request.form.get('project_id') or None
        new_pid = int(new_pid_str) if new_pid_str else None
        old_name = _proj_name(db, old_pid)
        new_name = _proj_name(db, new_pid)
        f = request.files.get('file')
        if f and f.filename and allowed_file(f.filename):
            delete_file(drawing_dir(old_name), fn)
            fn, orig = save_file(f, drawing_dir(new_name))
        elif fn and old_pid != new_pid:
            move_file(drawing_dir(old_name), drawing_dir(new_name), fn)
        db.execute(
            "UPDATE drawings SET project_id=?,title=?,revision=?,status=?,description=?,filename=?,orig_name=?,"
            "updated_at=datetime('now','localtime') WHERE id=?",
            (request.form.get('project_id') or None,
             title, request.form.get('revision'), request.form.get('status'),
             request.form.get('description'), fn, orig, id)
        )
        db.commit()
        flash('Drawing updated.', 'success')
        return redirect(url_for('drawings'))
    return render_template('drawing_form.html', drawing=row, auto=None, default_project='')


@app.route('/drawings/<int:id>/delete', methods=['POST'])
def drawing_delete(id):
    db = get_db()
    row = db.execute(
        "SELECT d.filename, p.name AS project_name FROM drawings d "
        "LEFT JOIN projects p ON d.project_id=p.id WHERE d.id=?", (id,)
    ).fetchone()
    if row:
        delete_file(drawing_dir(row['project_name']), row['filename'])
    db.execute("DELETE FROM drawings WHERE id=?", (id,))
    db.commit()
    flash('Drawing deleted.', 'success')
    return redirect(url_for('drawings'))


@app.route('/drawings/<int:id>/file')
def drawing_file(id):
    db = get_db()
    row = db.execute(
        "SELECT d.*, p.name AS project_name FROM drawings d "
        "LEFT JOIN projects p ON d.project_id=p.id WHERE d.id=?", (id,)
    ).fetchone()
    if not row or not row['filename']:
        abort(404)
    return send_from_directory(str(drawing_dir(row['project_name'])), row['filename'],
                               download_name=row['orig_name'])


# ── Documents ─────────────────────────────────────────────────────────────────

@app.route('/documents')
def documents():
    db = get_db()
    q       = request.args.get('q', '').strip()
    sf      = request.args.get('status', '')
    tf      = request.args.get('type', '')
    pj_name = request.args.get('project_name', '')
    sql = "SELECT * FROM documents"
    params, where = [], []
    if pj_name:
        pj_row = db.execute("SELECT id FROM projects WHERE name=?", (pj_name,)).fetchone()
        if pj_row:
            where.append("project_id=?"); params.append(pj_row[0])
    if q:
        where.append("(number LIKE ? OR title LIKE ? OR description LIKE ?)")
        params += [f'%{q}%'] * 3
    if sf:
        where.append("status=?"); params.append(sf)
    if tf:
        where.append("doc_type=?"); params.append(tf)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY number"
    rows = db.execute(sql, params).fetchall()
    return render_template('documents.html', documents=rows, q=q,
                           status_filter=sf, type_filter=tf, project_filter=pj_name)


@app.route('/documents/new', methods=['GET', 'POST'])
def document_new():
    db = get_db()
    auto = next_number('DOC', 'documents')
    if request.method == 'POST':
        number = request.form.get('number', '').strip() or auto
        title  = request.form.get('title', '').strip()
        if not title:
            flash('Title is required.', 'error')
            return render_template('document_form.html', document=None, auto=auto)
        fn = orig = None
        f = request.files.get('file')
        if f and f.filename and allowed_file(f.filename):
            pid = request.form.get('project_id') or None
            fn, orig = save_file(f, document_dir(_proj_name(db, int(pid) if pid else None)))
        try:
            db.execute(
                "INSERT INTO documents(project_id,number,title,doc_type,revision,status,description,filename,orig_name) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (request.form.get('project_id') or None,
                 number, title, request.form.get('doc_type', 'Technical Note'),
                 request.form.get('revision', 'A'), request.form.get('status', 'Draft'),
                 request.form.get('description', ''), fn, orig)
            )
            db.commit()
            flash(f'Document {number} created.', 'success')
            return redirect(url_for('documents'))
        except sqlite3.IntegrityError:
            flash(f'Number "{number}" already exists.', 'error')
    pj_name = request.args.get('project_name', '')
    default_project = ''
    if pj_name:
        pj_row = db.execute("SELECT id FROM projects WHERE name=?", (pj_name,)).fetchone()
        if pj_row:
            default_project = str(pj_row[0])
    return render_template('document_form.html', document=None, auto=auto,
                           default_project=default_project)


@app.route('/documents/<int:id>/edit', methods=['GET', 'POST'])
def document_edit(id):
    db = get_db()
    row = db.execute("SELECT * FROM documents WHERE id=?", (id,)).fetchone()
    if not row:
        abort(404)
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        if not title:
            flash('Title is required.', 'error')
            return render_template('document_form.html', document=row, auto=None, default_project='')
        fn, orig = row['filename'], row['orig_name']
        old_pid = row['project_id']
        new_pid_str = request.form.get('project_id') or None
        new_pid = int(new_pid_str) if new_pid_str else None
        old_name = _proj_name(db, old_pid)
        new_name = _proj_name(db, new_pid)
        f = request.files.get('file')
        if f and f.filename and allowed_file(f.filename):
            delete_file(document_dir(old_name), fn)
            fn, orig = save_file(f, document_dir(new_name))
        elif fn and old_pid != new_pid:
            move_file(document_dir(old_name), document_dir(new_name), fn)
        db.execute(
            "UPDATE documents SET project_id=?,title=?,doc_type=?,revision=?,status=?,description=?,"
            "filename=?,orig_name=?,updated_at=datetime('now','localtime') WHERE id=?",
            (request.form.get('project_id') or None,
             title, request.form.get('doc_type'), request.form.get('revision'),
             request.form.get('status'), request.form.get('description'), fn, orig, id)
        )
        db.commit()
        flash('Document updated.', 'success')
        return redirect(url_for('documents'))
    return render_template('document_form.html', document=row, auto=None, default_project='')


@app.route('/documents/<int:id>/delete', methods=['POST'])
def document_delete(id):
    db = get_db()
    row = db.execute(
        "SELECT d.filename, p.name AS project_name FROM documents d "
        "LEFT JOIN projects p ON d.project_id=p.id WHERE d.id=?", (id,)
    ).fetchone()
    if row:
        delete_file(document_dir(row['project_name']), row['filename'])
    db.execute("DELETE FROM documents WHERE id=?", (id,))
    db.commit()
    flash('Document deleted.', 'success')
    return redirect(url_for('documents'))


@app.route('/documents/<int:id>/file')
def document_file(id):
    db = get_db()
    row = db.execute(
        "SELECT d.*, p.name AS project_name FROM documents d "
        "LEFT JOIN projects p ON d.project_id=p.id WHERE d.id=?", (id,)
    ).fetchone()
    if not row or not row['filename']:
        abort(404)
    return send_from_directory(str(document_dir(row['project_name'])), row['filename'],
                               download_name=row['orig_name'])


# ── Tasks ─────────────────────────────────────────────────────────────────────

PRI_SORT = (
    "CASE priority "
    "WHEN 'Critical T1' THEN 1 WHEN 'Critical T2' THEN 2 WHEN 'Critical T3' THEN 3 "
    "WHEN 'Critical' THEN 2 "
    "WHEN 'High T1' THEN 4 WHEN 'High T2' THEN 5 WHEN 'High T3' THEN 6 "
    "WHEN 'High' THEN 5 "
    "WHEN 'Medium T1' THEN 7 WHEN 'Medium T2' THEN 8 WHEN 'Medium T3' THEN 9 "
    "WHEN 'Medium' THEN 8 "
    "WHEN 'Low T1' THEN 10 WHEN 'Low T2' THEN 11 WHEN 'Low T3' THEN 12 "
    "WHEN 'Low' THEN 11 "
    "ELSE 13 END"
)


@app.route('/tasks')
def tasks():
    db = get_db()
    q       = request.args.get('q', '').strip()
    pf      = request.args.get('priority', '')
    sf      = request.args.get('status', '')
    pj_name = request.args.get('project_name', '')
    sql = ("SELECT t.*, d.number dwg_no, d.title dwg_title, "
           "doc.number doc_no, doc.title doc_title "
           "FROM tasks t "
           "LEFT JOIN drawings d ON t.drawing_id=d.id "
           "LEFT JOIN documents doc ON t.document_id=doc.id "
           "WHERE t.parent_id IS NULL")
    params, where = [], []
    if pj_name:
        pj_row = db.execute("SELECT id FROM projects WHERE name=?", (pj_name,)).fetchone()
        if pj_row:
            where.append("t.project_id=?"); params.append(pj_row[0])
    if q:
        where.append("(t.title LIKE ? OR t.description LIKE ?)")
        params += [f'%{q}%'] * 2
    if pf:
        if pf in ('Critical', 'High', 'Medium', 'Low'):
            where.append("t.priority LIKE ?"); params.append(f'{pf}%')
        else:
            where.append("t.priority=?"); params.append(pf)
    if sf:
        where.append("t.status=?"); params.append(sf)
    if where:
        sql += " AND " + " AND ".join(where)
    sql += f" ORDER BY {PRI_SORT}, CASE WHEN t.finish_date IS NULL THEN 1 ELSE 0 END, t.finish_date, t.id"
    rows = db.execute(sql, params).fetchall()

    sub_rows = db.execute(
        "SELECT t.*, d.number dwg_no, doc.number doc_no "
        "FROM tasks t "
        "LEFT JOIN drawings d ON t.drawing_id=d.id "
        "LEFT JOIN documents doc ON t.document_id=doc.id "
        f"WHERE t.parent_id IS NOT NULL "
        f"ORDER BY {PRI_SORT}, CASE WHEN t.finish_date IS NULL THEN 1 ELSE 0 END, t.finish_date, t.id"
    ).fetchall()
    from collections import defaultdict
    subtask_map = defaultdict(list)
    for s in sub_rows:
        subtask_map[s['parent_id']].append(s)

    return render_template('tasks.html', tasks=rows, subtask_map=subtask_map,
                           q=q, priority_filter=pf, status_filter=sf, project_filter=pj_name)


@app.route('/tasks/new', methods=['GET', 'POST'])
def task_new():
    db = get_db()
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        if not title:
            flash('Title is required.', 'error')
        else:
            db.execute(
                "INSERT INTO tasks(project_id,title,description,priority,status,start_date,finish_date,drawing_id,document_id,parent_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (request.form.get('project_id') or None,
                 title, request.form.get('description', ''),
                 request.form.get('priority', 'Medium T2'),
                 request.form.get('status', 'Open'),
                 request.form.get('start_date') or None,
                 request.form.get('finish_date') or None,
                 request.form.get('drawing_id') or None,
                 request.form.get('document_id') or None,
                 request.form.get('parent_id') or None)
            )
            db.commit()
            flash('Task created.', 'success')
            return redirect(url_for('tasks'))
    dwgs = db.execute("SELECT id,number,title FROM drawings ORDER BY number").fetchall()
    docs = db.execute("SELECT id,number,title FROM documents ORDER BY number").fetchall()
    parent_tasks = db.execute(
        "SELECT id,title FROM tasks WHERE parent_id IS NULL ORDER BY title"
    ).fetchall()
    preselect_parent  = request.args.get('parent_id', type=int)
    pj_name = request.args.get('project_name', '')
    default_project = ''
    if pj_name:
        pj_row = db.execute("SELECT id FROM projects WHERE name=?", (pj_name,)).fetchone()
        if pj_row:
            default_project = str(pj_row[0])
    return render_template('task_form.html', task=None, drawings=dwgs, docs=docs,
                           parent_tasks=parent_tasks, preselect_parent=preselect_parent,
                           notes=[], drawing=None, document=None,
                           default_project=default_project)


@app.route('/tasks/<int:id>/edit', methods=['GET', 'POST'])
def task_edit(id):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (id,)).fetchone()
    if not task:
        abort(404)
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        if not title:
            flash('Title is required.', 'error')
        else:
            db.execute(
                "UPDATE tasks SET project_id=?,title=?,description=?,priority=?,status=?,start_date=?,finish_date=?,"
                "drawing_id=?,document_id=?,parent_id=?,updated_at=datetime('now','localtime') WHERE id=?",
                (request.form.get('project_id') or None,
                 title, request.form.get('description'),
                 request.form.get('priority'), request.form.get('status'),
                 request.form.get('start_date') or None,
                 request.form.get('finish_date') or None,
                 request.form.get('drawing_id') or None,
                 request.form.get('document_id') or None,
                 request.form.get('parent_id') or None, id)
            )
            db.commit()
            flash('Task updated.', 'success')
            return redirect(url_for('tasks'))
    dwgs = db.execute("SELECT id,number,title FROM drawings ORDER BY number").fetchall()
    docs = db.execute("SELECT id,number,title FROM documents ORDER BY number").fetchall()
    parent_tasks = db.execute(
        "SELECT id,title FROM tasks WHERE parent_id IS NULL AND id != ? ORDER BY title", (id,)
    ).fetchall()
    notes = db.execute(
        "SELECT * FROM task_notes WHERE task_id=? ORDER BY created_at DESC", (id,)
    ).fetchall()
    drawing = db.execute("SELECT * FROM drawings WHERE id=?",
                         (task['drawing_id'],)).fetchone() if task['drawing_id'] else None
    document = db.execute("SELECT * FROM documents WHERE id=?",
                          (task['document_id'],)).fetchone() if task['document_id'] else None
    return render_template('task_form.html', task=task, drawings=dwgs, docs=docs,
                           parent_tasks=parent_tasks, preselect_parent=None,
                           notes=notes, drawing=drawing, document=document,
                           default_project='')


@app.route('/tasks/<int:id>/delete', methods=['POST'])
def task_delete(id):
    get_db().execute("DELETE FROM tasks WHERE id=?", (id,))
    get_db().commit()
    flash('Task deleted.', 'success')
    return redirect(url_for('tasks'))


@app.route('/tasks/<int:id>/status', methods=['POST'])
def task_status(id):
    new = request.form.get('status', 'Open')
    db = get_db()
    db.execute("UPDATE tasks SET status=?,updated_at=datetime('now','localtime') WHERE id=?",
               (new, id))
    db.commit()
    return redirect(request.referrer or url_for('tasks'))


# ── Task notes (journal) ──────────────────────────────────────────────────────

@app.route('/tasks/<int:id>/notes/add', methods=['POST'])
def task_note_add(id):
    body = request.form.get('body', '').strip()
    if body:
        db = get_db()
        db.execute("INSERT INTO task_notes(task_id, body) VALUES(?,?)", (id, body))
        db.commit()
    return redirect(url_for('task_edit', id=id))


@app.route('/tasks/<int:id>/notes/<int:note_id>/edit', methods=['POST'])
def task_note_edit(id, note_id):
    body = request.form.get('body', '').strip()
    if body:
        db = get_db()
        db.execute("UPDATE task_notes SET body=? WHERE id=? AND task_id=?", (body, note_id, id))
        db.commit()
    return redirect(url_for('task_edit', id=id))


@app.route('/tasks/<int:id>/notes/<int:note_id>/delete', methods=['POST'])
def task_note_delete(id, note_id):
    db = get_db()
    db.execute("DELETE FROM task_notes WHERE id=? AND task_id=?", (note_id, id))
    db.commit()
    return redirect(url_for('task_edit', id=id))


if __name__ == '__main__':
    app.run(debug=True, port=5000, host='127.0.0.1')
