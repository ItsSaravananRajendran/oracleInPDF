"""Library: browse previously opened PDFs and add new ones. Shown as the
home screen when no document is loaded, and reachable any time via the
toolbar's Library button to switch books.
"""
from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from app.db import repo


def _relative_time(ts: int | None) -> str:
    if not ts:
        return "never opened"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


class LibraryView(Gtk.Box):
    def __init__(self, on_open, on_add, on_remove):
        """on_open(path, hash_), on_add() -> None (caller runs the file
        chooser), on_remove(hash_) -> None."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.on_open = on_open
        self.on_add = on_add
        self.on_remove = on_remove
        self.set_border_width(16)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="<b>Library</b>", use_markup=True, xalign=0)
        header.pack_start(title, True, True, 0)
        add_btn = Gtk.Button(label="＋ Add PDF…")
        add_btn.connect("clicked", lambda _b: self.on_add())
        header.pack_start(add_btn, False, False, 0)
        self.pack_start(header, False, False, 0)

        self.empty_label = Gtk.Label(
            label="No PDFs yet. Click “Add PDF…” to open one.", xalign=0.5
        )
        self.empty_label.set_margin_top(40)
        self.pack_start(self.empty_label, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.add(self.listbox)
        self.pack_start(scroller, True, True, 0)

        self.refresh()

    def refresh(self) -> None:
        for child in self.listbox.get_children():
            self.listbox.remove(child)

        pdfs = repo.list_pdfs()
        self.empty_label.set_visible(len(pdfs) == 0)

        for p in pdfs:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hbox.set_border_width(8)

            open_btn = Gtk.Button()
            open_btn.set_relief(Gtk.ReliefStyle.NONE)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(
                label=f"<b>{GLib.markup_escape_text(p['title'] or p['source'])}</b>",
                use_markup=True, xalign=0,
            )
            title_label.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle = (
                f"{p['page_count'] or '?'} pages · "
                f"{p['session_count']} session{'s' if p['session_count'] != 1 else ''} · "
                f"{p['message_count']} message{'s' if p['message_count'] != 1 else ''} · "
                f"last opened {_relative_time(p['last_session_at'] or p['created_at'])}"
            )
            subtitle_label = Gtk.Label(label=subtitle, xalign=0)
            subtitle_label.get_style_context().add_class("dim-label")
            inner.pack_start(title_label, False, False, 0)
            inner.pack_start(subtitle_label, False, False, 0)
            open_btn.add(inner)
            open_btn.connect("clicked", self._make_open_handler(p["path"], p["hash"]))
            hbox.pack_start(open_btn, True, True, 0)

            remove_btn = Gtk.Button(label="Remove")
            remove_btn.set_relief(Gtk.ReliefStyle.NONE)
            remove_btn.connect("clicked", self._make_remove_handler(p["hash"]))
            hbox.pack_start(remove_btn, False, False, 0)

            row.add(hbox)
            self.listbox.add(row)
        self.listbox.show_all()

    def _make_open_handler(self, path, hash_):
        return lambda _b: self.on_open(path, hash_)

    def _make_remove_handler(self, hash_):
        return lambda _b: self.on_remove(hash_)
