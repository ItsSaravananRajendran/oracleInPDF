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


def get_pdf(hash_: str):
    with conn() as c:
        return c.execute("SELECT * FROM pdf WHERE hash=?", (hash_,)).fetchone()


def set_pdf_title(hash_: str, title: str) -> int:
    with conn() as c:
        cur = c.execute("UPDATE pdf SET title=? WHERE hash=?", (title, hash_))
        return cur.rowcount


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

def list_pdfs():
    with conn() as c:
        return c.execute(
            """SELECT p.hash, p.source, p.title, p.page_count, p.bytes, p.created_at,
                      (SELECT MAX(s.created_at) FROM session s WHERE s.pdf_hash=p.hash) AS last_session_at,
                      (SELECT COUNT(*) FROM session s WHERE s.pdf_hash=p.hash) AS session_count,
                      (SELECT COUNT(*) FROM message m JOIN session s ON m.session_id=s.id
                       WHERE s.pdf_hash=p.hash) AS message_count
               FROM pdf p
               ORDER BY COALESCE(
                 (SELECT MAX(s.created_at) FROM session s WHERE s.pdf_hash=p.hash),
                 p.created_at
               ) DESC"""
        ).fetchall()


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
