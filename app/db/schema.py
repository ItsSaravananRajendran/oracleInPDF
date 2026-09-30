"""SQLite connection and schema.

Ported from anchored-pdf-reader's backend/db_schema.py. The `message`
table's anchor columns are adapted for text-selection anchors instead of
drag-rectangles: `anchor_rects` is a JSON list of quad rects (page-point
space, one per selected line/run) and `anchor_text` is the verbatim
selected text sent to the LLM.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS folder (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pdf (
    hash        TEXT PRIMARY KEY,
    source      TEXT NOT NULL,   -- original file path or URL
    bytes       INTEGER NOT NULL,
    page_count  INTEGER,
    title       TEXT,
    path        TEXT NOT NULL,   -- cached local path
    folder_id   INTEGER,         -- NULL = unfiled (shown in the "Unfiled" pseudo-folder)
    created_at  INTEGER NOT NULL,
    FOREIGN KEY (folder_id) REFERENCES folder(id) ON DELETE SET DEFAULT
);

CREATE TABLE IF NOT EXISTS pdf_text_cache (
    pdf_hash     TEXT NOT NULL,
    page         INTEGER NOT NULL,
    text         TEXT,
    has_text     INTEGER NOT NULL,
    extracted_at INTEGER NOT NULL,
    PRIMARY KEY (pdf_hash, page),
    FOREIGN KEY (pdf_hash) REFERENCES pdf(hash)
);

CREATE TABLE IF NOT EXISTS session (
    id          TEXT PRIMARY KEY,
    pdf_hash    TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY (pdf_hash) REFERENCES pdf(hash)
);

CREATE TABLE IF NOT EXISTS message (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL,
    text            TEXT NOT NULL,
    anchor_page     INTEGER,
    anchor_rects    TEXT,   -- JSON list of {x1,y1,x2,y2} in page-point space
    anchor_text     TEXT,   -- verbatim text under the anchor
    status          TEXT NOT NULL DEFAULT 'complete',
    error_kind      TEXT,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session(id)
);

CREATE INDEX IF NOT EXISTS idx_message_session_created
    ON message(session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_message_anchor
    ON message(session_id, anchor_page);
"""


def init_db() -> None:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.SQLITE_PATH) as connection:
        connection.executescript(SCHEMA)
        _migrate(connection)
        connection.commit()


def _migrate(connection: sqlite3.Connection) -> None:
    """Apply schema migrations that `CREATE TABLE IF NOT EXISTS` alone
    can't express. Currently:
      - pre-folder DBs: `pdf.folder_id` column is missing. SQLite can't
        add a foreign-key column to an existing table, but we don't need
        the FK at the column level — the `folder` table itself enforces
        referential integrity via `ON DELETE SET DEFAULT`, and the app
        layer's `delete_folder` already moves books back to NULL before
        dropping the row.
    Adding the column with NULL default means every existing PDF is
    auto-classified as "Unfiled" — the user can then drag them into
    folders as they wish."""
    cols = {row[1] for row in connection.execute("PRAGMA table_info(pdf)")}
    if "folder_id" not in cols:
        connection.execute("ALTER TABLE pdf ADD COLUMN folder_id INTEGER")


@contextmanager
def conn():
    connection = sqlite3.connect(settings.SQLITE_PATH, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
    finally:
        connection.close()
