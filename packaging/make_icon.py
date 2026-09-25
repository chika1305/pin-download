#!/usr/bin/env python3
"""
Рисует иконку приложения (ту же, что в интерфейсе) во всех размерах macOS
и собирает из них .icns через iconutil.

Запуск:  python packaging/make_icon.py build/icon.icns
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import pinterest_gui_mac as ui  # noqa: E402


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "build" / "icon.icns").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication([])  # noqa: F841 — нужен для QPixmap
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                ui.render_app_icon(size * scale).save(str(iconset / f"icon_{size}x{size}{suffix}.png"))
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    print(f"Иконка: {out}")


if __name__ == "__main__":
    main()
