"""Chat sidebar: anchors list, message history (markdown+KaTeX via a small
WebKit view), and the composer. Talks to the LLM in a worker thread and
marshals tokens back to the GTK main loop with GLib.idle_add.
"""
from __future__ import annotations

import base64
import json
import threading
import uuid

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango, WebKit2  # noqa: E402

from app.db import repo
from app.llm import providers
from app.llm.base import stream_chat

# Marker stored in `message.anchor_text` to flag an image-region
# (Ctrl+drag) anchor vs a plain text anchor. The schema has no
# separate column for it, so we piggyback on the existing
# `anchor_text` slot with a fixed string the rest of the panel can
# detect. The DB never surfaces this string to the user in
# conversation; `_render_message` suppresses the anchor banner for
# this marker (showing a thumbnail in the user bubble instead) and
# `restore_anchors_for_current_book` / `delete_*` use it to route
# between annotation cleanup and region-overlay cleanup.
IMAGE_REGION_MARKER = "(image region)"

CHAT_HTML_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<style>
/* Default = light. `body.dark` overrides everything below for the
   AI chat panel when the app is in dark mode. We swap a class on
   <body> instead of rebuilding stylesheets so messages rendered while
   the mode was one setting stay consistent if the user flips later —
   live DOM nodes just re-style in place via the cascade. */
