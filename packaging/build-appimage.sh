#!/bin/bash
# Builds CustomPdfReader-x86_64.AppImage.
#
# See AppRun for why this is a "thin" AppImage (host GTK3/WebKit2GTK/Evince,
# bundled app code + the one non-system Python dep).
set -euo pipefail
cd "$(dirname "$(readlink -f "${0}")")/.."   # repo root

PKG_DIR="$(pwd)/packaging"
APPDIR="$PKG_DIR/AppDir"
OUT="$(pwd)/CustomPdfReader-x86_64.AppImage"

echo "==> Cleaning AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/customPdfReader/pydeps" \
         "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"

echo "==> Copying app source"
cp -r app "$APPDIR/usr/lib/customPdfReader/app"
find "$APPDIR/usr/lib/customPdfReader/app" -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

echo "==> Vendoring non-system Python deps (httpx)"
pip install --quiet --target "$APPDIR/usr/lib/customPdfReader/pydeps" httpx

echo "==> Writing AppRun, .desktop, icon"
install -m 755 "$PKG_DIR/AppRun" "$APPDIR/AppRun"
install -m 644 "$PKG_DIR/customPdfReader.desktop" "$APPDIR/customPdfReader.desktop"
install -m 644 "$PKG_DIR/customPdfReader.desktop" "$APPDIR/usr/share/applications/customPdfReader.desktop"
install -m 644 "$PKG_DIR/customPdfReader.png" "$APPDIR/customPdfReader.png"
install -m 644 "$PKG_DIR/customPdfReader.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/customPdfReader.png"

APPIMAGETOOL="$PKG_DIR/appimagetool-x86_64.AppImage"
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "==> Downloading appimagetool"
    curl -L -o "$APPIMAGETOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$APPIMAGETOOL"
fi

echo "==> Building AppImage"
rm -f "$OUT"
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$OUT"

echo "==> Done: $OUT"
