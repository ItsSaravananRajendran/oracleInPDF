# Custom PDF Reader

A native Linux PDF reader built on Evince's own rendering engine
(`EvinceDocument`/`EvinceView` via GObject Introspection — Evince itself has
no plugin system in modern GNOME, so this embeds the same widget Evince uses
rather than extending Evince directly). You select text on a page, ask a
question about it, and the selection becomes a real PDF highlight annotation
("anchor") that you can click later to jump back to the conversation that
produced it.

## Why not an Evince plugin

Modern Evince (46.x and later) ships with no plugin architecture at all —
the binary isn't even linked against libpeas. The only way to extend it is
to build a separate app around `libevdocument3`/`libevview3`, which is what
this does.

## Features

- Native PDF rendering via Evince's own `EvView` widget — not a
  reimplementation.
- Drag-select text on a page and ask a question about it. The full selected
  text (not an image crop) plus the rest of the page's extracted text is
  sent to the LLM.
- The selection becomes a real highlight annotation, rendered by Evince's
  own engine. Click any highlight to jump back to the chat that produced it.
- Streaming chat via OpenRouter, Google AI Studio (Gemini), or MiniMax —
  whichever API key is configured, in that priority order.
- Library of previously opened PDFs (SQLite), with sessions and full
  message history per book.
- Markdown + KaTeX rendering in assistant replies.

## Requirements

- Linux with Evince installed (`evince`, `gir1.2-evince-3.0`,
  `libevince-dev`).
- Python 3.10+, GTK3 + PyGObject, WebKit2GTK 4.1 (used only for rendering
  markdown/KaTeX in the chat panel).
- An OpenRouter, Google AI Studio, or MiniMax API key.

```bash
sudo apt-get install -y gir1.2-evince-3.0 libevince-dev gir1.2-webkit2-4.1 \
    python3-gi python3-gi-cairo
python3 -m venv .venv --system-site-packages
.venv/bin/pip install httpx
cp .env.example .env   # fill in an API key
```

## Run

```bash
set -a && source .env && set +a
.venv/bin/python -m app.main [path/to/file.pdf]
```

## Packaging (AppImage)

```bash
./packaging/build-appimage.sh
./CustomPdfReader-x86_64.AppImage [path/to/file.pdf]
```

This is a "thin" AppImage: it bundles the app's own source and its one
non-system Python dependency (`httpx`), but runs on the *host's*
python3 + GTK3 + WebKit2GTK + Evince rather than vendoring those in — this
app embeds Evince's own rendering engine, so those libraries are
load-bearing, not incidental. Fully bundling WebKit2GTK/GTK3 inside an
AppImage is both very heavy and notoriously fragile (GL/rendering
breakage), so the machine running the AppImage still needs the packages
listed under Requirements installed, same as running from a checkout. See
`packaging/AppRun` for the exact reasoning.

## Desktop integration (launch from the dash/dock)

API keys: the app now loads a `.env` file itself (`app/settings.py`), checked
at the repo root and at `$XDG_DATA_HOME/custom-pdf-reader/.env` (default
`~/.local/share/custom-pdf-reader/.env`) — a real exported env var always
wins over either. This matters for a desktop launch specifically: clicking
an icon starts with none of a terminal's exported vars, so without this the
app would silently show "No LLM API key configured" even with a working
`.env`.

```bash
mkdir -p ~/.local/bin ~/.local/share/applications \
    ~/.local/share/icons/hicolor/256x256/apps ~/.local/share/custom-pdf-reader
cp CustomPdfReader-x86_64.AppImage ~/.local/bin/customPdfReader.AppImage
cp packaging/customPdfReader.png ~/.local/share/icons/hicolor/256x256/apps/
cp .env ~/.local/share/custom-pdf-reader/.env   # so a desktop launch finds your API key
sed "s|Exec=customPdfReader|Exec=$HOME/.local/bin/customPdfReader.AppImage|" \
    packaging/customPdfReader.desktop > ~/.local/share/applications/customPdfReader.desktop
update-desktop-database ~/.local/share/applications
```

The app should then show up in your app launcher/dash as "Custom PDF
Reader" — pin it from there to keep it in the dock.

## Layout

```
app/
  main.py           Gtk.Application entry point
  window.py          Toolbar + reader/chat split pane, file/library opening
  settings.py         Env config
  pdf_ops.py           File hashing + URL download/caching
  reader/view.py        EvView wrapper: load, selection, highlight anchors, click hit-test
  chat/panel.py          Sidebar: anchors, message history (WebKit markdown+KaTeX), composer, LLM streaming thread
  llm/base.py             Shared OpenAI-compatible SSE streaming (text-only)
  llm/providers.py         Provider selection (OpenRouter > Google > MiniMax)
  db/schema.py, db/repo.py  SQLite schema + CRUD (ported from the webapp version)
```

## Status

Open a PDF, render it natively, select text, ask a question, get a
streamed answer, and the selection is highlighted as a clickable anchor
that persists across reopens and scrolls precisely back to itself. A
library screen lists every previously opened book; an anchors list groups
each book's conversations by thread, with click-to-jump and delete on
both individual messages and whole threads. Packaged as an AppImage. Not
yet built: a UI for loading a PDF from a URL (the backend support exists
in `pdf_ops.download_pdf`, just no button for it) and a `.deb`.
