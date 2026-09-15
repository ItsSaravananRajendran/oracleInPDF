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

Working vertical slice: open a PDF, render it natively, select text, ask a
question, get a streamed answer, and the selection is highlighted as a
clickable anchor that persists across reopens. Not yet built: a library
picker for URL-loaded PDFs in the UI, message/anchor deletion, and app
packaging (.deb/AppImage).
