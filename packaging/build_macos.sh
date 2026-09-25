#!/bin/bash
# Сборка «Pinterest Downloader.app» — самостоятельной программы для macOS (Python внутри).
#
#   ./packaging/build_macos.sh             собрать в dist/
#   ./packaging/build_macos.sh --install   собрать и установить в /Applications
#   ./packaging/build_macos.sh --dmg       собрать и упаковать в dist/Pinterest Downloader.dmg
#
# Python берётся из .venv (или из переменной PYTHON).
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
APP_NAME="Pinterest Downloader"
APP="dist/$APP_NAME.app"

# PyInstaller вызывает lipo. Если Xcode выбран, но его лицензия не принята,
# /usr/bin/lipo не работает — берём инструменты из Command Line Tools.
if ! /usr/bin/xcrun --find lipo >/dev/null 2>&1 && [ -d /Library/Developer/CommandLineTools ]; then
    export DEVELOPER_DIR=/Library/Developer/CommandLineTools
fi

echo "▸ Зависимости"
"$PY" -m pip install -q -r requirements.txt -r packaging/requirements-build.txt

echo "▸ Иконка"
mkdir -p build
"$PY" packaging/make_icon.py build/icon.icns

# Откуда при первом запуске перенести очередь, историю и настройки
"$PY" - "$ROOT" <<'PYEOF'
import datetime, json, sys
json.dump(
    {"source_dir": sys.argv[1], "built": datetime.datetime.now().isoformat(timespec="seconds")},
    open("build/build_info.json", "w", encoding="utf-8"),
)
PYEOF

echo "▸ Сборка приложения"
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
    --distpath dist --workpath build/pyinstaller packaging/PinterestDownloader.spec

echo "▸ Самопроверка сборки"
QT_QPA_PLATFORM=offscreen "$APP/Contents/MacOS/$APP_NAME" --self-test

echo "✓ Готово: $APP ($(du -sh "$APP" | cut -f1))"

for arg in "$@"; do
    case "$arg" in
        --install)
            rm -rf "/Applications/$APP_NAME.app"
            ditto "$APP" "/Applications/$APP_NAME.app"
            echo "✓ Установлено: /Applications/$APP_NAME.app"
            ;;
        --dmg)
            STAGE="build/dmg"
            rm -rf "$STAGE"
            mkdir -p "$STAGE"
            ditto "$APP" "$STAGE/$APP_NAME.app"
            ln -s /Applications "$STAGE/Applications"
            hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "dist/$APP_NAME.dmg"
            echo "✓ Образ: dist/$APP_NAME.dmg"
            ;;
    esac
done
