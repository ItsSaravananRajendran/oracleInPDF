"""CRUD helpers for each table.

Ported from anchored-pdf-reader's backend/db_repos.py. Anchor storage is
adapted for text-selection anchors: `anchor_rects` (JSON list of quad
rects) + `anchor_text` replace the single drag-rectangle + rotation.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from app.db.schema import conn


# ---- PDF -----------------------------------------------------------

def upsert_pdf(
    hash_: str,
    source: str,
    bytes_: int,
    path: str,
    page_count: Optional[int],
    title: Optional[str],
) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO pdf(hash, source, bytes, page_count, title, path, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(hash) DO UPDATE SET
                   source=excluded.source,
                   bytes=excluded.bytes,
                   page_count=COALESCE(excluded.page_count, pdf.page_count),
                   title=COALESCE(excluded.title, pdf.title),
                   path=excluded.path""",
            (hash_, source, bytes_, page_count, title, path, int(time.time())),
        )


def set_pdf_folder(pdf_hash: str, folder_id: Optional[int]) -> None:
    """Assign a PDF to a folder (None = unfiled). Passing a folder_id that
    no longer exists is treated as a no-op — the caller should validate
    first, or we could raise; in practice the only path that does this is
    the UI's folder dropdown, which is built from `list_folders()` and so
    can't pick a deleted one."""
    with conn() as c:
        c.execute("UPDATE pdf SET folder_id=? WHERE hash=?", (folder_id, pdf_hash))


def get_pdf(hash_: str):
    with conn() as c:
        return c.execute("SELECT * FROM pdf WHERE hash=?", (hash_,)).fetchone()


def set_pdf_title(hash_: str, title: str) -> int:
    with conn() as c:
        cur = c.execute("UPDATE pdf SET title=? WHERE hash=?", (title, hash_))
        return cur.rowcount


def delete_pdf(hash_: str) -> None:
    """Remove a book from the library: every session/message tied to it,
    its cached page text, and the pdf row itself. Does not touch the file
    on disk (local files are opened in place, not owned by the app)."""
    with conn() as c:
        session_ids = [r["id"] for r in c.execute(
            "SELECT id FROM session WHERE pdf_hash=?", (hash_,)
        ).fetchall()]
        for sid in session_ids:
            c.execute("DELETE FROM message WHERE session_id=?", (sid,))
        c.execute("DELETE FROM session WHERE pdf_hash=?", (hash_,))
        c.execute("DELETE FROM pdf_text_cache WHERE pdf_hash=?", (hash_,))
        c.execute("DELETE FROM pdf WHERE hash=?", (hash_,))


def get_page_text(pdf_hash: str, page: int) -> tuple:
    with conn() as c:
        row = c.execute(
            "SELECT text, has_text FROM pdf_text_cache WHERE pdf_hash=? AND page=?",
            (pdf_hash, page),
        ).fetchone()
        if row is None:
            return None, False
        return row["text"], bool(row["has_text"])


def set_page_text(pdf_hash: str, page: int, text: Optional[str], has_text: bool) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO pdf_text_cache(pdf_hash, page, text, has_text, extracted_at)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(pdf_hash, page) DO UPDATE SET
                   text=excluded.text,
                   has_text=excluded.has_text,
                   extracted_at=excluded.extracted_at""",
            (pdf_hash, page, text, int(has_text), int(time.time())),
        )


# ---- Session -------------------------------------------------------

def create_session(session_id: str, pdf_hash: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO session(id, pdf_hash, created_at) VALUES(?, ?, ?)",
            (session_id, pdf_hash, int(time.time())),
        )


def get_session(session_id: str):
    with conn() as c:
        return c.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()


def latest_session_for_pdf(pdf_hash: str):
    with conn() as c:
        return c.execute(
            "SELECT * FROM session WHERE pdf_hash=? ORDER BY created_at DESC LIMIT 1",
            (pdf_hash,),
        ).fetchone()


def delete_session(session_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM message WHERE session_id=?", (session_id,))
        c.execute("DELETE FROM session WHERE id=?", (session_id,))


# ---- Message -------------------------------------------------------

def add_message(
    msg_id: str,
    session_id: str,
    role: str,
    text: str,
    anchor_page: Optional[int] = None,
    anchor_rects: Optional[list] = None,
    anchor_text: Optional[str] = None,
    status: str = "complete",
    error_kind: Optional[str] = None,
) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO message(id, session_id, role, text, anchor_page, anchor_rects,
                                    anchor_text, status, error_kind, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg_id, session_id, role, text, anchor_page,
                json.dumps(anchor_rects) if anchor_rects else None,
                anchor_text, status, error_kind,
                int(time.time() * 1000),
            ),
        )


def update_message_text(
    msg_id: str,
    text: str,
    status: str = "complete",
    error_kind: Optional[str] = None,
) -> None:
    with conn() as c:
        c.execute(
            "UPDATE message SET text=?, status=?, error_kind=? WHERE id=?",
            (text, status, error_kind, msg_id),
        )


def get_message(message_id: str):
    with conn() as c:
        return c.execute("SELECT * FROM message WHERE id=?", (message_id,)).fetchone()


def list_messages(session_id: str):
    with conn() as c:
        return c.execute(
            "SELECT * FROM message WHERE session_id=? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()


def last_message_with_anchor(session_id: str):
    """Most recent message in the session carrying an anchor. Used so
    follow-up messages without an explicit anchor inherit the last one."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM message WHERE session_id=? AND anchor_page IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()


def delete_message(message_id: str) -> int:
    with conn() as c:
        cur = c.execute("DELETE FROM message WHERE id=?", (message_id,))
        return cur.rowcount


