#!/usr/bin/env bash
# Качает tessdata (rus + kaz) в ~/.local/share/tessdata для OCR сканированных PDF.
# Требует только curl. Tesseract уже должен быть установлен (apt/pacman/brew).
#
# Использование:  bash scripts/setup_ocr.sh

set -euo pipefail

DEST="${ZERDE_TESSDATA:-$HOME/.local/share/tessdata}"
BASE_URL="https://github.com/tesseract-ocr/tessdata/raw/main"

if ! command -v tesseract >/dev/null 2>&1; then
    echo "ERROR: 'tesseract' binary not found. Install it first:"
    echo "  Arch/CachyOS:  sudo pacman -S tesseract"
    echo "  Debian/Ubuntu: sudo apt install tesseract-ocr"
    echo "  macOS:         brew install tesseract"
    exit 1
fi

mkdir -p "$DEST"
echo "tessdata destination: $DEST"

for lang in rus kaz; do
    target="$DEST/$lang.traineddata"
    if [ -s "$target" ]; then
        echo "  [skip] $lang.traineddata already present"
        continue
    fi
    echo "  [get]  $lang.traineddata"
    curl -fL --connect-timeout 30 -o "$target" "$BASE_URL/$lang.traineddata"
done

echo
echo "Done. To enable OCR in a shell session, export:"
echo "  export ZERDE_TESSDATA=\"$DEST\""
echo
echo "(Optional; the pipeline already defaults to $DEST.)"
