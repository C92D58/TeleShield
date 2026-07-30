#!/usr/bin/env bash
set -euo pipefail

# Build a relocatable, minimal Tesseract runtime for the unsigned macOS DMG.
# The output is consumed as PyInstaller data under _MEIPASS/tesseract-runtime.
ROOT="${1:-build/tesseract-runtime}"
rm -rf "$ROOT"
mkdir -p "$ROOT/bin" "$ROOT/lib" "$ROOT/share/tessdata"

TESSERACT_PREFIX="$(brew --prefix tesseract)"
TESSERACT_BIN="$TESSERACT_PREFIX/bin/tesseract"
if [[ ! -x "$TESSERACT_BIN" ]]; then
  echo "Tesseract binary not found: $TESSERACT_BIN" >&2
  exit 1
fi

cp "$TESSERACT_BIN" "$ROOT/bin/tesseract"
chmod 755 "$ROOT/bin/tesseract"

copy_lang() {
  local lang="$1"
  local source=""
  local prefix
  for prefix in "$TESSERACT_PREFIX" "$(brew --prefix tesseract-lang 2>/dev/null || true)" "$(brew --prefix)"; do
    if [[ -f "$prefix/share/tessdata/$lang.traineddata" ]]; then
      source="$prefix/share/tessdata/$lang.traineddata"
      break
    fi
  done
  if [[ -z "$source" ]]; then
    echo "Missing Tesseract language data: $lang.traineddata" >&2
    exit 1
  fi
  cp "$source" "$ROOT/share/tessdata/$lang.traineddata"
}

copy_lang eng
copy_lang chi_sim
copy_lang chi_tra

# dylibbundler follows Homebrew's dependency graph and rewrites load paths to
# the app-local lib directory. System libraries are intentionally not copied.
dylibbundler \
  -od \
  -b \
  -x "$ROOT/bin/tesseract" \
  -d "$ROOT/lib" \
  -p '@loader_path/../lib'

# Confirm the exact runtime we are about to put into the app works before
# PyInstaller starts. TESSDATA_PREFIX points at the bundled language files.
TESSDATA_PREFIX="$PWD/$ROOT/share/tessdata" "$ROOT/bin/tesseract" --list-langs