def delete_anchor_for_message(message_id: str) -> int:
    """Delete the entire anchor pair (user question + assistant reply),
    identified by shared session_id + (page, rects, text)."""
    with conn() as c:
        row = c.execute(
            "SELECT session_id, anchor_page, anchor_rects, anchor_text, role "
            "FROM message WHERE id=?",
            (message_id,),
        ).fetchone()
        if row is None:
            return 0
        if row["anchor_page"] is None:
            return delete_message(message_id)
        cur = c.execute(
            """DELETE FROM message
               WHERE session_id = ?
                 AND anchor_page = ?
                 AND anchor_rects = ?
                 AND anchor_text = ?
                 AND (id = ? OR role != ?)""",
            (
                row["session_id"], row["anchor_page"], row["anchor_rects"],
                row["anchor_text"], message_id, row["role"],
            ),
        )
        return cur.rowcount


# ---- Library -------------------------------------------------------

def list_pdfs(folder_id: Optional[int] = None, include_unfiled: bool = False):
    """List PDFs in the library.

    `folder_id=None, include_unfiled=False`  → all PDFs (legacy behavior).
    `folder_id=<int>`                        → just that folder's books.
    `folder_id=None, include_unfiled=True`   → only unfiled books
                                               (folder_id IS NULL), for the
                                               "Unfiled" pseudo-folder row.
    """
    where = []
    params: list = []
    if folder_id is not None and not include_unfiled:
        where.append("p.folder_id = ?")
        params.append(folder_id)
    elif folder_id is None and include_unfiled:
        where.append("p.folder_id IS NULL")
    sql = """SELECT p.hash, p.source, p.path, p.title, p.page_count, p.bytes,
                    p.created_at, p.folder_id,
                    (SELECT MAX(s.created_at) FROM session s WHERE s.pdf_hash=p.hash) AS last_session_at,
                    (SELECT COUNT(*) FROM session s WHERE s.pdf_hash=p.hash) AS session_count,
                    (SELECT COUNT(*) FROM message m JOIN session s ON m.session_id=s.id
                     WHERE s.pdf_hash=p.hash) AS message_count
             FROM pdf p"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += """ ORDER BY COALESCE(
                (SELECT MAX(s.created_at) FROM session s WHERE s.pdf_hash=p.hash),
                p.created_at
              ) DESC"""
    with conn() as c:
        return c.execute(sql, params).fetchall()


# ---- Folder --------------------------------------------------------

def create_folder(name: str) -> int:
    """Create a folder and return its new id. Empty / whitespace-only names
    are rejected by the UI before reaching here, but we still trim and
    collapse blanks defensively so a stray "   " from a dialog round-trip
    doesn't pollute the library."""
    name = (name or "").strip()
    if not name:
        raise ValueError("folder name cannot be empty")
    with conn() as c:
        cur = c.execute(
            "INSERT INTO folder(name, created_at) VALUES(?, ?)",
            (name, int(time.time())),
        )
        return cur.lastrowid


def list_folders():
    """All folders in the order they were created, with the count of PDFs
    in each. Used to populate the top-level library grid; the count next
    to each folder name comes from this."""
    with conn() as c:
        return c.execute(
            """SELECT f.id, f.name, f.created_at,
                      (SELECT COUNT(*) FROM pdf p WHERE p.folder_id=f.id) AS book_count
               FROM folder f
               ORDER BY f.created_at ASC, f.id ASC"""
        ).fetchall()


def rename_folder(folder_id: int, new_name: str) -> None:
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("folder name cannot be empty")
    with conn() as c:
        c.execute("UPDATE folder SET name=? WHERE id=?", (new_name, folder_id))


def delete_folder(folder_id: int) -> None:
    """Drop a folder and move every PDF it contained back to "Unfiled".
    Done in one transaction so a crash mid-delete can't leave books
    orphaned against a non-existent folder. The schema's `ON DELETE SET
    DEFAULT` would otherwise null `folder_id` for them, which is the same
    end state — we still prefer the explicit UPDATE so the intent is
    documented in the code, not just the schema."""
    with conn() as c:
        c.execute("UPDATE pdf SET folder_id=NULL WHERE folder_id=?", (folder_id,))
        c.execute("DELETE FROM folder WHERE id=?", (folder_id,))


def get_folder(folder_id: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM folder WHERE id=?", (folder_id,)
        ).fetchone()


def count_unfiled_pdfs() -> int:
    """Cheap single-int lookup used by the "Unfiled" pseudo-folder row in
    the library view. Separate from list_pdfs() so we don't pay the
    full-row fetch just to render a count badge."""
    with conn() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM pdf WHERE folder_id IS NULL"
        ).fetchone()["n"]


def list_anchors_for_pdf(pdf_hash):
    """All distinct text anchors for a book across all sessions, so the
    reader can redraw highlight annotations on reopen."""
    with conn() as c:
        rows = c.execute(
            """SELECT m.id AS message_id, m.session_id, m.anchor_page, m.anchor_rects,
                      m.anchor_text, m.role, m.text, m.created_at
               FROM message m
               JOIN session s ON m.session_id = s.id
               WHERE s.pdf_hash = ?
                 AND m.anchor_page IS NOT NULL
                 AND m.anchor_rects IS NOT NULL
               ORDER BY m.anchor_page ASC, m.created_at ASC""",
            (pdf_hash,),
        ).fetchall()
    return [
        {
            "message_id": r["message_id"],
            "session_id": r["session_id"],
            "role": r["role"],
            "anchor_page": r["anchor_page"],
            "anchor_rects": json.loads(r["anchor_rects"]) if r["anchor_rects"] else None,
            "anchor_text": r["anchor_text"],
            "text": r["text"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