body { font: 14px/1.5 -apple-system, sans-serif; margin: 0; padding: 8px; background: #ffffff; color: #1a1a1a; }
body.dark { background: #1e1e1e; color: #e6e6e6; }
.msg { position: relative; margin-bottom: 14px; padding: 8px 26px 8px 10px; border-radius: 8px; }
.msg.user { background: #eef2ff; }
body.dark .msg.user { background: #2a3550; }
.msg.assistant { background: #f5f5f5; }
body.dark .msg.assistant { background: #2a2a2a; }
.role { font-size: 11px; color: #888; margin-bottom: 4px; text-transform: uppercase; }
body.dark .role { color: #aaa; }
.anchor { font-size: 12px; color: #666; background: #fff9db; border-left: 3px solid #f0c419; padding: 2px 6px; margin-bottom: 6px; cursor: pointer; }
body.dark .anchor { color: #d4d4d4; background: #3a3520; border-left-color: #c8a913; }
/* Thumbnail of a Ctrl+drag image region attached to a user message.
   Fixed max-size keeps the chat scrollable on long sessions even when
   the user repeatedly attaches images. The border color tracks the
   dark/light theme via the body.dark class — same pattern as .anchor. */
.region-thumb { margin-bottom: 6px; cursor: pointer; }
.region-thumb img { max-width: 140px; max-height: 100px; border-radius: 4px; border: 1px solid #ddd; display: block; }
body.dark .region-thumb img { border-color: #444; }
.anchor[data-id] { cursor: pointer; }
.delete { position: absolute; top: 6px; right: 6px; cursor: pointer; color: #aaa; font-size: 13px; line-height: 1; border: none; background: none; padding: 2px 4px; }
body.dark .delete { color: #888; }
.delete:hover { color: #c0392b; }
body.dark .delete:hover { color: #ff6b5a; }
pre { background: #272822; color: #eee; padding: 8px; border-radius: 6px; overflow-x: auto; }
body.dark pre { background: #181818; color: #e0e0e0; }
code { font-family: ui-monospace, monospace; }
</style></head>
<body class="LIGHT_PLACEHOLDER"><div id="root"></div>
<script>
var mdReady = false;
function initMarkdown() {
  if (mdReady || !window.marked || !window.katex) return mdReady;
  marked.use({
    extensions: [
      {
        name: "inlineMath", level: "inline",
        start: function(src) { return src.indexOf("$"); },
        tokenizer: function(src) {
          var match = /^\\$([^$\\n]+?)\\$/.exec(src);
          if (!match) return undefined;
          return { type: "inlineMath", raw: match[0], text: match[1] };
        },
        renderer: function(token) {
          try { return katex.renderToString(token.text, { throwOnError: false, displayMode: false }); }
          catch (e) { return token.text; }
        },
      },
      {
        name: "blockMath", level: "block",
        start: function(src) { return src.indexOf("$$"); },
        tokenizer: function(src) {
          var match = /^\\$\\$([^$]+?)\\$\\$(?:\\n|$)/.exec(src);
          if (!match) return undefined;
          return { type: "blockMath", raw: match[0], text: match[1] };
        },
        renderer: function(token) {
          try { return katex.renderToString(token.text, { throwOnError: false, displayMode: true }); }
          catch (e) { return "<pre>" + token.text + "</pre>"; }
        },
      },
    ],
  });
  mdReady = true;
  return true;
}
var PURIFY_OPTS = {
  ALLOWED_TAGS: ["p","br","strong","em","b","i","u","s","code","pre","a","ul","ol","li",
    "blockquote","h1","h2","h3","h4","h5","h6","hr","table","thead","tbody","tr","th","td",
    "span","div","annotation","semantics","mrow","mi","mo","mn","mfrac","msup","msub","math"],
  ALLOWED_ATTR: ["href","title","class","style","target","rel","xmlns","encoding"],
};
function render() {
  document.querySelectorAll('.md').forEach(function(el) {
    if (el.dataset.rendered) return;
    if (!initMarkdown()) return;
    var raw = marked.parse(el.textContent);
    el.innerHTML = window.DOMPurify ? DOMPurify.sanitize(raw, PURIFY_OPTS) : raw;
    el.dataset.rendered = "1";
  });
}
// Toggled by the host when the app's dark-mode setting flips. Idempotent
// — calling it with the current state is a no-op.
window.__currentDark = false;
window.setDarkMode = function(enabled) {
  window.__currentDark = !!enabled;
  document.body.classList.toggle("dark", !!enabled);
  document.body.classList.toggle("light", !enabled);
};
// Called by the host-injected UserScript at document-end on every page
// load. Replays the last dark-mode state set via setDarkMode — this is
// what makes the initial dark/light choice stick even if the host
// called set_dark_mode() before window.setDarkMode existed.
window.__applyDark = function() {
  if (window.__currentDark) document.body.classList.add("dark");
  else document.body.classList.remove("dark");
  document.body.classList.toggle("light", !window.__currentDark);
};
window.appendMessage = function(html) {
  var d = document.createElement('div');
  d.innerHTML = html;
  document.getElementById('root').appendChild(d.firstElementChild);
  render();
  window.scrollTo(0, document.body.scrollHeight);
};
window.updateLastAssistant = function(text) {
  var nodes = document.querySelectorAll('.msg.assistant .md');
  var last = nodes[nodes.length - 1];
  if (last) { last.textContent = text; last.dataset.rendered = ""; render(); window.scrollTo(0, document.body.scrollHeight); }
};
window.clearMessages = function() { document.getElementById('root').innerHTML = ""; };
document.getElementById('root').addEventListener('click', function(ev) {
  var btn = ev.target.closest('.delete');
  if (btn && window.webkit) {
    window.webkit.messageHandlers.pyHandler.postMessage(JSON.stringify({type: 'delete', id: btn.dataset.id}));
    return;
  }
  var anchor = ev.target.closest('.region-thumb[data-id], .anchor[data-id]');
  if (anchor && window.webkit) {
    window.webkit.messageHandlers.pyHandler.postMessage(JSON.stringify({type: 'goto', id: anchor.dataset.id}));
  }
});
</script>
</body></html>"""


def _esc(s: str) -> str:
    return json.dumps(s or "")


class ChatPanel(Gtk.Box):
    def __init__(self, reader_view, initial_dark_mode: bool = False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.reader_view = reader_view
        self.session_id: str | None = None
        self.pdf_hash: str | None = None
        self._streaming_msg_id: str | None = None
        self._streaming_text = ""
        # `initial_dark_mode` is captured here so the HTML can be loaded
        # with the right body class from the very first paint — no flash
        # of light-mode styles. Set this BEFORE the chat panel is
        # constructed; MainWindow passes `settings.DARK_MODE_DEFAULT`
        # at construction time, then re-applies via `set_dark_mode()`
        # after the widget tree is set up (which keeps state in sync
        # for live toggles).
        self._dark_mode = bool(initial_dark_mode)
        self._annot_to_message: dict[int, str] = {}    # id(annot) -> message_id
        self._annots_by_anchor_key: dict[tuple, list] = {}  # (page, rects_json, text) -> [annot, ...]

        # Holds the most recent Ctrl+drag region until the next send.
        # `png_bytes` may be None if the page capture failed — that
        # surfaces a status message to the user but still creates a
        # chat message so they can retry or rephrase. Cleared on send.
        self._pending_region: tuple[int, dict, bytes | None] | None = None

        reader_view.on_anchor_clicked = self._on_anchor_clicked
        reader_view.on_region_created = self._on_region_created
        reader_view.on_region_clicked = self._on_region_clicked
        # Stash the panel for the main window so it can forward the
        # dark-mode toggle to the WebKit view (see set_dark_mode below).
        # We don't read settings here directly — the caller drives us.

        self.set_border_width(6)
        self.set_size_request(360, -1)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.anchor_banner = Gtk.Label(label="No text selected", xalign=0)
        self.anchor_banner.set_line_wrap(True)
        top_row.pack_start(self.anchor_banner, True, True, 0)
        new_anchor_btn = Gtk.Button(label="+ New")
        new_anchor_btn.set_tooltip_text(
            "Start a new conversation thread — select new text on the page, "
            "then ask a question to anchor it here."
        )
        new_anchor_btn.connect("clicked", self._on_new_session_clicked)
        top_row.pack_start(new_anchor_btn, False, False, 0)
        self.pack_start(top_row, False, False, 0)

        self.pack_start(self._build_anchors_list(), False, False, 0)

        content_manager = WebKit2.UserContentManager()
        content_manager.register_script_message_handler("pyHandler")
        content_manager.connect("script-message-received::pyHandler", self._on_script_message)
        self.webview = WebKit2.WebView.new_with_user_content_manager(content_manager)
        # Re-apply dark mode after every page load — covers the race
        # where `set_dark_mode()` is called before the HTML has finished
        # loading and `window.setDarkMode` doesn't exist yet (the JS
        # call then no-ops; the load handler catches up).
        self.webview.connect("load-changed", self._on_webview_load_changed)
        # Pick the initial body class from the current `_dark_mode`
        # state. Defaults to "light" — overridden below if the caller
        # already set `_dark_mode` before constructing (e.g. from
        # `_set_dark_mode()` in MainWindow.__init__, where the
        # constructor runs and sets `_dark_mode` before `load_html`).
        initial_class = "dark" if self._dark_mode else "light"
        self.webview.load_html(
            CHAT_HTML_SHELL.replace("LIGHT_PLACEHOLDER", initial_class),
            "file:///",
        )
        scroller = Gtk.ScrolledWindow()
        scroller.add(self.webview)
        scroller.set_vexpand(True)
        self.pack_start(scroller, True, True, 0)

        composer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.entry = Gtk.Entry(placeholder_text="Ask about the selected text…")
        self.entry.connect("activate", self._on_send)
        send_btn = Gtk.Button(label="Send")
        send_btn.connect("clicked", self._on_send)
        composer.pack_start(self.entry, True, True, 0)
        composer.pack_start(send_btn, False, False, 0)
        self.pack_start(composer, False, False, 0)

        self.status_label = Gtk.Label(label="", xalign=0)
        self.pack_start(self.status_label, False, False, 0)

        provider = providers.active_provider()
        if provider is None:
            self.status_label.set_text(
                "No LLM API key configured. Set OPENROUTER_API_KEY, "
                "GOOGLE_AI_STUDIO_API_KEY, or MINIMAX_API_KEY."
            )
            self.entry.set_sensitive(False)
            send_btn.set_sensitive(False)

    # ---- anchors list (one row per conversation thread) -------------------

    def _build_anchors_list(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header = Gtk.Label(label="<b>Anchors</b>", use_markup=True, xalign=0)
        box.pack_start(header, False, False, 0)

        self.anchors_empty = Gtk.Label(
            label="No anchors yet. Select text and ask a question to create one.",
            xalign=0,
        )
        self.anchors_empty.set_line_wrap(True)
        self.anchors_empty.get_style_context().add_class("dim-label")
        box.pack_start(self.anchors_empty, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(0)
        scroller.set_max_content_height(160)
        scroller.set_propagate_natural_height(True)
        self.anchors_listbox = Gtk.ListBox()
        self.anchors_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.anchors_listbox.connect("row-activated", self._on_anchor_row_activated)
        scroller.add(self.anchors_listbox)
        box.pack_start(scroller, False, False, 0)
        return box

    def refresh_anchors_list(self) -> None:
        for child in self.anchors_listbox.get_children():
            self.anchors_listbox.remove(child)

        anchors = repo.list_anchors_for_pdf(self.pdf_hash) if self.pdf_hash else []
        threads: dict[str, dict] = {}
        order: list[str] = []
        for a in anchors:
            sid = a["session_id"]
            if sid not in threads:
                threads[sid] = {"title_msg": a, "count": 1}
                order.append(sid)
            else:
                g = threads[sid]
                g["count"] += 1
                if a["role"] == "user" and a["created_at"] < g["title_msg"]["created_at"]:
                    g["title_msg"] = a

        self.anchors_empty.set_visible(len(order) == 0)

        for sid in order:
            g = threads[sid]
            title_msg = g["title_msg"]
            row = Gtk.ListBoxRow()
            row.session_id = sid
            row.anchor_page = title_msg["anchor_page"]
            row.anchor_rects = title_msg["anchor_rects"]
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            hbox.set_border_width(4)

            page_badge = Gtk.Label(label=f"p.{title_msg['anchor_page'] + 1}")
            page_badge.get_style_context().add_class("dim-label")
            hbox.pack_start(page_badge, False, False, 0)

            text = title_msg["text"] or "(no text)"
            preview = text if len(text) <= 60 else text[:60] + "…"
            text_label = Gtk.Label(label=preview, xalign=0)
            text_label.set_ellipsize(Pango.EllipsizeMode.END)
            hbox.pack_start(text_label, True, True, 0)

            followups = g["count"] - 1
            if followups > 0:
                badge = Gtk.Label(label=f"+{followups}")
                badge.get_style_context().add_class("dim-label")
                hbox.pack_start(badge, False, False, 0)

            delete_btn = Gtk.Button(label="✕")
            delete_btn.set_relief(Gtk.ReliefStyle.NONE)
            delete_btn.set_tooltip_text("Delete this conversation thread")
            # A child Gtk.Button consumes its own button-press-event, so
            # this doesn't also trigger the row's "row-activated" handler.
            delete_btn.connect("clicked", self._on_delete_thread_clicked, sid)
            hbox.pack_start(delete_btn, False, False, 0)

            row.add(hbox)
            self.anchors_listbox.add(row)
        self.anchors_listbox.show_all()

    def _on_delete_thread_clicked(self, _btn, session_id: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(), modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Delete this conversation thread and all its messages?",
            secondary_text="This cannot be undone.",
        )
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "Delete", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.delete_thread(session_id)

    def delete_thread(self, session_id: str) -> None:
        """Delete a whole conversation thread (every message in the
        session) and every highlight annotation any of its anchors created.
        Image-region anchors take a parallel branch: their visual mark
        on the page is a faint rectangle overlay rather than a real
        Evince annotation, so we ask the reader view to remove the
        overlay instead of `remove_annotation`."""
        for m in repo.list_messages(session_id):
            if m["anchor_page"] is None:
                continue
            if m["anchor_text"] == IMAGE_REGION_MARKER:
                self.reader_view.remove_region_overlay(m["id"])
                continue
            key = self._anchor_key(
                m["anchor_page"],
                json.loads(m["anchor_rects"]) if m["anchor_rects"] else None,
                m["anchor_text"],
            )
            annots = self._annots_by_anchor_key.pop(key, [])
            for annot in annots:
                self.reader_view.remove_annotation(annot)
                self._annot_to_message.pop(id(annot), None)
        repo.delete_session(session_id)
        if session_id == self.session_id:
            self.new_session()
        self.refresh_anchors_list()

    def _on_anchor_row_activated(self, _listbox, row) -> None:
        if row.anchor_rects:
            self.reader_view.go_to_anchor(row.anchor_page, row.anchor_rects[0])
        else:
            self.reader_view.go_to_page(row.anchor_page)
        if row.session_id != self.session_id:
            self.load_session(row.session_id)

    # ---- wiring from the reader ---------------------------------------

    def set_dark_mode(self, enabled: bool) -> None:
        """Swap the WebKit view between light and dark CSS variants by
        flipping a class on <body>. We track the state ourselves so
        reapplying the same value is a no-op (WebKit's run_javascript
        round-trip is non-trivial and easy to spam).

        The HTML is loaded async, so calling this from MainWindow's __init__
        runs before `window.setDarkMode` is defined — the JS becomes a
        silent no-op. We re-apply the desired state on the WebView's
        `load-changed::FINISHED` event, which fires after every page
        load including the initial one."""
        if enabled == self._dark_mode:
            return
        self._dark_mode = enabled
        self._apply_dark_to_webview()

    def _apply_dark_to_webview(self) -> None:
        """Push the current `_dark_mode` into the WebView's CSS. Safe to
        call any time — `window.setDarkMode` may not exist yet if the
        page hasn't finished loading, in which case the call is a no-op
        and the `load-changed` listener will replay the state."""
        self.webview.run_javascript(
            f"window.setDarkMode && window.setDarkMode({str(bool(self._dark_mode)).lower()});"
        )

    def _on_webview_load_changed(self, _webview, load_event) -> None:
        """Re-apply the dark-mode CSS after every page load. The
        `load-changed` signal fires repeatedly during a load; we just
        re-run our setter unconditionally — it's idempotent."""
        self._apply_dark_to_webview()

    def set_book(self, pdf_hash: str) -> None:
        self.pdf_hash = pdf_hash
        # Switching books invalidates any captured-but-unsent region
        # from the previous book — the rect coordinates are in the
        # previous book's page space and would silently become wrong if
        # reused here.
        self._pending_region = None
        existing = repo.latest_session_for_pdf(pdf_hash)
        if existing:
            self.load_session(existing["id"])
        else:
            self.new_session()
        self.refresh_anchors_list()

    def new_session(self) -> None:
        self.session_id = str(uuid.uuid4())
        repo.create_session(self.session_id, self.pdf_hash)
        # Starting a new thread discards any captured but unsent image
        # region — otherwise the next send in the new thread would
        # attach a PNG the user already mentally moved on from.
        self._pending_region = None
        self.webview.run_javascript("window.clearMessages && window.clearMessages();")

    def _on_new_session_clicked(self, _btn) -> None:
        """Explicit 'start fresh' — otherwise a follow-up with no new
        selection silently inherits the previous anchor (see _on_send),
        so without this there's no way to deliberately move on to a new
        piece of text without also picking a new anchor immediately."""
        if self.pdf_hash is None:
            return
        self.new_session()
        self.status_label.set_text("")
        self.anchor_banner.set_text("No text selected — select text on the page to anchor a new question.")
        self.refresh_anchors_list()

    def load_session(self, session_id: str) -> None:
        """Replay a session's messages. A follow-up question asked without
        a new selection inherits the prior anchor for LLM-context/pairing
        purposes (see _on_send), but every message carrying that same
        inherited anchor would otherwise re-display the anchor quote box —
        show it only on the message that actually introduced that anchor
        (its first occurrence in the session), not on every repeat."""
        self.session_id = session_id
        self.webview.run_javascript("window.clearMessages && window.clearMessages();")
        seen_anchor_keys: set[tuple] = set()
        for m in repo.list_messages(session_id):
            anchor_text_to_show = None
            if m["role"] == "user" and m["anchor_text"]:
                key = self._anchor_key(
                    m["anchor_page"],
                    json.loads(m["anchor_rects"]) if m["anchor_rects"] else None,
                    m["anchor_text"],
                )
                if key not in seen_anchor_keys:
                    seen_anchor_keys.add(key)
                    anchor_text_to_show = m["anchor_text"]
            self._render_message(
                m["id"], m["role"], m["text"], anchor_text_to_show,
                anchor_page=m["anchor_page"],
            )

    def restore_anchors_for_current_book(self) -> None:
        """Recreate highlight annotations for every anchor ever asked about
        in this book (across all sessions), so they're visible on reopen,
        and remember which message each one opens on click.

        Image-region anchors (anchor_text == IMAGE_REGION_MARKER) take a
        different branch: Evince's annotation model is text-only, so they
        don't become highlight annotations. Instead we add a faint
        rectangle overlay on the reader's overlay drawing area, which
        serves the same click-to-jump purpose via the reader view's
        region-overlay hit-test."""
        self._annot_to_message.clear()
        self._annots_by_anchor_key.clear()
        # `load()` in the reader view already wiped the overlay list;
        # this call is idempotent but kept for clarity in case a future
        # caller invokes restore without first loading a document.
        self.reader_view.clear_region_overlays()
        for a in repo.list_anchors_for_pdf(self.pdf_hash):
            rects = a["anchor_rects"]
            if not rects:
                continue
            if a["anchor_text"] == IMAGE_REGION_MARKER:
                self.reader_view.add_region_overlay(
                    a["anchor_page"], rects[0], a["message_id"],
                )
                continue
            annot = self.reader_view.restore_anchor(a["anchor_page"], rects[0], a["anchor_text"] or "")
            if annot is not None:
                self._annot_to_message[id(annot)] = a["message_id"]
                self._annots_by_anchor_key[self._anchor_key(a["anchor_page"], rects, a["anchor_text"])] = [annot]

    @staticmethod
    def _anchor_key(page, rects, text) -> tuple:
        rects_json = json.dumps(rects) if rects else None
        return (page, rects_json, text)

    def _on_anchor_clicked(self, annot) -> None:
        message_id = self._annot_to_message.get(id(annot))
        if message_id:
            self.open_anchor_message(message_id)

    def _on_region_created(self, page_index: int, rect: dict, png_bytes: bytes | None) -> None:
        """Stash a freshly-captured Ctrl+drag region as the next-pending
        anchor. The reader view's overlay doesn't show yet (it's a
        transient rubber band there) — we wait until the user actually
        sends and a message_id exists, then ask the reader to draw a
        persistent overlay at the same rect.

        `png_bytes=None` means the page capture failed inside the reader
        view. We still surface the pending region so the user can see
        that something happened, but the status label warns them the
        image won't be attached to the LLM call."""
        self._pending_region = (page_index, dict(rect), png_bytes)
        if png_bytes is None:
            self.anchor_banner.set_text(
                f"Couldn't capture page region on page {page_index + 1} "
                "— type your question and send anyway, or try again."
            )
            self.status_label.set_text(
                "Image capture failed — the question will be sent with page text only."
            )
        else:
            self.anchor_banner.set_text(
                f"Image region selected on page {page_index + 1} — type your question."
            )
            self.status_label.set_text("")

    def _on_region_clicked(self, message_id: str) -> None:
        """Hit-test fired from the reader view when the user clicks an
        existing region overlay. Mirrors `_on_anchor_clicked` for image
        regions — open the conversation that produced the anchor."""
        self.open_anchor_message(message_id)

    def on_selection_changed(self, text: str | None) -> None:
        if text:
            preview = text if len(text) <= 140 else text[:140] + "…"
            self.anchor_banner.set_text(f"Selected: “{preview}”")
        else:
            self.anchor_banner.set_text("No text selected")

    def open_anchor_message(self, message_id: str) -> None:
        """Jump the chat panel to the conversation containing message_id."""
        with_anchor = repo.list_anchors_for_pdf(self.pdf_hash)
        target = next((a for a in with_anchor if a["message_id"] == message_id), None)
        if target and target["session_id"] != self.session_id:
            self.load_session(target["session_id"])

    # ---- sending --------------------------------------------------------

    def _on_send(self, _widget) -> None:
        question = self.entry.get_text().strip()
        if not question:
            return
        provider = providers.active_provider()
        if provider is None:
            return
        self.entry.set_text("")

        anchor_text = None
        anchor_page = None
        anchor_rects = None
        created_annots = []
        # Pending image region takes priority over the currently-selected
        # text: a Ctrl+drag is a more explicit recent action than whatever
        # the user happened to have highlighted from a previous moment.
        # After the send the region is cleared and the banner resets.
        image_data_url: str | None = None
        pending = self._pending_region
        if pending is not None:
            page_index, rect, png_bytes = pending
            anchor_page = page_index
            anchor_rects = [rect]
            anchor_text = IMAGE_REGION_MARKER
            if png_bytes is not None:
                # Data URL, ready to drop into a multimodal content part.
                # base64-encoded inline rather than uploaded separately so
                # the round-trip stays self-contained — the provider sees
                # the same image bytes the user just captured.
                image_data_url = (
                    "data:image/png;base64,"
                    + base64.b64encode(png_bytes).decode("ascii")
                )
        else:
            anchor_text = self.reader_view.selected_text()
            if anchor_text:
                batch = self.reader_view.create_anchor_from_selection()
                if batch:
                    anchor_page, rect, _ = batch[0]
                    anchor_rects = [rect]
                    created_annots = [annot for _, _, annot in batch]
            else:
                # Follow-up with no new selection: inherit the session's last anchor.
                # For an inherited image-region anchor we deliberately do
                # NOT re-attach the image — the LLM already has it from
                # the original turn and the conversation history carries
                # the context forward. Only the page text + a marker
                # tell the model which region is still in play.
                prior = repo.last_message_with_anchor(self.session_id)
                if prior is not None:
                    anchor_page = prior["anchor_page"]
                    anchor_rects = json.loads(prior["anchor_rects"]) if prior["anchor_rects"] else None
                    anchor_text = prior["anchor_text"]

        user_msg_id = str(uuid.uuid4())
        repo.add_message(
            user_msg_id, self.session_id, "user", question,
            anchor_page=anchor_page, anchor_rects=anchor_rects, anchor_text=anchor_text,
        )
        if created_annots:
            for annot in created_annots:
                self._annot_to_message[id(annot)] = user_msg_id
            self._annots_by_anchor_key[self._anchor_key(anchor_page, anchor_rects, anchor_text)] = created_annots
        # Drop the persistent region overlay now that the message exists
        # — the reader view keys the overlay on message_id, so it has to
        # happen AFTER `add_message` and BEFORE we render the bubble.
        # Order matters: if we added the overlay first then a user who
        # deleted the message in the same instant would see an orphan
        # overlay until refresh; this order keeps the two in sync.
        if pending is not None:
            self.reader_view.add_region_overlay(
                anchor_page, anchor_rects[0], user_msg_id,
            )
        # Show the anchor quote box when this message introduced a new
        # selection (text) OR a new image-region anchor (thumbnail). An
        # inherited anchor is stored for LLM-context/pairing but isn't
        # re-displayed — same rule as before, just extended to cover
        # the image case via `_render_message`'s thumbnail path.
        show_anchor_banner = bool(created_annots) or pending is not None
        self._pending_region = None
        self.anchor_banner.set_text("No text selected")
        self._render_message(
            user_msg_id, "user", question,
            anchor_text if show_anchor_banner else None,
            image_data_url=image_data_url,
        )

        assistant_msg_id = str(uuid.uuid4())
        repo.add_message(
            assistant_msg_id, self.session_id, "assistant", "",
            anchor_page=anchor_page, anchor_rects=anchor_rects, anchor_text=anchor_text,
            status="streaming",
        )
        self._streaming_msg_id = assistant_msg_id
        self._streaming_text = ""
        self._render_message(assistant_msg_id, "assistant", "", None)
        self.status_label.set_text("Thinking…")
        self.refresh_anchors_list()

        page_text = self.reader_view.page_text(anchor_page) if anchor_page is not None else None
        history = [
            {"role": m["role"], "content": m["text"]}
            for m in repo.list_messages(self.session_id)
            if m["id"] not in (user_msg_id, assistant_msg_id)
        ]

        thread = threading.Thread(
            target=self._stream_worker,
            args=(provider, question, anchor_text, page_text, history, assistant_msg_id, image_data_url),
            daemon=True,
        )
        thread.start()

    def _stream_worker(self, provider, question, anchor_text, page_text, history, assistant_msg_id, image_data_url=None):
        full_text = []
        for event in stream_chat(provider, question, anchor_text, page_text, history, image_data_url):
            if event["type"] == "token":
                full_text.append(event["text"])
                text_so_far = "".join(full_text)
                GLib.idle_add(self._on_token, assistant_msg_id, text_so_far)
            elif event["type"] == "error":
                GLib.idle_add(self._on_stream_error, assistant_msg_id, event["message"])
            elif event["type"] == "done":
                GLib.idle_add(self._on_stream_done, assistant_msg_id, "".join(full_text))

    def _on_token(self, msg_id, text_so_far) -> bool:
        if msg_id != self._streaming_msg_id:
            return False
        self._streaming_text = text_so_far
        self.webview.run_javascript(f"window.updateLastAssistant && window.updateLastAssistant({_esc(text_so_far)});")
        return False

    def _on_stream_error(self, msg_id, message) -> bool:
        repo.update_message_text(msg_id, message, status="error", error_kind="provider")
        self.status_label.set_text(f"Error: {message}")
        self.webview.run_javascript(f"window.updateLastAssistant && window.updateLastAssistant({_esc('[error] ' + message)});")
        return False

    def _on_stream_done(self, msg_id, full_text) -> bool:
        repo.update_message_text(msg_id, full_text, status="complete")
        self.status_label.set_text("")
        self._streaming_msg_id = None
        return False

    # ---- deletion --------------------------------------------------------

    def _on_script_message(self, _manager, js_result) -> None:
        try:
            payload = json.loads(js_result.to_string())
        except (AttributeError, ValueError):
            return
        if payload.get("type") == "delete" and payload.get("id"):
            self.delete_message(payload["id"])
        elif payload.get("type") == "goto" and payload.get("id"):
            self._goto_message_anchor(payload["id"])

    def _goto_message_anchor(self, message_id: str) -> None:
        """Scroll the reader to the page/rect a chat bubble's anchor
        points at — the reverse direction of `_on_region_clicked` /
        `_on_anchor_clicked` (page → chat). Fired when the user clicks
        the image thumbnail or quote banner on a message bubble."""
        row = repo.get_message(message_id)
        if row is None or row["anchor_page"] is None:
            return
        rects = json.loads(row["anchor_rects"]) if row["anchor_rects"] else None
        if rects:
            self.reader_view.go_to_anchor(row["anchor_page"], rects[0])
        else:
            self.reader_view.go_to_page(row["anchor_page"])

    def delete_message(self, message_id: str) -> None:
        row = repo.get_message(message_id)
        if row is None:
            return
        if row["anchor_page"] is not None:
            if row["anchor_text"] == IMAGE_REGION_MARKER:
                # Drop the overlay BEFORE the DB delete so a user who
                # spam-clicks the delete button never sees a flash of
                # overlay-without-message; the same ordering comment
                # applies in the highlight-annotation branch below.
                self.reader_view.remove_region_overlay(message_id)
                repo.delete_anchor_for_message(message_id)
            else:
                key = self._anchor_key(row["anchor_page"], json.loads(row["anchor_rects"]) if row["anchor_rects"] else None, row["anchor_text"])
                annots = self._annots_by_anchor_key.pop(key, [])
                for annot in annots:
                    self.reader_view.remove_annotation(annot)
                    self._annot_to_message.pop(id(annot), None)
                repo.delete_anchor_for_message(message_id)
        else:
            repo.delete_message(message_id)
        self.load_session(self.session_id)
        self.refresh_anchors_list()

    # ---- rendering --------------------------------------------------------

    def _render_message(self, message_id: str, role: str, text: str, anchor_text: str | None,
                        image_data_url: str | None = None, anchor_page: int | None = None) -> None:
        """Render one chat bubble into the WebKit view.

        `anchor_text` shows as a yellow quote-banner above the bubble
        when truthy. The IMAGE_REGION_MARKER string is filtered out
        here (rather than at the call sites) because callers don't
        otherwise have to special-case image regions — they just pass
        whatever the DB row says and this method decides what's
        user-visible.

        `image_data_url`, when provided, drops a small inline
        thumbnail above the bubble for image-region anchors. It's
        only available on the live send path; replayed history
        (load_session) never has the bytes, so old image-region
        messages fall back to a plain clickable badge (below) instead
        of a thumbnail. Either way, clicking scrolls the reader to the
        page holding the region overlay — `anchor_page` is only used
        for that badge's label."""
        anchor_html = ""
        if anchor_text and anchor_text != IMAGE_REGION_MARKER:
            preview = anchor_text if len(anchor_text) <= 200 else anchor_text[:200] + "…"
            anchor_html = f'<div class="anchor" data-id="{message_id}">“{GLib.markup_escape_text(preview)}”</div>'
        elif anchor_text == IMAGE_REGION_MARKER and anchor_page is not None:
            anchor_html = (
                f'<div class="anchor" data-id="{message_id}">'
                f'📷 Image region — page {anchor_page + 1}</div>'
            )
        image_html = ""
        if image_data_url:
            image_html = (
                f'<div class="region-thumb" data-id="{message_id}">'
                f'<img src="{image_data_url}" alt="captured region">'
                f'</div>'
            )
        html = (
            f'<div class="msg {role}">'
            f'<button class="delete" data-id="{message_id}" title="Delete">✕</button>'
            f'<div class="role">{role}</div>{image_html}{anchor_html}'
            f'<div class="md">{GLib.markup_escape_text(text)}</div></div>'
        )
        self.webview.run_javascript(f"window.appendMessage && window.appendMessage({_esc(html)});")
