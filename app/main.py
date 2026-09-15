from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk  # noqa: E402

from app.db.schema import init_db
from app.window import MainWindow


class PdfReaderApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.customPdfReader", flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window = None
        self._open_path = None

    def do_activate(self):
        if self.window is None:
            init_db()
            self.window = MainWindow(self)
        self.window.present()
        if self._open_path:
            self.window.open_path(self._open_path)
            self._open_path = None

    def do_open(self, files, n_files, hint):
        if files:
            self._open_path = files[0].get_path()
        self.activate()


def main():
    app = PdfReaderApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
