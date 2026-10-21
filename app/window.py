"""Main window: toolbar + library/reader stack."""
from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("EvinceView", "3.0")
from gi.repository import EvinceView, Gdk, GLib, Gtk  # noqa: E402

from app import pdf_ops, settings
from app.chat.panel import ChatPanel
from app.db import repo
from app.library_view import LibraryView
from app.reader.view import ReaderView

ZOOM_LEVELS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]


# Pure-black override applied only in dark mode: Adwaita-Dark's greys and
# Evince's page frame/shadow otherwise show up as a light border around
# the PDF and between panes.
_DARK_CSS = b"""
window, .background, stack, paned, scrolledwindow, viewport, .view,
.content-view, .document-page, EvView, list, row, box, grid { background-color: #000000; background-image: none; }
list row { border-bottom: 1px solid #222222; }
list row:hover { background-color: #111111; }
entry, searchentry { background-color: #111111; background-image: none; color: #e6e6e6; }
label { color: #e6e6e6; }
eventbox, frame, expander, toolbar, headerbar, actionbar, notebook, revealer { background-color: #000000; background-image: none; }
frame > border { border: none; }
button, combobox button, togglebutton, spinbutton, spinbutton button {
  background-color: #1a1a1a; background-image: none; color: #e6e6e6;
  border: 1px solid #333333; box-shadow: none; text-shadow: none; }
button:hover, togglebutton:hover { background-color: #2a2a2a; background-image: none; }
button:active, button:checked { background-color: #333333; background-image: none; }
button.flat { background-color: transparent; border-color: transparent; }
button.flat:hover { background-color: #2a2a2a; }
button label, button image { color: #e6e6e6; }
.dim-label { color: #9a9a9a; }
menu, menuitem, popover, popover.background { background-color: #111111; background-image: none; color: #e6e6e6; }
scrollbar, scrollbar trough { background-color: #000000; background-image: none; }
scrollbar slider { background-color: #444444; }
scrolledwindow, .document-page { border: none; box-shadow: none; }
paned > separator { background-color: #000000; background-image: none; min-width: 1px; min-height: 1px; }
"""


