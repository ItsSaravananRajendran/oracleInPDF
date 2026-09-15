"""PDF identity (hash), caching, and URL download.

Unlike the webapp, local files are opened in place (by absolute path) and
just hashed for identity — no copy into the cache dir, since a native app
has no reason to duplicate the user's own files on disk. PDFs loaded from a
URL are still downloaded into the cache dir, same as the webapp.

Page-text extraction is NOT here — it uses the already-open EvinceDocument
in app/reader/view.py (EvinceDocument.DocumentText), so there is no second
PDF-parsing library to keep in sync with the renderer.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Tuple

import httpx

from app import settings

MAX_BYTES = 200 * 1024 * 1024  # 200 MB
DOWNLOAD_TIMEOUT = 60.0


def hash_file(path: Path) -> Tuple[str, int]:
    hasher = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
    return hasher.hexdigest(), total


def download_pdf(url: str) -> Tuple[str, int, str]:
    """Download a PDF, stream-hash, store in the cache dir. Returns
    (hash, bytes, cached_path). Runs synchronously — call from a thread."""
    settings.PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = settings.PDF_CACHE_DIR / f".download-{int(time.time() * 1000)}.tmp"
    hasher = hashlib.sha256()
    total = 0
    with httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            ct = r.headers.get("content-type", "").lower()
            if ct and "pdf" not in ct and "octet-stream" not in ct:
                raise ValueError(f"Server returned content-type '{ct}', expected PDF")
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        tmp_path.unlink(missing_ok=True)
                        raise ValueError(f"PDF exceeds {MAX_BYTES // (1024 * 1024)} MB limit")
                    hasher.update(chunk)
                    f.write(chunk)

    digest = hasher.hexdigest()
    final_path = settings.PDF_CACHE_DIR / f"{digest}.pdf"
    tmp_path.rename(final_path)
    return digest, total, str(final_path)
