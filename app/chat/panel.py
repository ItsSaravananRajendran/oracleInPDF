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
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango, WebKit2  # noqa: E402

from app.db import repo
from app.llm import providers
from app.llm.base import stream_chat

CHAT_HTML_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<style>
body { font: 14px/1.5 -apple-system, sans-serif; margin: 0; padding: 8px; background: #fff; color: #1a1a1a; }
.msg { position: relative; margin-bottom: 14px; padding: 8px 26px 8px 10px; border-radius: 8px; }
.msg.user { background: #eef2ff; }
.msg.assistant { background: #f5f5f5; }
.role { font-size: 11px; color: #888; margin-bottom: 4px; text-transform: uppercase; }
.anchor { font-size: 12px; color: #666; background: #fff9db; border-left: 3px solid #f0c419; padding: 2px 6px; margin-bottom: 6px; cursor: pointer; }
.delete { position: absolute; top: 6px; right: 6px; cursor: pointer; color: #aaa; font-size: 13px; line-height: 1; border: none; background: none; padding: 2px 4px; }
.delete:hover { color: #c0392b; }
pre { background: #272822; color: #eee; padding: 8px; border-radius: 6px; overflow-x: auto; }
code { font-family: ui-monospace, monospace; }
</style></head>
<body><div id="root"></div>
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
  }
});
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
        self._annot_to_message: dict[int, str] = {}    # id(annot) -> message_id
        self._annots_by_anchor_key: dict[tuple, list] = {}  # (page, rects_json, text) -> [annot, ...]

        reader_view.on_anchor_clicked = self._on_anchor_clicked

        self.set_border_width(6)
        self.set_size_request(360, -1)

        self.anchor_banner = Gtk.Label(label="No text selected", xalign=0)
        self.anchor_banner.set_line_wrap(True)
        self.pack_start(self.anchor_banner, False, False, 0)

        self.pack_start(self._build_anchors_list(), False, False, 0)

        content_manager = WebKit2.UserContentManager()
        content_manager.register_script_message_handler("pyHandler")
        content_manager.connect("script-message-received::pyHandler", self._on_script_message)
        self.webview = WebKit2.WebView.new_with_user_content_manager(content_manager)
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

            row.add(hbox)
            self.anchors_listbox.add(row)
        self.anchors_listbox.show_all()

    def _on_anchor_row_activated(self, _listbox, row) -> None:
        if row.anchor_rects:
            self.reader_view.go_to_anchor(row.anchor_page, row.anchor_rects[0])
        else:
            self.reader_view.go_to_page(row.anchor_page)
        if row.session_id != self.session_id:
            self.load_session(row.session_id)

    # ---- wiring from the reader ---------------------------------------

    def set_book(self, pdf_hash: str) -> None:
        self.pdf_hash = pdf_hash
        existing = repo.latest_session_for_pdf(pdf_hash)
        if existing:
            self.load_session(existing["id"])
        else:
            self.new_session()
        self.refresh_anchors_list()

    def new_session(self) -> None:
        self.session_id = str(uuid.uuid4())
        repo.create_session(self.session_id, self.pdf_hash)
        self.webview.run_javascript("window.clearMessages && window.clearMessages();")

    def load_session(self, session_id: str) -> None:
        self.session_id = session_id
        self.webview.run_javascript("window.clearMessages && window.clearMessages();")
        for m in repo.list_messages(session_id):
            self._render_message(m["id"], m["role"], m["text"], m["anchor_text"])

    def restore_anchors_for_current_book(self) -> None:
        """Recreate highlight annotations for every anchor ever asked about
        in this book (across all sessions), so they're visible on reopen,
        and remember which message each one opens on click."""
        self._annot_to_message.clear()
        self._annots_by_anchor_key.clear()
        for a in repo.list_anchors_for_pdf(self.pdf_hash):
            rects = a["anchor_rects"]
            if not rects:
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
        created_annots = []
        if anchor_text:
            batch = self.reader_view.create_anchor_from_selection()
            if batch:
                anchor_page, rect, _ = batch[0]
                anchor_rects = [rect]
                created_annots = [annot for _, _, annot in batch]
        else:
            # Follow-up with no new selection: inherit the session's last anchor.
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
        self._render_message(user_msg_id, "user", question, anchor_text)

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

    # ---- deletion --------------------------------------------------------

    def _on_script_message(self, _manager, js_result) -> None:
        try:
            payload = json.loads(js_result.to_string())
        except (AttributeError, ValueError):
            return
        if payload.get("type") == "delete" and payload.get("id"):
            self.delete_message(payload["id"])

    def delete_message(self, message_id: str) -> None:
        row = repo.get_message(message_id)
        if row is None:
            return
        if row["anchor_page"] is not None:
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

    def _render_message(self, message_id: str, role: str, text: str, anchor_text: str | None) -> None:
        anchor_html = ""
        if anchor_text:
            preview = anchor_text if len(anchor_text) <= 200 else anchor_text[:200] + "…"
            anchor_html = f'<div class="anchor">“{GLib.markup_escape_text(preview)}”</div>'
        html = (
            f'<div class="msg {role}">'
            f'<button class="delete" data-id="{message_id}" title="Delete">✕</button>'
            f'<div class="role">{role}</div>{anchor_html}'
            f'<div class="md">{GLib.markup_escape_text(text)}</div></div>'
        )
        self.webview.run_javascript(f"window.appendMessage && window.appendMessage({_esc(html)});")
