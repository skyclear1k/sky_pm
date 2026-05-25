"""
Bulk-import drawings from a folder into the PLM.

Usage
-----
  python import_drawings.py                        # import all files from data/drawings_import/
  python import_drawings.py --src C:\path\to\files # import from a custom folder
  python import_drawings.py --dry-run              # preview what would be imported

Each file becomes one drawing record.  The script:
  1. Copies the file into data/drawings/ with a UUID name
  2. Inserts a row in the drawings table (number auto-assigned, title = filename stem)
  3. Skips files whose extension is not in the allowed list
  4. Skips files already imported (same orig_name already in the DB)

You can edit the metadata defaults (revision, status) at the top of this file.
"""

import argparse
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

# ── Paths (same as app.py) ────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
DATA_DIR     = BASE_DIR / "data"
DRAWINGS_DIR = DATA_DIR / "drawings"
DB_PATH      = DATA_DIR / "plm.db"
DEFAULT_SRC  = BASE_DIR / "data" / "drawings_import"

ALLOWED_EXT = {
    'pdf', 'dwg', 'dxf', 'step', 'stp', 'iges', 'igs', 'stl',
    'png', 'jpg', 'jpeg', 'gif', 'tif', 'tiff', 'bmp', 'svg',
    'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'csv', 'zip',
}

# ── Defaults for imported records — edit as needed ────────────────────────────
DEFAULT_REVISION = "A"
DEFAULT_STATUS   = "In Work"       # In Work | Released | Obsolete
DEFAULT_DESC     = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def next_number(conn: sqlite3.Connection, reserved: set) -> str:
    n = conn.execute("SELECT COUNT(*) FROM drawings").fetchone()[0] + 1
    while True:
        cand = f"DWG-{n:03d}"
        exists = conn.execute(
            "SELECT 1 FROM drawings WHERE number=?", (cand,)
        ).fetchone()
        if not exists and cand not in reserved:
            return cand
        n += 1


def already_imported(conn: sqlite3.Connection, orig_name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM drawings WHERE orig_name=?", (orig_name,)
        ).fetchone()
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bulk-import drawings into Sky Action")
    parser.add_argument("--src",     default=str(DEFAULT_SRC),
                        help="Folder containing files to import")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview imports without writing anything")
    args = parser.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"Source folder not found: {src}")
        print(f"Create it and drop your files there, then re-run.")
        sys.exit(1)

    files = sorted(
        f for f in src.iterdir()
        if f.is_file() and f.suffix.lstrip('.').lower() in ALLOWED_EXT
    )

    if not files:
        print(f"No supported files found in: {src}")
        sys.exit(0)

    DRAWINGS_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    imported = skipped = 0
    reserved: set[str] = set()   # numbers assigned in this run, not yet committed

    print(f"{'DRY RUN — ' if args.dry_run else ''}Scanning {len(files)} file(s) from: {src}\n")
    print(f"  {'Number':<12} {'Title':<40} {'File'}")
    print(f"  {'-'*12} {'-'*40} {'-'*30}")

    for f in files:
        orig_name = f.name

        if already_imported(conn, orig_name):
            print(f"  [SKIP] {orig_name!r} already in DB")
            skipped += 1
            continue

        title  = f.stem                          # filename without extension
        number = next_number(conn, reserved)
        reserved.add(number)

        stored = f"{uuid.uuid4().hex}{f.suffix.lower()}"

        print(f"  {number:<12} {title:<40} {orig_name}")

        if not args.dry_run:
            dest = DRAWINGS_DIR / stored
            shutil.copy2(f, dest)
            conn.execute(
                "INSERT INTO drawings"
                "(number,title,revision,status,description,filename,orig_name) "
                "VALUES(?,?,?,?,?,?,?)",
                (number, title, DEFAULT_REVISION, DEFAULT_STATUS,
                 DEFAULT_DESC, stored, orig_name)
            )

        imported += 1

    if not args.dry_run:
        conn.commit()

    conn.close()

    print(f"\n{'Would import' if args.dry_run else 'Imported'}: {imported}  |  Skipped: {skipped}")
    if args.dry_run:
        print("Re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
