"""Library: browse previously opened PDFs, organized into folders.

The library has two views:

1. **Folder list (default)** — one row per user-created folder, plus an
   "Unfiled" pseudo-folder for books not yet assigned. The header has
   "＋ Add PDF…" and "＋ New Folder…" buttons.

2. **Folder detail** — entered by clicking a folder row; shows the books
   in that folder using the same row layout the old flat library used.
   Header shows the folder name and a "← Folders" button to return.

Callbacks the host window provides:
  on_open_pdf(path, hash_)                  — open a PDF in the reader.
  on_add_pdf(_btn)                          — show the file-chooser dialog
                                              (`_btn` is forwarded from the
                                              "clicked" signal so the host
                                              can ignore it).
  on_remove_pdf(hash_)                      — drop a PDF from the library.
  on_create_folder(name)                    — create a new folder.
  on_rename_folder(folder_id, new_name)     — rename a folder.
  on_delete_folder(folder_id)               — delete a folder (books → Unfiled).
  on_move_pdf_to_folder(pdf_hash, fid|None) — assign a PDF to a folder
                                              (None = Unfiled).
  on_open_folder(fid|None)                  — drill into a folder
                                              (None = Unfiled).
"""
from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from app.db import repo


# Sentinel for the "Unfiled" pseudo-folder — distinct from any real
# folder id (which are positive SQLite autoincrement ints) and from None
# (which here means "no folder open / all books").
UNFILED = "unfiled"


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


def _prompt_text(parent: Gtk.Window, title: str, message: str, initial: str = "") -> str | None:
    """Single-field input dialog. Returns the entered string (stripped),
    or None if the user cancelled. Used for "New Folder" and "Rename
    Folder" — both want the same UI shape, so they share this helper
    rather than each inlining a full Gtk.Dialog build."""
    dialog = Gtk.Dialog(title=title, transient_for=parent, modal=True,
                        buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                 Gtk.STOCK_OK, Gtk.ResponseType.OK))
    box = dialog.get_content_area()
    box.set_border_width(12)
    box.set_spacing(8)
    box.add(Gtk.Label(label=message))
    entry = Gtk.Entry()
    entry.set_text(initial)
    entry.set_activates_default(True)
    box.add(entry)
    dialog.set_default_response(Gtk.ResponseType.OK)
    dialog.show_all()
    response = dialog.run()
    text = entry.get_text().strip() if response == Gtk.ResponseType.OK else None
    dialog.destroy()
    return text


def _confirm(parent: Gtk.Window, title: str, message: str) -> bool:
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True,
                               message_type=Gtk.MessageType.QUESTION,
                               buttons=Gtk.ButtonsType.YES_NO, text=message)
    dialog.set_title(title)
    response = dialog.run()
    dialog.destroy()
    return response == Gtk.ResponseType.YES


