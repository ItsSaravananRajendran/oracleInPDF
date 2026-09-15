"""Chat sidebar: anchors list, message history (markdown+KaTeX via a small
WebKit view), and the composer. Talks to the LLM in a worker thread and
marshals tokens back to the GTK main loop with GLib.idle_add.
"""
from __future__ import annotations

import json
import threading
import uuid

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

from app.db import repo
from app.llm import providers
from app.llm.base import stream_chat

CHAT_HTML_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<style>
body { font: 14px/1.5 -apple-system, sans-serif; margin: 0; padding: 8px; background: #fff; color: #1a1a1a; }
.msg { margin-bottom: 14px; padding: 8px 10px; border-radius: 8px; }
.msg.user { background: #eef2ff; }
.msg.assistant { background: #f5f5f5; }
.role { font-size: 11px; color: #888; margin-bottom: 4px; text-transform: uppercase; }
.anchor { font-size: 12px; color: #666; background: #fff9db; border-left: 3px solid #f0c419; padding: 2px 6px; margin-bottom: 6px; cursor: pointer; }
pre { background: #272822; color: #eee; padding: 8px; border-radius: 6px; overflow-x: auto; }
code { font-family: ui-monospace, monospace; }
</style></head>
<body><div id="root"></div>
<script>
function render() {
  document.querySelectorAll('.md').forEach(function(el) {
    if (el.dataset.rendered) return;
    if (window.marked) { el.innerHTML = marked.parse(el.textContent); el.dataset.rendered = "1"; }
  });
}
window.appendMessage = function(html) {
  var d = document.createElement('div');
  d.innerHTML = html;
  document.getElementById('root').appendChild(d);
  render();
  window.scrollTo(0, document.body.scrollHeight);
};
window.updateLastAssistant = function(text) {
  var nodes = document.querySelectorAll('.msg.assistant .md');
  var last = nodes[nodes.length - 1];
  if (last) { last.textContent = text; last.dataset.rendered = ""; render(); window.scrollTo(0, document.body.scrollHeight); }
};
window.clearMessages = function() { document.getElementById('root').innerHTML = ""; };
</script>
</body></html>"""


def _esc(s: str) -> str:
    return json.dumps(s or "")


class ChatPanel(Gtk.Box):
    def __init__(self, reader_view):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.reader_view = reader_view
        self.session_id: str | None = None
        self.pdf_hash: str | None = None
        self._streaming_msg_id: str | None = None
        self._streaming_text = ""
        self._annot_to_message: dict[int, str] = {}  # id(annot) -> message_id

        reader_view.on_anchor_clicked = self._on_anchor_clicked

        self.set_border_width(6)
        self.set_size_request(360, -1)

        self.anchor_banner = Gtk.Label(label="No text selected", xalign=0)
        self.anchor_banner.set_line_wrap(True)
        self.pack_start(self.anchor_banner, False, False, 0)

        self.webview = WebKit2.WebView()
        self.webview.load_html(CHAT_HTML_SHELL, "file:///")
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

    # ---- wiring from the reader ---------------------------------------

    def set_book(self, pdf_hash: str) -> None:
        self.pdf_hash = pdf_hash
        existing = repo.latest_session_for_pdf(pdf_hash)
        if existing:
            self.load_session(existing["id"])
        else:
            self.new_session()

    def new_session(self) -> None:
        self.session_id = str(uuid.uuid4())
        repo.create_session(self.session_id, self.pdf_hash)
        self.webview.run_javascript("window.clearMessages && window.clearMessages();")

    def load_session(self, session_id: str) -> None:
        self.session_id = session_id
        self.webview.run_javascript("window.clearMessages && window.clearMessages();")
        for m in repo.list_messages(session_id):
            self._render_message(m["role"], m["text"], m["anchor_text"])

    def restore_anchors_for_current_book(self) -> None:
        """Recreate highlight annotations for every anchor ever asked about
        in this book (across all sessions), so they're visible on reopen,
        and remember which message each one opens on click."""
        for a in repo.list_anchors_for_pdf(self.pdf_hash):
            rects = a["anchor_rects"]
            if not rects:
                continue
            annot = self.reader_view.restore_anchor(a["anchor_page"], rects[0], a["anchor_text"] or "")
            if annot is not None:
                self._annot_to_message[id(annot)] = a["message_id"]

    def _on_anchor_clicked(self, annot) -> None:
        message_id = self._annot_to_message.get(id(annot))
        if message_id:
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

        anchor_text = self.reader_view.selected_text()
        anchor_page = None
        anchor_rects = None
        created_annot = None
        if anchor_text:
            created = self.reader_view.create_anchor_from_selection()
            if created:
                anchor_page, rect, created_annot = created
                anchor_rects = [rect]

        user_msg_id = str(uuid.uuid4())
        repo.add_message(
            user_msg_id, self.session_id, "user", question,
            anchor_page=anchor_page, anchor_rects=anchor_rects, anchor_text=anchor_text,
        )
        if created_annot is not None:
            self._annot_to_message[id(created_annot)] = user_msg_id
        self._render_message("user", question, anchor_text)

        assistant_msg_id = str(uuid.uuid4())
        repo.add_message(assistant_msg_id, self.session_id, "assistant", "", status="streaming")
        self._streaming_msg_id = assistant_msg_id
        self._streaming_text = ""
        self._render_message("assistant", "", None)
        self.status_label.set_text("Thinking…")

        page_text = self.reader_view.page_text(anchor_page) if anchor_page is not None else None
        history = [
            {"role": m["role"], "content": m["text"]}
            for m in repo.list_messages(self.session_id)
            if m["id"] not in (user_msg_id, assistant_msg_id)
        ]

        thread = threading.Thread(
            target=self._stream_worker,
            args=(provider, question, anchor_text, page_text, history, assistant_msg_id),
            daemon=True,
        )
        thread.start()

    def _stream_worker(self, provider, question, anchor_text, page_text, history, assistant_msg_id):
        full_text = []
        for event in stream_chat(provider, question, anchor_text, page_text, history):
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

    # ---- rendering --------------------------------------------------------

    def _render_message(self, role: str, text: str, anchor_text: str | None) -> None:
        anchor_html = ""
        if anchor_text:
            preview = anchor_text if len(anchor_text) <= 200 else anchor_text[:200] + "…"
            anchor_html = f'<div class="anchor">“{GLib.markup_escape_text(preview)}”</div>'
        html = f'<div class="msg {role}"><div class="role">{role}</div>{anchor_html}<div class="md">{GLib.markup_escape_text(text)}</div></div>'
        self.webview.run_javascript(f"window.appendMessage && window.appendMessage({_esc(html)});")
