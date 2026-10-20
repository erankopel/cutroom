#!/usr/bin/env bash
# Assemble a CUTROOM AppDir that appimagetool can turn into one runnable file.
#
#   PYTHON_HOME  a relocatable Python 3.12 install (contains bin/python3.12)
#   FFMPEG_DIR   a folder holding ffmpeg and ffprobe
#   APPDIR       output folder (default: build/linux/CutRoom.AppDir)
#
# The AppDir bundles the interpreter, the Python dependencies from
# requirements.txt, FFmpeg and the application source. No model weights are
# included; they download on first use, as in a source checkout.
set -euo pipefail

: "${PYTHON_HOME:?Set PYTHON_HOME to a Python 3.12 install}"
: "${FFMPEG_DIR:?Set FFMPEG_DIR to a folder containing ffmpeg and ffprobe}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
appdir="${APPDIR:-$repo/build/linux/CutRoom.AppDir}"

[[ -x "$PYTHON_HOME/bin/python3.12" ]] || { echo "No $PYTHON_HOME/bin/python3.12" >&2; exit 1; }
for tool in ffmpeg ffprobe; do
  [[ -x "$FFMPEG_DIR/$tool" ]] || { echo "No executable $FFMPEG_DIR/$tool" >&2; exit 1; }
done

rm -rf "$appdir"
mkdir -p "$appdir/usr/bin" "$appdir/usr/lib/cutroom/scripts" "$appdir/usr/share/doc/cutroom"

echo "Bundling Python from $PYTHON_HOME"
cp -a "$PYTHON_HOME" "$appdir/usr/python"
python="$appdir/usr/python/bin/python3.12"
if command -v uv >/dev/null; then
  uv pip install --python "$python" --break-system-packages --no-cache -r "$repo/requirements.txt"
else
  "$python" -m pip install --break-system-packages --no-cache-dir -r "$repo/requirements.txt"
fi

echo "Bundling FFmpeg from $FFMPEG_DIR"
install -m 755 "$FFMPEG_DIR/ffmpeg" "$FFMPEG_DIR/ffprobe" "$appdir/usr/bin/"
for notice in "$FFMPEG_DIR/../LICENSE.txt" "$FFMPEG_DIR/LICENSE.txt"; do
  if [[ -f "$notice" ]]; then install -m 644 "$notice" "$appdir/usr/share/doc/cutroom/FFMPEG_LICENSE.txt"; break; fi
done

echo "Bundling CUTROOM source"
cp -a "$repo/server.py" "$repo/config.json" "$repo/cutroom" "$repo/web" "$appdir/usr/lib/cutroom/"
install -m 644 "$repo/scripts/preflight.py" "$appdir/usr/lib/cutroom/scripts/"
install -m 644 "$repo/LICENSE" "$repo/docs/THIRD_PARTY_NOTICES.md" "$appdir/usr/share/doc/cutroom/"

# The image is read-only at run time, so compile bytecode now and prune stale caches first.
find "$appdir/usr/lib/cutroom" -name __pycache__ -type d -prune -exec rm -rf {} +
"$python" -m compileall -q "$appdir/usr/lib/cutroom"
"$python" -m compileall -q "$appdir/usr/python/lib" >/dev/null 2>&1 || true

install -m 755 "$here/AppRun" "$appdir/AppRun"
install -m 644 "$here/cutroom.desktop" "$appdir/cutroom.desktop"
install -m 644 "$repo/web/favicon.svg" "$appdir/cutroom.svg"
mkdir -p "$appdir/usr/share/icons/hicolor/scalable/apps"
install -m 644 "$repo/web/favicon.svg" "$appdir/usr/share/icons/hicolor/scalable/apps/cutroom.svg"
ln -sf cutroom.svg "$appdir/.DirIcon"

echo "AppDir ready: $appdir ($(du -sh "$appdir" | cut -f1))"