class LibraryView(Gtk.Box):
    def __init__(
        self,
        on_open_pdf,
        on_add_pdf,
        on_remove_pdf,
        on_create_folder,
        on_rename_folder,
        on_delete_folder,
        on_move_pdf_to_folder,
        on_open_folder,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.on_open_pdf = on_open_pdf
        self.on_add_pdf = on_add_pdf
        self.on_remove_pdf = on_remove_pdf
        self.on_create_folder = on_create_folder
        self.on_rename_folder = on_rename_folder
        self.on_delete_folder = on_delete_folder
        self.on_move_pdf_to_folder = on_move_pdf_to_folder
        self.on_open_folder = on_open_folder
        self.set_border_width(16)

        # Header (label + action buttons) — rebuilt per mode so the
        # available actions match the current view.
        self.header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.header_label = Gtk.Label(xalign=0)
        self.header_label.set_markup("<b>Library</b>")
        self.header_box.pack_start(self.header_label, True, True, 0)
        self.header_actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.header_box.pack_start(self.header_actions_box, False, False, 0)
        self.pack_start(self.header_box, False, False, 0)

        # Empty-state label, shared across modes (text is set per mode).
        self.empty_label = Gtk.Label(xalign=0.5)
        self.empty_label.set_margin_top(40)
        self.pack_start(self.empty_label, False, False, 0)

        # Scrolled list of folder or PDF rows.
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.add(self.listbox)
        self.pack_start(scroller, True, True, 0)

        # `None`        → showing the folder list.
        # `(UNFILED,)`  → showing books with no folder assigned.
        # `(int,)`      → showing books inside folder with that id.
        self._current: tuple | None = None
        self.refresh()

    # ---- top-level refresh ------------------------------------------

    def refresh(self) -> None:
        """Rebuild the visible list for the current mode. Pulls fresh
        data every call so external changes (a PDF added via the toolbar's
        Open PDF… while a folder is open, a folder renamed elsewhere) are
        picked up without a separate invalidation API."""
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self._rebuild_header()

        if self._current is None:
            self._populate_folder_list()
        else:
            self._populate_pdf_list(self._current[0])
        self.listbox.show_all()

    def show_folders(self) -> None:
        """Pop up to the folder list from anywhere — used by the toolbar's
        Library button so the user always lands on the top-level view,
        not whatever folder they happened to be reading the last time
        they were in the library. Without this, `refresh()` preserves
        `_current` and re-renders the same folder on every Library click,
        which feels like the button is broken."""
        self._current = None
        self.refresh()

    def _rebuild_header(self) -> None:
        # Drop every existing action button — easier than tracking which
        # ones belong to which mode and risking a stale button hanging
        # around after a mode switch.
        for child in self.header_actions_box.get_children():
            self.header_actions_box.remove(child)

        if self._current is None:
            self.header_label.set_markup("<b>Library</b>")
            add_btn = Gtk.Button(label="＋ Add PDF…")
            # The button has to be forwarded so the host's signature
            # `_on_open_clicked(self, _btn)` is satisfied. The host
            # ignores it, but the call still needs the argument — without
            # it the lambda fires `TypeError` and GTK silently swallows
            # the exception, leaving the button visibly doing nothing.
            add_btn.connect("clicked", lambda _b: self.on_add_pdf(_b))
            self.header_actions_box.pack_start(add_btn, False, False, 0)
            new_btn = Gtk.Button(label="＋ New Folder…")
            new_btn.connect("clicked", self._on_new_folder_clicked)
            self.header_actions_box.pack_start(new_btn, False, False, 0)
        else:
            fid = self._current[0]
            folder = repo.get_folder(fid) if fid != UNFILED else None
            title = folder["name"] if folder else "Unfiled"
            self.header_label.set_markup(
                f"<b>{GLib.markup_escape_text(title)}</b>"
            )
            back_btn = Gtk.Button(label="← Folders")
            back_btn.connect("clicked", self._on_back_clicked)
            self.header_actions_box.pack_start(back_btn, False, False, 0)
            add_btn = Gtk.Button(label="＋ Add PDF…")
            add_btn.connect("clicked", lambda _b: self.on_add_pdf(_b))
            self.header_actions_box.pack_start(add_btn, False, False, 0)
            if fid != UNFILED:
                rename_btn = Gtk.Button(label="Rename")
                rename_btn.connect("clicked", self._make_rename_handler(fid))
                self.header_actions_box.pack_start(rename_btn, False, False, 0)
                delete_btn = Gtk.Button(label="Delete")
                delete_btn.connect("clicked", self._make_delete_folder_handler(fid, title))
                self.header_actions_box.pack_start(delete_btn, False, False, 0)

        # Make sure the buttons we just added count toward this row's
        # minimum width. Without this, `actions_box` reports a 1-pixel
        # minimum to the parent box's layout pass — GTK then gives the
        # `expand=True` label all the leftover horizontal space and the
        # buttons get clipped to a 1×19 strip (and themselves stay
        # `visible=False` because their parent has zero area to expose).
        # `show_all()` recursively shows each packed button so they
        # participate in size negotiation.
        self.header_actions_box.show_all()

    # ---- folder list mode -------------------------------------------

    def _populate_folder_list(self) -> None:
        folders = repo.list_folders()
        unfiled = repo.count_unfiled_pdfs()
        has_any = bool(folders) or unfiled > 0
        self.empty_label.set_visible(not has_any)
        self.empty_label.set_text(
            "No folders or PDFs yet.\n"
            "Click “＋ Add PDF…” to open a file, or “＋ New Folder…” to create one."
        )

        for f in folders:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hbox.set_border_width(8)

            open_btn = Gtk.Button()
            open_btn.set_relief(Gtk.ReliefStyle.NONE)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(
                label=f"<b>📁 {GLib.markup_escape_text(f['name'])}</b>",
                use_markup=True, xalign=0,
            )
            title_label.set_ellipsize(Pango.EllipsizeMode.END)
            count = f["book_count"]
            subtitle_label = Gtk.Label(
                label=f"{count} book{'s' if count != 1 else ''}",
                xalign=0,
            )
            subtitle_label.get_style_context().add_class("dim-label")
            inner.pack_start(title_label, False, False, 0)
            inner.pack_start(subtitle_label, False, False, 0)
            open_btn.add(inner)
            open_btn.connect("clicked", self._make_open_folder_handler(f["id"]))
            hbox.pack_start(open_btn, True, True, 0)

            rename_btn = Gtk.Button(label="Rename")
            rename_btn.set_relief(Gtk.ReliefStyle.NONE)
            rename_btn.connect("clicked", self._make_rename_handler(f["id"]))
            hbox.pack_start(rename_btn, False, False, 0)

            delete_btn = Gtk.Button(label="Delete")
            delete_btn.set_relief(Gtk.ReliefStyle.NONE)
            delete_btn.connect(
                "clicked", self._make_delete_folder_handler(f["id"], f["name"]),
            )
            hbox.pack_start(delete_btn, False, False, 0)

            row.add(hbox)
            self.listbox.add(row)

        if unfiled > 0:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            hbox.set_border_width(8)

            open_btn = Gtk.Button()
            open_btn.set_relief(Gtk.ReliefStyle.NONE)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(
                label="<b>📄 Unfiled</b>", use_markup=True, xalign=0,
            )
            title_label.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle_label = Gtk.Label(
                label=f"{unfiled} book{'s' if unfiled != 1 else ''} not in any folder",
                xalign=0,
            )
            subtitle_label.get_style_context().add_class("dim-label")
            inner.pack_start(title_label, False, False, 0)
            inner.pack_start(subtitle_label, False, False, 0)
            open_btn.add(inner)
            open_btn.connect("clicked", self._make_open_folder_handler(UNFILED))
            hbox.pack_start(open_btn, True, True, 0)
            row.add(hbox)
            self.listbox.add(row)

    # ---- folder-detail mode -----------------------------------------

    def _populate_pdf_list(self, folder_id) -> None:
        if folder_id == UNFILED:
            pdfs = repo.list_pdfs(include_unfiled=True)
        else:
            pdfs = repo.list_pdfs(folder_id=folder_id)
        self.empty_label.set_visible(len(pdfs) == 0)
        if folder_id == UNFILED:
            self.empty_label.set_text(
                "No unfiled PDFs.\n"
                "Open a file with “＋ Add PDF…” or move a book here from inside a folder."
            )
        else:
            self.empty_label.set_text("This folder is empty.")

        for p in pdfs:
            self._add_pdf_row(p)

    def _add_pdf_row(self, p) -> None:
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
        open_btn.connect("clicked", self._make_open_pdf_handler(p["path"], p["hash"]))
        hbox.pack_start(open_btn, True, True, 0)

        # Per-row "Move to folder" combo. Built fresh from `list_folders()`
        # so the dropdown always reflects the current set of folders —
        # someone might have created a folder while this row was on screen.
        # NB: `p` is a sqlite3.Row, which supports __getitem__ but not .get()
        # — calling p.get("folder_id") raises AttributeError mid-loop and
        # leaves the listbox empty even though rows were just fetched.
        move_combo = self._build_move_combo(p["hash"], p["folder_id"])
        hbox.pack_start(move_combo, False, False, 0)

        remove_btn = Gtk.Button(label="Remove")
        remove_btn.set_relief(Gtk.ReliefStyle.NONE)
        remove_btn.connect("clicked", self._make_remove_handler(p["hash"]))
        hbox.pack_start(remove_btn, False, False, 0)

        row.add(hbox)
        self.listbox.add(row)

    def _build_move_combo(self, pdf_hash: str, current_folder_id):
        """A short ComboBox listing "Unfiled" + every folder, with the
        PDF's current folder pre-selected. The user picks a new value and
        we immediately call `on_move_pdf_to_folder`; no separate
        "Apply" button needed since the only action is the selection
        itself. We rebuild on each refresh() so deleted folders don't
        linger as orphan options."""
        # Column 0 holds either an int (real folder id from sqlite) or the
        # `UNFILED` string sentinel. Declaring it `int` makes Gtk.ListStore
        # reject the sentinel with TypeError("Must be number, not str"),
        # which used to abort the row-construction loop on the first PDF
        # and leave the whole listbox empty — `object` accepts both.
        model = Gtk.ListStore(object, str)
        # Row 0 = "Unfiled". Use the UNFILED sentinel as the id so the
        # callback's `fid is None` branch handles it without a special case.
        model.append([UNFILED, "Unfiled"])
        for f in repo.list_folders():
            model.append([f["id"], f["name"]])

        combo = Gtk.ComboBox.new_with_model(model)
        renderer = Gtk.CellRendererText()
        combo.pack_start(renderer, True)
        combo.add_attribute(renderer, "text", 1)
        # Preselect the current folder. `iter_nth_child` is safer than
        # assuming a numeric position — if the current folder has been
        # deleted, the combo defaults to "Unfiled" instead of throwing.
        target_value = current_folder_id if current_folder_id is not None else UNFILED
        for i, row in enumerate(model):
            if row[0] == target_value:
                combo.set_active(i)
                break
        else:
            combo.set_active(0)
        combo.connect("changed", self._make_move_handler(pdf_hash, model))
        return combo

    # ---- event handlers ---------------------------------------------

    def _on_new_folder_clicked(self, _btn) -> None:
        name = _prompt_text(
            self.get_toplevel(), "New Folder",
            "Folder name:",
        )
        if name:
            try:
                self.on_create_folder(name)
            except ValueError as e:
                self._show_error(str(e))
                return
            self.refresh()

    def _on_back_clicked(self, _btn) -> None:
        self._current = None
        self.refresh()

    def _make_open_pdf_handler(self, path, hash_):
        return lambda _b: self.on_open_pdf(path, hash_)

    def _make_remove_handler(self, hash_):
        return lambda _b: self.on_remove_pdf(hash_)

    def _make_open_folder_handler(self, fid):
        return lambda _b: self._enter_folder(fid)

    def _enter_folder(self, fid) -> None:
        self._current = (fid,)
        # Hand off the navigation to the host so any side-effects (e.g.
        # analytics, future "remember last opened folder") live in one
        # place. The library view itself only owns the visible state.
        self.on_open_folder(fid)
        self.refresh()

    def _make_rename_handler(self, fid):
        def handler(_btn):
            folder = repo.get_folder(fid)
            if folder is None:
                return
            new_name = _prompt_text(
                self.get_toplevel(), "Rename Folder",
                "New folder name:", initial=folder["name"],
            )
            if new_name and new_name != folder["name"]:
                try:
                    self.on_rename_folder(fid, new_name)
                except ValueError as e:
                    self._show_error(str(e))
                    return
                self.refresh()
        return handler

    def _make_delete_folder_handler(self, fid, name):
        def handler(_btn):
            if not _confirm(
                self.get_toplevel(), "Delete Folder",
                f"Delete folder “{name}”?\n"
                "Books inside will be moved back to Unfiled.",
            ):
                return
            self.on_delete_folder(fid)
            # If we were inside this folder, fall back to the folder
            # list so the user isn't stranded on an empty page that
            # references a now-gone folder.
            if self._current is not None and self._current[0] == fid:
                self._current = None
            self.refresh()
        return handler

    def _make_move_handler(self, pdf_hash, model):
        def handler(combo):
            iter_ = combo.get_active_iter()
            if iter_ is None:
                return
            value = model[iter_][0]
            new_fid = None if value == UNFILED else int(value)
            self.on_move_pdf_to_folder(pdf_hash, new_fid)
            # The combobox already shows the new selection, so a full
            # refresh would be redundant — but we still want the parent
            # folder list's count badges to update. Trigger a full
            # rebuild; cheap because folder lists are small.
            self.refresh()
        return handler

    def _show_error(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(), modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text=message,
        )
        dialog.run()
        dialog.destroy()