def _has_dark_variant(theme_name: str) -> bool:
    """Return True if `<theme_name>-Dark` is installed under any of the
    standard theme search paths. Used by `_set_dark_mode` to fall back
    to a theme with a dark sibling when the user's current theme lacks
    one (e.g. legacy `Raleigh` on a fresh install)."""
    import os
    candidates = [
        f"/usr/share/themes/{theme_name}-Dark/gtk-3.0",
        os.path.expanduser(f"~/.themes/{theme_name}-Dark/gtk-3.0"),
    ]
    return any(os.path.isdir(p) for p in candidates)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Custom PDF Reader")
        self.set_default_size(1200, 850)

        self.reader_view = ReaderView()
        # Build the chat panel with the user's initial dark-mode preference
        # so the WebKit view's body class is correct on the very first paint
        # — `MainWindow._set_dark_mode` then forwards live toggles.
        self.chat_panel = ChatPanel(
            self.reader_view, initial_dark_mode=settings.DARK_MODE_DEFAULT,
        )
        self.reader_view.on_selection_changed = self._on_reader_selection_changed
        self.reader_view.on_page_changed = self._on_page_changed
        self.library_view = LibraryView(
            on_open_pdf=self._open_from_library,
            on_add_pdf=self._on_open_clicked,
            on_remove_pdf=self._on_remove_from_library,
            on_create_folder=self._on_create_folder,
            on_rename_folder=self._on_rename_folder,
            on_delete_folder=self._on_delete_folder,
            on_move_pdf_to_folder=self._on_move_pdf_to_folder,
            on_open_folder=self._on_open_folder,
        )

        # Search state. `_search_results` is a list of (page_index, rect_dict)
        # tuples in document order; `_search_index` is the cursor pointing at
        # the currently-shown match (None means no active search).
        self._search_results: list[tuple[int, dict]] = []
        self._search_index: int | None = None
        self._search_query: str = ""
        self._search_cancel: list[bool] = [False]  # one-element list used as a mutable flag
        self._search_thread: threading.Thread | None = None

        # Tracks the dark-mode state for the PDF renderer, so newly loaded
        # documents pick up the right inversion on `load()` without the user
        # having to flip the toggle again.
        self._pdf_inverted = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        root.pack_start(self._build_toolbar(), False, False, 0)
        root.pack_start(self._build_search_bar(), False, False, 0)

        reader_pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        reader_pane.pack1(self.reader_view, True, False)
        reader_pane.pack2(self.chat_panel, False, False)
        reader_pane.set_position(820)

        self.stack = Gtk.Stack()
        self.stack.add_named(self.library_view, "library")
        self.stack.add_named(reader_pane, "reader")
        root.pack_start(self.stack, True, True, 0)

        self.current_hash: str | None = None
        self.show_all()
        self.stack.set_visible_child_name("library")
        # show_all() force-shows every child, including the empty-state
        # label refresh() had hidden — reapply visibility after it.
        self.library_view.refresh()
        self._set_search_bar_visible(False)
        # Build the chat panel HTML with `body class="light"` already set
        # so we don't get a flash of unstyled content. Apply the user's
        # dark-mode preference on top of that — `_set_dark_mode` is a
        # no-op for the chat panel if the state hasn't changed, so this is
        # safe to call even when DARK_MODE_DEFAULT is False.
        self._set_dark_mode(settings.DARK_MODE_DEFAULT, persist=False)
        # Window-level Ctrl+C fallback: the reader view already has its own
        # key handler, but if focus drifted (e.g. user clicked the search
        # bar or toolbar) that handler won't fire. Catching Ctrl+C here and
        # delegating to the reader's copy_selection() makes the shortcut
        # work regardless of where focus is, as long as the PDF has a live
        # selection. We only delegate when the focused widget isn't itself
        # a text editor — those (chat composer, search entry) handle their
        # own clipboard correctly, and we must not steal Ctrl+C from them.
        self.connect("key-press-event", self._on_window_key_press)

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

        # Page-number jump: a SpinButton (1-indexed) with an "of N" label.
        # Disabled until a document is loaded; range 1..N is set on each load.
        # Pressing Enter / focusing-out jumps to the typed page; the
        # step buttons below it are the same prev/next arrows already in the
        # toolbar, so a SpinButton here is the more precise input.
        self.page_spin = Gtk.SpinButton.new_with_range(1, 1, 1)
        self.page_spin.set_tooltip_text("Jump to page")
        self.page_spin.set_width_chars(5)
        self.page_spin.set_sensitive(False)
        # Store the handler id so we can block it while we update the spin's
        # value programmatically in response to the model's page-changed
        # signal — otherwise the spin would echo that back into a redundant
        # go_to_page. handler_block_by_func fails to match bound methods
        # through PyGObject's marshaling, so we block by id instead.
        self._page_spin_handler = self.page_spin.connect("value-changed", self._on_page_spin_changed)
        bar.pack_start(self.page_spin, False, False, 0)
        self.page_of_label = Gtk.Label(label="of 0")
        self.page_of_label.get_style_context().add_class("dim-label")
        bar.pack_start(self.page_of_label, False, False, 0)

        self.title_label = Gtk.Label(label="No document loaded")
        bar.pack_start(self.title_label, True, True, 0)

        # Dark-mode toggle. The label flips between "🌙 Dark" and "☀ Light"
        # to mirror the action (and double as a state indicator), instead of
        # shipping a separate status widget.
        self.dark_btn = Gtk.Button(label="🌙 Dark")
        self.dark_btn.set_tooltip_text("Toggle dark mode")
        self.dark_btn.connect("clicked", lambda _b: self._toggle_dark_mode())
        bar.pack_end(self.dark_btn, False, False, 0)

        # Search button toggles the search bar (see _build_search_bar).
        search_btn = Gtk.Button(label="🔍")
        search_btn.set_tooltip_text("Find text in document")
        search_btn.connect("clicked", self._on_toggle_search_bar)
        bar.pack_end(search_btn, False, False, 0)

        # Copy selected PDF text to the clipboard. Hooked at the toolbar so
        # it shows up in the same place as the rest of the document actions
        # — gives a clickable fallback to the Ctrl+C keyboard shortcut for
        # users who haven't discovered it yet, and keeps the keyboard
        # shortcut visible alongside other read-only operations.
        self.copy_btn = Gtk.Button(label="📋 Copy")
        self.copy_btn.set_tooltip_text(
            "Copy the selected PDF text to the clipboard (Ctrl+C)"
        )
        self.copy_btn.connect("clicked", self._on_copy_clicked)
        self.copy_btn.set_sensitive(False)
        bar.pack_end(self.copy_btn, False, False, 0)

        return bar

    def _build_search_bar(self) -> Gtk.Box:
        """A second toolbar strip — hidden by default — that holds the
        search input, prev/next/count, and a close button. Kept separate
        from the main toolbar so the toolbar stays compact and the search
        UI can be expanded/collapsed cleanly."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_border_width(6)
        bar.set_name("search-bar")

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Find text…")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("activate", self._on_search_activate)
        self.search_entry.connect("stop-search", self._on_search_close)
        # The SearchEntry's built-in arrow buttons emit these — wire them
        # to the same prev/next search-step handlers the toolbar buttons use,
        # so the user has two ways to step through matches.
        self.search_entry.connect("next-match", lambda _e: self._step_search(+1))
        self.search_entry.connect("previous-match", lambda _e: self._step_search(-1))
        bar.pack_start(self.search_entry, True, True, 0)

        self.search_match_label = Gtk.Label(label="")
        self.search_match_label.get_style_context().add_class("dim-label")
        bar.pack_start(self.search_match_label, False, False, 0)

        self.search_prev_btn = Gtk.Button(label="▲")
        self.search_prev_btn.set_tooltip_text("Previous match (Shift+Enter)")
        self.search_prev_btn.connect("clicked", lambda _b: self._step_search(-1))
        bar.pack_start(self.search_prev_btn, False, False, 0)

        self.search_next_btn = Gtk.Button(label="▼")
        self.search_next_btn.set_tooltip_text("Next match (Enter)")
        self.search_next_btn.connect("clicked", lambda _b: self._step_search(+1))
        bar.pack_start(self.search_next_btn, False, False, 0)

        self.search_case_btn = Gtk.ToggleButton(label="Aa")
        self.search_case_btn.set_tooltip_text("Match case")
        bar.pack_start(self.search_case_btn, False, False, 0)

        self.search_close_btn = Gtk.Button(label="✕")
        self.search_close_btn.set_tooltip_text("Close search (Esc)")
        self.search_close_btn.connect("clicked", self._on_search_close_clicked)
        bar.pack_start(self.search_close_btn, False, False, 0)

        # Disable controls until a search runs. Re-enabled in
        # _update_search_label after results arrive.
        for w in (self.search_prev_btn, self.search_next_btn):
            w.set_sensitive(False)
        return bar

    def _set_search_bar_visible(self, visible: bool) -> None:
        self.search_entry.get_parent().set_visible(visible)
        if visible:
            self.search_entry.grab_focus()

    def _on_toggle_search_bar(self, _btn) -> None:
        bar = self.search_entry.get_parent()
        if bar.get_visible():
            self._on_search_close_clicked(None)
        else:
            self._set_search_bar_visible(True)
            self._update_search_label()

    # ---- library / opening ------------------------------------------------

    def _on_library_clicked(self, _btn) -> None:
        # Always pop to the folder list, not whatever folder was open
        # the last time we were in the library — `library_view.refresh()`
        # alone preserves its internal `_current` state and would just
        # re-render the same folder, which looks like the button did
        # nothing.
        self.library_view.show_folders()
        self.stack.set_visible_child_name("library")

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

    def _open_from_library(self, path: str, hash_: str) -> None:
        self.open_path(path, known_hash=hash_)

    def _on_remove_from_library(self, hash_: str) -> None:
        repo.delete_pdf(hash_)
        self.library_view.refresh()

    # ---- folder callbacks ---------------------------------------------

    def _on_create_folder(self, name: str) -> None:
        """Create a folder via the repo. Errors are surfaced to the user
        by the library view itself, so we don't need to catch here — but
        we do still call refresh() afterwards so the new row appears even
        if the view was rebuilt before the dialog returned."""
        repo.create_folder(name)
        self.library_view.refresh()

    def _on_rename_folder(self, folder_id: int, new_name: str) -> None:
        repo.rename_folder(folder_id, new_name)
        self.library_view.refresh()

    def _on_delete_folder(self, folder_id: int) -> None:
        repo.delete_folder(folder_id)
        self.library_view.refresh()

    def _on_move_pdf_to_folder(self, pdf_hash: str, folder_id: int | None) -> None:
        repo.set_pdf_folder(pdf_hash, folder_id)
        self.library_view.refresh()

    def _on_open_folder(self, _fid) -> None:
        """Navigation callback fired when the user clicks a folder row.
        We don't need to do anything here — the library view already
        updates its own `_current` state and refreshes its list — but we
        keep the wiring so future side-effects (e.g. analytics, an
        "opened folder" indicator in the toolbar) have a single chokepoint
        rather than reaching into the view directly."""
        pass

    def open_path(self, path: str, known_hash: str | None = None) -> None:
        try:
            hash_, size = pdf_ops.hash_file(Path(path))
            self.reader_view.load(path)
        except (FileNotFoundError, OSError, GLib.Error) as e:
            self._show_error(f"Couldn't open “{path}”.\n\n{e}")
            return

        # Clear any leftover search state from the previous document so the
        # new one starts on a fresh search bar.
        self._reset_search_state()

        title = Path(path).stem
        repo.upsert_pdf(hash_, path, size, path, self.reader_view.page_count, title)

        self.current_hash = hash_
        self.title_label.set_text(f"{title}  ({self.reader_view.page_count} pages)")
        self.chat_panel.set_book(hash_)
        self.chat_panel.restore_anchors_for_current_book()
        self.stack.set_visible_child_name("reader")

        n = self.reader_view.page_count
        self.page_spin.set_range(1, max(1, n))
        self.page_spin.set_sensitive(n > 0)
        self.page_of_label.set_text(f"of {n}")
        self.page_spin.set_value(1)

        # Carry the dark-mode PDF inversion across to the freshly-loaded
        # document — otherwise opening a new PDF in dark mode would render
        # the page in default (light) colors until the user re-toggled.
        self.reader_view.inverted_colors = self._pdf_inverted

    # ---- dark mode -------------------------------------------------------

    def _set_dark_mode(self, enabled: bool, persist: bool = True) -> None:
        """Apply or remove the application's dark-mode preference.

        GTK3 has two related settings:
        - `gtk-application-prefer-dark-theme` — asks GTK to use the dark
          variant of the current theme. Honored at construction time only;
          changing it after widgets exist has no visible effect.
        - `gtk-theme-name` — the active theme. Setting this *does* force
          a full style re-application, which is what makes the runtime
          toggle actually repaint everything.

        We flip both so the intent is recorded correctly even if a future
        GTK version starts honoring the preference at runtime, and so
        external apps reading the X11 `_GTK_THEME_VARIANT` property see
        the right value.

        Theme name resolution: GTK only renders dark-mode visually when the
        active theme has a sibling `<name>-Dark` variant installed. If
        `gtk-theme-name` is `Raleigh` (the no-deps fallback) there is no
        `Raleigh-Dark`, so flipping it does nothing visible. To make the
        toggle work out-of-the-box on a default install we resolve to a
        theme that ships a dark variant — Adwaita is always present on
        GTK3-aware systems, so we prefer it when the active theme has no
        dark sibling.

        PDF inversion: in dark mode, also enable EvinceView's
        `inverted-colors` so the page renders as white-on-black instead of
        black-on-white. The DocumentModel's setting is independent of the
        GTK theme, so toggling one without the other would leave the page
        either blindingly bright (no inversion in dark mode) or stuck
        inverted after a switch back to light mode."""
        gtk_settings = Gtk.Settings.get_default()
        current = gtk_settings.get_property("gtk-theme-name") or ""
        base = current[:-5] if current.endswith("-Dark") else current
        if base and not _has_dark_variant(base):
            # Fall back to Adwaita, which is bundled with GTK3 and always
            # has a dark sibling — better than rendering the toggle as a
            # no-op just because the user has a legacy theme installed.
            base = "Adwaita"
        elif not base:
            base = "Adwaita"
        gtk_settings.set_property("gtk-application-prefer-dark-theme", enabled)
        gtk_settings.set_property(
            "gtk-theme-name", f"{base}-Dark" if enabled else base,
        )
        if not hasattr(self, "_dark_provider"):
            self._dark_provider = Gtk.CssProvider()
            self._dark_provider.load_from_data(_DARK_CSS)
        screen = Gdk.Screen.get_default()
        if enabled:
            Gtk.StyleContext.add_provider_for_screen(
                screen, self._dark_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        else:
            Gtk.StyleContext.remove_provider_for_screen(screen, self._dark_provider)
        self.dark_btn.set_label("☀ Light" if enabled else "🌙 Dark")
        self._pdf_inverted = enabled
        # Only push the inversion to the renderer if a document is loaded —
        # `open_path()` will replay this state once it sets the new doc.
        if self.reader_view.document is not None:
            self.reader_view.inverted_colors = enabled
        # The chat panel runs its own CSS (WebKit-rendered Markdown/KaTeX),
        # independent of the GTK theme, so it needs an explicit push.
        self.chat_panel.set_dark_mode(enabled)
        if persist:
            settings.DARK_MODE = enabled

    def _toggle_dark_mode(self) -> None:
        self._set_dark_mode(not settings.DARK_MODE)

    # ---- page number -----------------------------------------------------

    def _on_page_changed(self, page_index: int) -> None:
        """Kept in sync with the visible SpinButton. Set the value
        programmatically; block the value-changed signal so we don't
        trigger another go_to_page."""
        self.page_spin.handler_block(self._page_spin_handler)
        try:
            self.page_spin.set_value(page_index + 1)
        finally:
            self.page_spin.handler_unblock(self._page_spin_handler)

    def _on_page_spin_changed(self, spin) -> None:
        if self.reader_view.document is None:
            return
        target = int(spin.get_value()) - 1
        if target != self.reader_view.current_page:
            self.reader_view.go_to_page(target)

    # ---- copy / selection ----------------------------------------------

    def _on_reader_selection_changed(self, text: str | None) -> None:
        """Forward reader selection events to both the chat panel (its
        existing behavior — drives the "Selected: …" banner and lets the
        next sent message use the new selection as its anchor) and the
        toolbar Copy button (new — keeps the button enabled iff there's
        actually something worth copying)."""
        self.chat_panel.on_selection_changed(text)
        # copy_btn is built in _build_toolbar; it's fine to touch the
        # attribute unconditionally — the only call site is __init__ and
        # this handler.
        if getattr(self, "copy_btn", None) is not None:
            self.copy_btn.set_sensitive(bool(text))

    def _on_copy_clicked(self, _btn) -> None:
        """Toolbar button → forward to the reader view's copy handler."""
        self.reader_view.copy_selection()

    # ---- search ----------------------------------------------------------

    def _on_search_activate(self, entry) -> None:
        """Run the search when the user presses Enter inside the entry.
        Shift+Enter steps backwards (the SearchEntry emits "next-match"
        /"previous-match" for arrow buttons, but pressing Enter on the
        entry itself just emits "activate" — we synthesize a step from
        a KeyboardEvent look at modifier state below)."""
        # SearchEntry doesn't expose Shift state cleanly on activate, but
        # it does emit `next-match` / `previous-match` on its embedded
        # arrow buttons. For typed Enter, just kick off a fresh search.
        self._run_search()

    def _on_search_close(self, entry) -> None:
        self._set_search_bar_visible(False)
        self.reader_view.clear_search_highlights()
        self._reset_search_state()

    def _on_search_close_clicked(self, _btn) -> None:
        self._set_search_bar_visible(False)
        self.reader_view.clear_search_highlights()
        self._reset_search_state()

    def _reset_search_state(self) -> None:
        self._search_results = []
        self._search_index = None
        self._search_query = ""
        self._search_cancel[0] = True  # cancel any in-flight search
        self.search_match_label.set_text("")
        for w in (self.search_prev_btn, self.search_next_btn):
            w.set_sensitive(False)

    def _run_search(self) -> None:
        query = self.search_entry.get_text().strip()
        if not query or self.reader_view.document is None:
            return
        if query == self._search_query:
            # Same query as last time → just advance to next match.
            self._step_search(+1)
            return
        # New query: cancel any in-flight search, clear old hits, and start.
        self._search_cancel[0] = True
        self.reader_view.clear_search_highlights()
        self._search_results = []
        self._search_index = None
        self._search_query = query
        self._search_cancel = [False]
        case_sensitive = self.search_case_btn.get_active()
        self.search_match_label.set_text("Searching…")
        cancel_flag = self._search_cancel
        view = self.reader_view

        def worker():
            results = view.find_text_all(
                query, case_sensitive=case_sensitive, cancel_flag=cancel_flag,
            )
            GLib.idle_add(self._on_search_complete, query, results, case_sensitive)

        thread = threading.Thread(target=worker, daemon=True)
        self._search_thread = thread
        thread.start()

    def _on_search_complete(self, query, results, case_sensitive) -> bool:
        # If a newer search started after this one was kicked off, drop these
        # results — they don't reflect the current query anymore.
        if query != self._search_query:
            return False
        self._search_results = results
        if not results:
            self.search_match_label.set_text("No matches")
            self.search_match_label.get_style_context().remove_class("dim-label")
            for w in (self.search_prev_btn, self.search_next_btn):
                w.set_sensitive(False)
            return False
        # Drop a translucent orange highlight on every match across the
        # document so the user can see what was found at a glance — even
        # matches on pages they haven't navigated to. The current match
        # gets its own focus via navigation; we don't make it stand out
        # from the rest, since color-coding "current vs. other" only makes
        # search hits easier to confuse with anchor highlights (yellow).
        for page_index, rect in results:
            self.reader_view.highlight_search_match(page_index, rect)
        self._step_search(+1, from_initial=True)
        return False

    def _step_search(self, direction: int, from_initial: bool = False) -> None:
        """Move the search cursor by `direction` (±1) and jump the reader
        to that match. `from_initial=True` on the first match after a fresh
        search so we land on the first result rather than `current + 1`
        (which would skip past it for searches typed on a later page)."""
        if not self._search_results:
            return
        n = len(self._search_results)
        if from_initial:
            self._search_index = 0
        else:
            self._search_index = (self._search_index + direction) % n
        page_index, rect = self._search_results[self._search_index]
        self.reader_view.go_to_anchor(page_index, rect)
        self._update_search_label()

    def _update_search_label(self) -> None:
        n = len(self._search_results)
        if n == 0 or self._search_index is None:
            self.search_match_label.set_text("")
            return
        self.search_match_label.set_text(f"{self._search_index + 1} / {n}")
        for w in (self.search_prev_btn, self.search_next_btn):
            w.set_sensitive(True)

    def _show_error(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text=message,
        )
        dialog.run()
        dialog.destroy()

    # ---- keyboard fallback ---------------------------------------------

    def _focused_widget_is_text_editor(self) -> bool:
        """True when the widget with keyboard focus already handles its
        own clipboard — chat composer (Gtk.Entry), search bar (Gtk.SearchEntry),
        or any TextView. Used by the window-level Ctrl+C handler to decide
        whether to delegate to the reader view, or to leave the event alone
        so the focused widget gets a clean copy/paste."""
        focus = self.get_focus()
        if focus is None:
            return False
        # Gtk.Editable covers Entry, SearchEntry, SpinButton (when editable),
        # TextView, and friends. Using the interface is more reliable than
        # isinstance() against each concrete class across GTK versions.
        try:
            from gi.repository import Gtk as _Gtk
            return isinstance(focus, _Gtk.Editable) or isinstance(focus, _Gtk.TextView)
        except Exception:
            return False

    def _on_window_key_press(self, _widget, event):
        """Window-level Ctrl+C fallback: when the PDF has an active text
        selection and focus is NOT in a text-editing widget (so the event
        won't already be copy-pasting that widget's own buffer), route
        Ctrl+C to the reader's copy handler.

        This complements ReaderView's own key handler — that one only
        fires when the EvView has focus, but focus often drifts to the
        toolbar or chat panel after a drag-select. Returning True here
        stops the event from propagating further (we've handled it);
        False lets GTK continue normal dispatch for everything else."""
        if event.keyval != Gdk.KEY_c:
            return False
        state = event.get_state()
        # Only act on Ctrl+C / Cmd+C with no other modifiers, so we don't
        # fight Ctrl+Shift+C, Alt+C, etc.
        ctrl_or_cmd = bool(state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.META_MASK))
        if not ctrl_or_cmd:
            return False
        noisy = state & (
            Gdk.ModifierType.SHIFT_MASK
            | Gdk.ModifierType.MOD1_MASK
            | Gdk.ModifierType.MOD2_MASK
            | Gdk.ModifierType.MOD4_MASK
            | Gdk.ModifierType.HYPER_MASK
        )
        noisy &= ~Gdk.ModifierType.META_MASK
        if noisy:
            return False
        # Don't steal Ctrl+C from the chat composer or search bar — they
        # handle their own clipboard correctly and need to be able to
        # copy/paste their own contents.
        if self._focused_widget_is_text_editor():
            return False
        # Only do something if there's actually a PDF selection to copy;
        # otherwise this would silently swallow Ctrl+C across the whole
        # window for no benefit.
        if not self.reader_view.selected_text():
            return False
        self.reader_view.copy_selection()
        return True
