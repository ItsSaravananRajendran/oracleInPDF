"""Env reads. All os.getenv lives here."""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — no python-dotenv dependency to keep the
    AppImage's vendored deps to just httpx. Only fills in keys not already
    set in the environment, so an explicit `export`/`source` always wins.
    Launching from a desktop icon (or the dash) starts with none of the
    shell's exported vars, unlike running from a terminal where `source
    .env` was done by hand — this makes both paths behave the same way."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


DATA_DIR = Path(os.environ.get("PDF_READER_DATA_DIR") or str(Path.home() / ".local/share/custom-pdf-reader"))
SQLITE_PATH = DATA_DIR / "app.db"
PDF_CACHE_DIR = DATA_DIR / "pdfs"

# Checked in order; the first hit for a given key wins, real env vars
# always win over either file. Repo-root .env covers running from source;
# DATA_DIR/.env covers an installed AppImage with no checkout nearby.
_load_dotenv(Path(__file__).resolve().parent.parent / ".env")
_load_dotenv(DATA_DIR / ".env")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
GOOGLE_AI_STUDIO_API_KEY = os.environ.get("GOOGLE_AI_STUDIO_API_KEY")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY")

OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
GOOGLE_MODEL = os.environ.get("GOOGLE_AI_STUDIO_MODEL", "gemini-3.6-flash")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M1")

# Per-provider response token budgets — reasoning-capable models spend
# hidden "thinking" tokens out of this same budget before any visible
# content, and how much they need varies a lot by provider (see
# llm/base.py's stream_chat). MiniMax's M1 needs a much bigger one than
# the rest to reliably leave room for a visible answer after thinking.
OPENROUTER_MAX_TOKENS = int(os.environ.get("OPENROUTER_MAX_TOKENS", "8192"))
GOOGLE_MAX_TOKENS = int(os.environ.get("GOOGLE_AI_STUDIO_MAX_TOKENS", "8192"))
MINIMAX_MAX_TOKENS = int(os.environ.get("MINIMAX_MAX_TOKENS", "52000"))

# How much of the page's extracted text to send around a text anchor, in
# characters, when the anchor is short (see llm/context.py).
MAX_PAGE_TEXT_CHARS = int(os.environ.get("PDF_READER_MAX_PAGE_TEXT_CHARS", "12000"))

# UI preferences.
DARK_MODE_DEFAULT = os.environ.get("PDF_READER_DARK_MODE", "").lower() in ("1", "true", "yes", "on")
# Read once at startup; mutated at runtime by the toolbar's dark-mode toggle.
DARK_MODE = DARK_MODE_DEFAULT


def active_provider() -> str | None:
    if OPENROUTER_API_KEY:
        return "openrouter"
    if GOOGLE_AI_STUDIO_API_KEY:
        return "google"
    if MINIMAX_API_KEY:
        return "minimax"
    return None
