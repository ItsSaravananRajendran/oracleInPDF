"""Env reads. All os.getenv lives here."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("PDF_READER_DATA_DIR", str(Path.home() / ".local/share/custom-pdf-reader")))
SQLITE_PATH = DATA_DIR / "app.db"
PDF_CACHE_DIR = DATA_DIR / "pdfs"

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
GOOGLE_AI_STUDIO_API_KEY = os.environ.get("GOOGLE_AI_STUDIO_API_KEY")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY")

OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
GOOGLE_MODEL = os.environ.get("GOOGLE_MODEL", "gemini-3.6-flash")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M1")

# How much of the page's extracted text to send around a text anchor, in
# characters, when the anchor is short (see llm/context.py).
MAX_PAGE_TEXT_CHARS = int(os.environ.get("PDF_READER_MAX_PAGE_TEXT_CHARS", "12000"))


def active_provider() -> str | None:
    if OPENROUTER_API_KEY:
        return "openrouter"
    if GOOGLE_AI_STUDIO_API_KEY:
        return "google"
    if MINIMAX_API_KEY:
        return "minimax"
    return None
