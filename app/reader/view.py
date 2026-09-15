"""Embeds Evince's own rendering widget (EvView) for native PDF display,
text selection, and highlight-annotation anchors.

Anchors are real EvAnnotation (AnnotationTextMarkup / highlight) objects —
Evince's own renderer draws them, so there is no custom overlay-drawing
code. A click on a highlight is hit-tested against the document's
annotation mapping and reported via `on_anchor_clicked`.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("EvinceDocument", "3.0")
gi.require_version("EvinceView", "3.0")
from gi.repository import EvinceDocument, EvinceView, Gdk, Gio, Gtk  # noqa: E402

EvinceDocument.init()

HIGHLIGHT_RGBA = Gdk.RGBA(1.0, 0.85, 0.2, 0.35)  # translucent yellow, like a real highlighter


class ReaderView(Gtk.ScrolledWindow):
    def __init__(self):
        super().__init__()
        self.model = EvinceView.DocumentModel.new()
        self.view = EvinceView.View.new()
        self.view.set_model(self.model)
        self.add(self.view)

        self.document = None
        self.path = None

        self.on_selection_changed = None   # callback(text: str)
        self.on_anchor_created = None       # callback(page_index: int, rect: dict)
        self.on_anchor_clicked = None       # callback(annot)

        self._annot_index: dict[int, tuple[int, object]] = {}  # id(annot) -> (page_index, annot)
        self._last_created: tuple[int, dict] | None = None

        self.view.connect("selection-changed", self._on_selection_changed)
        self.view.connect("annot-added", self._on_annot_added)
        self.view.connect("button-press-event", self._on_button_press)

    # ---- loading -----------------------------------------------------

    def load(self, path: str) -> None:
        uri = Gio.File.new_for_path(path).get_uri()
        document = EvinceDocument.Document.factory_get_document(uri)
        self.document = document
        self.path = path
        self._annot_index.clear()
        self.model.set_document(document)

    @property
    def page_count(self) -> int:
        return self.document.get_n_pages() if self.document else 0

    def page_text(self, page_index: int) -> str | None:
        if self.document is None:
            return None
        text = self.document.get_text(self.document.get_page(page_index))
        text = (text or "").strip()
        return text or None

    def go_to_page(self, page_index: int) -> None:
        self.model.set_page(page_index)

    # ---- selection / anchors -----------------------------------------

    def selected_text(self) -> str | None:
        text = self.view.get_selected_text()
        return text.strip() if text else None

    def _on_selection_changed(self, _view) -> None:
        if self.on_selection_changed:
            self.on_selection_changed(self.selected_text())

    def create_anchor_from_selection(self):
        """Turn the current text selection into a highlight annotation.
        Returns (page_index, rect, annot) synchronously — 'annot-added'
        fires within the call — or None if there's no active selection."""
        text = self.selected_text()
        if not text:
            return None
        self._last_created = None
        self.view.add_text_markup_annotation_for_selected_text()
        return self._last_created

    def _on_annot_added(self, _view, annot) -> None:
        page_index = annot.get_page_index()
        area = annot.get_area()
        annot.set_rgba(HIGHLIGHT_RGBA)
        self._annot_index[id(annot)] = (page_index, annot)
        rect = {"x1": area.x1, "y1": area.y1, "x2": area.x2, "y2": area.y2}
        self._last_created = (page_index, rect, annot)
        if self.on_anchor_created:
            self.on_anchor_created(page_index, rect, annot)

    def restore_anchor(self, page_index: int, rect: dict, contents: str = ""):
        """Recreate a highlight annotation from a persisted anchor (on
        document reopen). Does not fire on_anchor_created. Returns annot."""
        if self.document is None:
            return None
        page = self.document.get_page(page_index)
        annot = EvinceDocument.AnnotationTextMarkup.highlight_new(page)
        area = EvinceDocument.Rectangle()
        area.x1, area.y1, area.x2, area.y2 = rect["x1"], rect["y1"], rect["x2"], rect["y2"]
        annot.set_area(area)
        annot.set_contents(contents)
        annot.set_rgba(HIGHLIGHT_RGBA)
        out_rect = EvinceDocument.Rectangle()
        self.document.add_annotation(annot, out_rect)
        self._annot_index[id(annot)] = (page_index, annot)
        return annot

    def _on_button_press(self, _widget, event):
        """Hit-test against our own tracked annotations, not EvMappingList
        (its `.data` comes back as a raw gpointer int through GI — not
        usable as a Python object — so identity has to come from the
        Annotation instances we already hold from creation/restore)."""
        if event.button != 1 or self.document is None:
            return False
        current = self.model.get_page()
        scale = self.model.get_scale()
        for page_index in range(max(0, current - 2), min(self.page_count, current + 3)):
            rect = Gdk.Rectangle()
            found = self.view.get_page_extents(page_index, rect, Gtk.Border())
            if not found:
                continue
            if not (rect.x <= event.x <= rect.x + rect.width
                    and rect.y <= event.y <= rect.y + rect.height):
                continue
            doc_x = (event.x - rect.x) / scale
            doc_y = (event.y - rect.y) / scale
            for annot_page, annot in self._annot_index.values():
                if annot_page != page_index:
                    continue
                a = annot.get_area()
                if a.x1 <= doc_x <= a.x2 and a.y1 <= doc_y <= a.y2:
                    if self.on_anchor_clicked:
                        self.on_anchor_clicked(annot)
                    return True
        return False
