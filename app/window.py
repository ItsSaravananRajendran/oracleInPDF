"""Main window: toolbar + reader/chat split pane."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("EvinceView", "3.0")
from gi.repository import EvinceView, Gtk  # noqa: E402

from app import pdf_ops, settings
from app.chat.panel import ChatPanel
from app.db import repo
from app.reader.view import ReaderView

ZOOM_LEVELS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Custom PDF Reader")
        self.set_default_size(1200, 850)

        self.reader_view = ReaderView()
        self.chat_panel = ChatPanel(self.reader_view)
        self.reader_view.on_selection_changed = self.chat_panel.on_selection_changed

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        root.pack_start(self._build_toolbar(), False, False, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.pack1(self.reader_view, True, False)
        paned.pack2(self.chat_panel, False, False)
        paned.set_position(820)
        root.pack_start(paned, True, True, 0)

        self.current_hash: str | None = None
        self.show_all()

    def _build_toolbar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_border_width(6)

        open_btn = Gtk.Button(label="Open PDF…")
        open_btn.connect("clicked", self._on_open_clicked)
        bar.pack_start(open_btn, False, False, 0)

        library_btn = Gtk.Button(label="Library")
        library_btn.connect("clicked", self._on_library_clicked)
        bar.pack_start(library_btn, False, False, 0)

        prev_btn = Gtk.Button(label="◀")
        prev_btn.connect("clicked", lambda _b: self.reader_view.view.previous_page())
        next_btn = Gtk.Button(label="▶")
        next_btn.connect("clicked", lambda _b: self.reader_view.view.next_page())
        bar.pack_start(prev_btn, False, False, 0)
        bar.pack_start(next_btn, False, False, 0)

        zoom_out = Gtk.Button(label="－")
        zoom_out.connect("clicked", lambda _b: self.reader_view.view.zoom_out())
        zoom_in = Gtk.Button(label="＋")
        zoom_in.connect("clicked", lambda _b: self.reader_view.view.zoom_in())
        bar.pack_start(zoom_out, False, False, 0)
        bar.pack_start(zoom_in, False, False, 0)

        self.title_label = Gtk.Label(label="No document loaded")
        bar.pack_start(self.title_label, True, True, 0)
        return bar

    # ---- opening --------------------------------------------------------

    def _on_open_clicked(self, _btn) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Open PDF", parent=self, action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        pdf_filter = Gtk.FileFilter()
        pdf_filter.set_name("PDF files")
        pdf_filter.add_mime_type("application/pdf")
        dialog.add_filter(pdf_filter)
        if dialog.run() == Gtk.ResponseType.OK:
            self.open_path(dialog.get_filename())
        dialog.destroy()

    def _on_library_clicked(self, _btn) -> None:
        pdfs = repo.list_pdfs()
        dialog = Gtk.Dialog(title="Library", parent=self, flags=0)
        dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        box = dialog.get_content_area()
        listbox = Gtk.ListBox()
        for p in pdfs:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=f"{p['title'] or p['source']}  ({p['page_count'] or '?'} pages)", xalign=0)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            row.add(label)
            row.pdf_hash = p["hash"]
            row.pdf_path = p["path"]
            listbox.add(row)
        listbox.connect("row-activated", self._on_library_row_activated, dialog)
        box.pack_start(listbox, True, True, 0)
        dialog.set_default_size(500, 400)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _on_library_row_activated(self, _listbox, row, dialog) -> None:
        dialog.response(Gtk.ResponseType.CLOSE)
        self.open_path(row.pdf_path, known_hash=row.pdf_hash)

    def open_path(self, path: str, known_hash: str | None = None) -> None:
        from pathlib import Path

        hash_, size = pdf_ops.hash_file(Path(path))
        self.reader_view.load(path)
        title = Path(path).stem
        repo.upsert_pdf(hash_, path, size, path, self.reader_view.page_count, title)

        self.current_hash = hash_
        self.title_label.set_text(f"{title}  ({self.reader_view.page_count} pages)")
        self.chat_panel.set_book(hash_)
        self.chat_panel.restore_anchors_for_current_book()
