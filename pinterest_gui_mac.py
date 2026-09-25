#!/usr/bin/env python3
"""
Pinterest Image Downloader — нативный интерфейс для macOS на Qt (PySide6).

Логика скачивания общая с pinterest_gui.py (через pinterest_download_engine).
Тяжёлые модули (Selenium и т.д.) подгружаются в фоне, поэтому окно открывается сразу.

Запуск:  python pinterest_gui_mac.py
Нужны:  pip install -r requirements.txt  (на Mac подтянется PySide6)
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

try:
    from PySide6.QtCore import (
        QByteArray,
        QEasingCurve,
        QEvent,
        QObject,
        QPoint,
        QPointF,
        QRect,
        QRectF,
        QSize,
        Qt,
        QThread,
        QTimer,
        QUrl,
        QVariantAnimation,
        Signal,
        Slot,
    )
    from PySide6.QtGui import (
        QAction,
        QColor,
        QDesktopServices,
        QFont,
        QFontDatabase,
        QFontMetrics,
        QGuiApplication,
        QIcon,
        QImage,
        QImageReader,
        QKeySequence,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPixmap,
    )
    from PySide6.QtWidgets import (
        QAbstractButton,
        QAbstractItemView,
        QApplication,
        QButtonGroup,
        QDialog,
        QDoubleSpinBox,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QStackedWidget,
        QToolButton,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
        QWidgetAction,
    )
except ImportError as e:
    print(
        "Нужен PySide6:  pip install PySide6\n"
        "На macOS: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from e


APP_DIR = Path(__file__).resolve().parent
APP_NAME = "Pinterest Downloader"
APP_TITLE = "Pinterest Image Downloader"
APP_VERSION = "2.0.0"
# Собранное приложение (.app через PyInstaller): данные живут в Application Support,
# картинки по умолчанию — в «Изображениях»
FROZEN = bool(getattr(sys, "frozen", False))
SAVED_URLS_FILE = Path("saved_urls.json")
UI_SETTINGS_FILE = Path("ui_settings.json")
HISTORY_FILE = Path("download_history.json")
DATA_FILES = ("saved_urls.json", "download_history.json", "timing_stats.json", "ui_settings.json")
DEFAULT_FOLDER = str(Path.home() / "Pictures" / "Pinterest") if FROZEN else "pinterest_images"
KEYCHAIN_SERVICE = os.environ.get("PIN_DOWNLOADER_KEYCHAIN_SERVICE", APP_NAME)
with warnings.catch_warnings():
    # В PySide6 этот флаг делит значение с устаревшим MaximizeUsingFullscreenGeometryHint
    warnings.simplefilter("ignore", DeprecationWarning)
    EXPANDED_CLIENT_AREA = getattr(Qt.WindowType, "ExpandedClientAreaHint", None)
DEFAULT_TEMPLATE = "{index04}_{hash}.jpg"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
SIDEBAR_WIDTH = 272
THUMB_W, THUMB_H = 72, 56
LIMIT_PRESETS = [0, 10, 20, 30, 50, 80, 100, 200, 500]
PINTEREST_URL_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)*(?:pinterest\.com|pin\.it)/[^\s<>\"']*",
    re.IGNORECASE,
)
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


# ---------------------------------------------------------------- утилиты


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    n1 = n % 10
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def extract_pinterest_urls(text: str) -> List[str]:
    urls: List[str] = []
    for m in PINTEREST_URL_RE.findall(text or ""):
        u = m.rstrip(").,;")
        if not u.lower().startswith(("http://", "https://")):
            u = "https://" + u
        if u not in urls:
            urls.append(u)
    return urls


def pretty_url(url: str) -> str:
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = unquote(u.path).rstrip("/")
        return f"{host}{path}" if path else host
    except Exception:
        return url


def url_key(url: str) -> str:
    """Ссылка без схемы, www, параметров и регистра — чтобы сравнивать доски."""
    return pretty_url(url if "://" in url else "https://" + url).lower()


def human_date(value: str) -> str:
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value or ""
    today = datetime.now().date()
    if dt.date() == today:
        return f"Сегодня, {dt:%H:%M}"
    if dt.date() == today - timedelta(days=1):
        return f"Вчера, {dt:%H:%M}"
    year = f" {dt.year}" if dt.year != today.year else ""
    return f"{dt.day} {MONTHS[dt.month - 1]}{year}, {dt:%H:%M}"


def format_speed(bps: float) -> str:
    if bps <= 0:
        return ""
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} МБ/с".replace(".", ",")
    return f"{max(1, round(bps / 1024))} КБ/с"


def display_board(name: Optional[str]) -> Optional[str]:
    if name and "%" in name:
        try:
            return unquote(name)
        except Exception:
            return name
    return name


def scan_board_folder(folder: Path) -> Tuple[Optional[Path], int]:
    try:
        names = sorted(
            e.name
            for e in os.scandir(folder)
            if e.is_file() and Path(e.name).suffix.lower() in IMAGE_EXTS
        )
    except OSError:
        return None, 0
    return (folder / names[0] if names else None), len(names)


def load_cover_thumbnail(path: Path, w: int, h: int) -> Optional[QImage]:
    """Миниатюра w×h с обрезкой по центру (как object-fit: cover)."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        scale = max(w / size.width(), h / size.height())
        reader.setScaledSize(
            QSize(math.ceil(size.width() * scale), math.ceil(size.height() * scale))
        )
    img = reader.read()
    if img.isNull():
        return None
    cw, ch = min(w, img.width()), min(h, img.height())
    return img.copy((img.width() - cw) // 2, (img.height() - ch) // 2, cw, ch)


def _mac_notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    try:

        def esc(s: str) -> str:
            return (
                (s or "")
                .replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", " ")
            )

        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{esc(message)}" with title "{esc(title)}"',
            ],
            capture_output=True,
            timeout=15,
        )
    except OSError:
        pass


def data_dir() -> Path:
    """Где хранить очередь, историю и настройки."""
    override = os.environ.get("PIN_DOWNLOADER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if FROZEN:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return APP_DIR


def mask_email(email: str) -> str:
    name, at, domain = email.partition("@")
    return f"{name[:2]}•••{at}{domain}" if at else f"{email[:2]}•••"


# --- Связка ключей macOS: пароль Pinterest не хранится в файлах приложения


def keychain_get(account: str) -> Optional[str]:
    if sys.platform != "darwin" or not account:
        return None
    try:
        r = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def keychain_set(account: str, password: str) -> bool:
    if sys.platform != "darwin" or not account:
        return False
    try:
        r = subprocess.run(
            ["/usr/bin/security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
             "-a", account, "-l", APP_NAME, "-w", password],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def keychain_delete(account: str) -> None:
    if sys.platform != "darwin" or not account:
        return
    try:
        subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def bundled_build_info() -> Dict[str, Any]:
    base = Path(getattr(sys, "_MEIPASS", APP_DIR))
    try:
        return json.loads((base / "build_info.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def migrate_legacy_data(target: Path, source: Optional[Path] = None) -> List[str]:
    """
    Первый запуск собранного приложения: переносит очередь, историю и настройки из папки
    проекта, где программа запускалась раньше, а логин из .env — в Связку ключей.
    """
    if any((target / name).exists() for name in DATA_FILES):
        return []
    if source is None:
        src = bundled_build_info().get("source_dir")
        source = Path(src) if src else None
    if source is None or not source.is_dir() or source.resolve() == target.resolve():
        return []
    source = source.resolve()
    moved: List[str] = []
    for name in DATA_FILES:
        f = source / name
        if f.is_file():
            shutil.copy2(f, target / name)
            moved.append(name)

    settings_path = target / "ui_settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except Exception:
        settings = {}
    folder = settings.get("download_folder") or (
        "pinterest_images" if (source / "pinterest_images").is_dir() else ""
    )
    if folder and not Path(folder).expanduser().is_absolute():
        # Продолжаем качать в ту же папку, что и раньше
        settings["download_folder"] = str(source / folder)

    env = source / ".env"
    if env.is_file() and not settings.get("pinterest_email"):
        try:
            from dotenv import dotenv_values

            values = dotenv_values(env)
        except Exception:
            values = {}
        email = (values.get("PINTEREST_EMAIL") or values.get("PINTEREST_LOGIN") or "").strip()
        password = (values.get("PINTEREST_PASSWORD") or "").strip()
        if email and password and not email.endswith("@example.com") and keychain_set(email, password):
            settings["pinterest_email"] = email
            moved.append("логин Pinterest → Связка ключей")

    if settings:
        settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return moved


def _mac_hide_window_title(widget: QWidget) -> None:
    """Прячет текст заголовка NSWindow: контент и так уходит под прозрачный titlebar."""
    # winId() — это NSView* только у платформы cocoa (не offscreen/minimal)
    if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        get_obj = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(addr)
        set_long = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(addr)
        window = get_obj(int(widget.winId()), objc.sel_registerName(b"window"))
        if window:
            set_long(window, objc.sel_registerName(b"setTitleVisibility:"), 1)  # NSWindowTitleHidden
    except Exception:
        pass


# ---------------------------------------------------------------- тема


def _c(value: str, alpha: Optional[int] = None) -> QColor:
    color = QColor(value)
    if alpha is not None:
        color.setAlpha(alpha)
    return color


@dataclass
class Theme:
    dark: bool
    sidebar: QColor
    content: QColor
    card: QColor
    card_hover: QColor
    border: QColor
    separator: QColor
    text: QColor
    secondary: QColor
    tertiary: QColor
    field: QColor
    field_border: QColor
    hover: QColor
    pressed: QColor
    control: QColor
    chip: QColor
    segment: QColor
    accent: QColor
    accent_top: QColor
    accent_bottom: QColor
    accent_hover: QColor
    accent_soft: QColor
    row_active: QColor
    on_accent: QColor
    success: QColor
    warning: QColor
    danger: QColor
    track: QColor
    shadow: QColor


LIGHT = Theme(
    dark=False,
    sidebar=_c("#EFEFF3"),
    content=_c("#F8F8FA"),
    card=_c("#FFFFFF"),
    card_hover=_c("#F6F6F8"),
    border=_c("#000000", 18),
    separator=_c("#000000", 16),
    text=_c("#1D1D1F"),
    secondary=_c("#6E6E73"),
    tertiary=_c("#A1A1A6"),
    field=_c("#FFFFFF"),
    field_border=_c("#000000", 28),
    hover=_c("#000000", 11),
    pressed=_c("#000000", 24),
    control=_c("#000000", 16),
    chip=_c("#000000", 12),
    segment=_c("#FFFFFF"),
    accent=_c("#E60023"),
    accent_top=_c("#F2243F"),
    accent_bottom=_c("#D90023"),
    accent_hover=_c("#C8001E"),
    accent_soft=_c("#E60023", 22),
    row_active=_c("#E60023", 18),
    on_accent=_c("#FFFFFF"),
    success=_c("#34C759"),
    warning=_c("#FF9500"),
    danger=_c("#FF3B30"),
    track=_c("#000000", 20),
    shadow=_c("#000000", 9),
)

DARK = Theme(
    dark=True,
    sidebar=_c("#202023"),
    content=_c("#18181A"),
    card=_c("#252528"),
    card_hover=_c("#2D2D31"),
    border=_c("#FFFFFF", 20),
    separator=_c("#FFFFFF", 18),
    text=_c("#F5F5F7"),
    secondary=_c("#A1A1A6"),
    tertiary=_c("#6C6C70"),
    field=_c("#FFFFFF", 12),
    field_border=_c("#FFFFFF", 34),
    hover=_c("#FFFFFF", 16),
    pressed=_c("#FFFFFF", 30),
    control=_c("#FFFFFF", 26),
    chip=_c("#FFFFFF", 20),
    segment=_c("#636366"),
    accent=_c("#FF3B55"),
    accent_top=_c("#FF4D64"),
    accent_bottom=_c("#E8193A"),
    accent_hover=_c("#FF5C71"),
    accent_soft=_c("#FF3B55", 40),
    row_active=_c("#FF3B55", 34),
    on_accent=_c("#FFFFFF"),
    success=_c("#30D158"),
    warning=_c("#FF9F0A"),
    danger=_c("#FF453A"),
    track=_c("#FFFFFF", 28),
    shadow=_c("#000000", 40),
)

THEME = LIGHT


def css(color: QColor) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


QSS_TEMPLATE = """
QWidget#sidebar, QWidget#page, QWidget#scrollBody, QWidget#queueBody,
QStackedWidget#stack { background: transparent; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }

QLabel#appName { font-size: 19px; font-weight: 700; color: @text; }
QLabel#appCaption { font-size: 14px; color: @secondary; }
QLabel#largeTitle { font-size: 34px; font-weight: 700; color: @text; }
QLabel#subtitle { font-size: 15px; color: @secondary; }
QLabel#sectionHeader { font-size: 15px; font-weight: 600; color: @text; }
QLabel#cardTitle { font-size: 17px; font-weight: 700; color: @text; }
QLabel#badge {
    background: @chip; color: @text; border-radius: 11px; padding: 0 8px;
    min-width: 10px; min-height: 22px; max-height: 22px; font-size: 13px; font-weight: 600;
}
QLabel#muted { font-size: 13px; color: @secondary; }
QLabel#fieldLabel { font-size: 14px; color: @secondary; }
QLabel#rowTitle { font-size: 14px; color: @text; }
QLabel#rowTitleStrong { font-size: 16px; font-weight: 700; color: @text; }
QLabel#rowSubtitle { font-size: 13px; color: @secondary; }
QLabel#rowSubtitle[tone="danger"] { color: @danger; }
QLabel#groupSubtitle { font-size: 12px; color: @secondary; }
QLabel#progressText { font-size: 13px; color: @secondary; }
QLabel#progressText[tone="done"] { font-weight: 600; color: @success; }
QLabel#progressText[tone="error"] { font-weight: 600; color: @warning; }
QLabel#statusTitle { font-size: 17px; font-weight: 700; color: @text; }
QLabel#statusText { font-size: 13px; color: @secondary; }
QLabel#timer { font-size: 14px; color: @secondary; }
QLabel#statValue { font-size: 26px; font-weight: 700; color: @text; }
QLabel#statValue[tone="danger"] { color: @danger; }
QLabel#statCaption { font-size: 13px; color: @secondary; }
QLabel#percent { font-size: 13px; color: @secondary; }
QLabel#emptyTitle { font-size: 19px; font-weight: 700; color: @text; }
QLabel#emptyText { font-size: 14px; color: @secondary; }
QLabel#sideTitle { font-size: 13px; font-weight: 600; color: @text; }
QLabel#sideText { font-size: 12px; color: @secondary; }
QLabel#urlIcon { background: @chip; border-radius: 8px; }

QFrame#card { background: @card; border: 1px solid @border; border-radius: 14px; }
QDialog#account { background: @content; }
QFrame#sideCard { background: @card; border: 1px solid @border; border-radius: 12px; }
QFrame#folderChip { background: @card; border: 1px solid @border; border-radius: 12px; }
QFrame#folderChip:hover { background: @card_hover; }
QFrame#urlBox { background: @field; border: 1px solid @field_border; border-radius: 12px; }
QFrame#urlBox[focused="true"] { border: 2px solid @accent; }
QLineEdit#urlInput {
    background: transparent; border: none; padding: 0; font-size: 15px; color: @text;
}

QPushButton#primary {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1, stop: 0 @accent_top, stop: 1 @accent_bottom);
    color: @on_accent; border: none; border-radius: 10px; padding: 0 18px;
    min-height: 40px; font-size: 14px; font-weight: 600;
}
QPushButton#primary:hover { background: @accent_hover; }
QPushButton#primary:pressed { background: @accent_bottom; }
QPushButton#primary:disabled { background: @control; color: @tertiary; }
QPushButton#secondary {
    background: @control; color: @text; border: none; border-radius: 10px; padding: 0 18px;
    min-height: 40px; font-size: 14px; font-weight: 500;
}
QPushButton#secondary:hover { background: @pressed; }
QPushButton#secondary:disabled { color: @tertiary; }
QPushButton#outline, QPushButton#destructive {
    background: @card; color: @text; border: 1px solid @field_border; border-radius: 9px;
    padding: 0 14px; min-height: 32px; font-size: 13px;
}
QPushButton#destructive { color: @danger; }
QPushButton#outline:hover, QPushButton#destructive:hover { background: @card_hover; }
QPushButton#outline:disabled, QPushButton#destructive:disabled { color: @tertiary; }
QPushButton#primary[size="large"] { min-height: 46px; border-radius: 12px; font-size: 15px; }
QPushButton#outline[size="large"] { min-height: 40px; border-radius: 10px; font-size: 14px; padding: 0 16px; }
QPushButton#chip {
    background: @chip; color: @text; border: none; border-radius: 9px;
    padding: 0 12px; min-height: 30px; font-size: 13px;
}
QPushButton#chip:hover { background: @pressed; }
QPushButton#link {
    background: transparent; border: none; color: @accent; font-size: 13px; padding: 2px 4px;
}
QPushButton#link:hover { color: @accent_hover; }
QPushButton#link:disabled { color: @tertiary; }
QToolButton#square {
    background: @card; border: 1px solid @field_border; border-radius: 9px;
}
QToolButton#square:hover { background: @card_hover; }
QToolButton#iconButton { background: transparent; border: none; border-radius: 14px; }
QToolButton#iconButton:hover { background: @hover; }

QLineEdit#field {
    background: @field; border: 1px solid @field_border; border-radius: 9px;
    padding: 5px 10px; font-size: 14px; color: @text;
}
QLineEdit#field:focus { border: 2px solid @accent; padding: 4px 9px; }

QProgressBar { background: @track; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: @accent; border-radius: 4px; }
QProgressBar#thin, QProgressBar#row { border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar#thin::chunk, QProgressBar#row::chunk { border-radius: 3px; }
QProgressBar#row[tone="done"]::chunk { background: @accent; }

QPlainTextEdit#log {
    background: @card; border: 1px solid @border; border-radius: 14px;
    padding: 12px; color: @text; font-size: 12px;
}
QTreeWidget#history {
    background: @card; border: 1px solid @border; border-radius: 14px;
    padding: 6px; color: @text; font-size: 14px; outline: 0;
}
QTreeWidget#history::item { min-height: 38px; border: none; padding: 0 8px; }
QTreeWidget#history::item:hover { background: @hover; }
QTreeWidget#history::item:selected { background: @accent; color: @on_accent; }
QTreeWidget#history QHeaderView::section {
    background: transparent; color: @secondary; border: none;
    border-bottom: 1px solid @separator; padding: 6px 8px 8px 8px;
    font-size: 12px; font-weight: 600;
}

QMenu#popup {
    background: @card; border: 1px solid @border; border-radius: 12px; padding: 6px;
}
QMenu#popup::item {
    padding: 7px 16px 7px 8px; border-radius: 7px; color: @text; font-size: 13px;
}
QMenu#popup::item:selected { background: @hover; }
QMenu#popup::item:disabled { color: @tertiary; }
QMenu#popup::icon { padding-left: 10px; }
QMenu#popup::separator { height: 1px; background: @separator; margin: 5px 8px; }
QPushButton#menuDanger {
    text-align: left; background: transparent; border: none; border-radius: 7px;
    padding: 7px 16px 7px 12px; color: @danger; font-size: 13px;
}
QPushButton#menuDanger:hover { background: @accent_soft; }
QPushButton#menuDanger:disabled { color: @tertiary; }
"""


def build_qss(t: Theme) -> str:
    tokens = {f.name: getattr(t, f.name) for f in fields(t) if isinstance(getattr(t, f.name), QColor)}
    qss = QSS_TEMPLATE
    # Длинные имена первыми, чтобы @accent не съел @accent_hover
    for name in sorted(tokens, key=len, reverse=True):
        qss = qss.replace(f"@{name}", css(tokens[name]))
    return qss


def blend(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
        round(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def set_tone(widget: QWidget, tone: str) -> None:
    if widget.property("tone") != tone:
        widget.setProperty("tone", tone)
        repolish(widget)


# ---------------------------------------------------------------- иконки и глифы

_symbol_cache: Dict[tuple, QPixmap] = {}


def _dpr() -> float:
    app = QGuiApplication.instance()
    return app.devicePixelRatio() if app else 2.0


def symbol_pixmap(name: str, color: QColor, size: int) -> QPixmap:
    """SF Symbol нужного цвета в квадратном холсте size×size (пусто, если символа нет)."""
    dpr = _dpr()
    key = (name, color.rgba(), size, dpr)
    cached = _symbol_cache.get(key)
    if cached is not None:
        return cached
    base = QIcon.fromTheme(name)
    if base.isNull():
        out = QPixmap()
    else:
        src = base.pixmap(QSize(size, size), dpr)
        out = QPixmap(round(size * dpr), round(size * dpr))
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        s = src.deviceIndependentSize()
        p.drawPixmap(QPointF((size - s.width()) / 2, (size - s.height()) / 2), src)
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(QRectF(0, 0, size, size), color)
        p.end()
    _symbol_cache[key] = out
    return out


def symbol_icon(
    name: str, color: QColor, size: int = 16, on_color: Optional[QColor] = None
) -> QIcon:
    pm = symbol_pixmap(name, color, size)
    if pm.isNull():
        return QIcon()
    icon = QIcon()
    icon.addPixmap(pm, QIcon.Normal, QIcon.Off)
    icon.addPixmap(symbol_pixmap(name, THEME.tertiary, size), QIcon.Disabled, QIcon.Off)
    if on_color is not None:
        icon.addPixmap(symbol_pixmap(name, on_color, size), QIcon.Normal, QIcon.On)
    return icon


def draw_glyph(p: QPainter, kind: str, r: QRectF, color: QColor) -> None:
    """Жирные глифы для иконки приложения и круглого значка статуса."""
    w, h, x0, y0 = r.width(), r.height(), r.left(), r.top()
    cx = x0 + w / 2
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(color, w * 0.085, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if kind == "download":
        p.drawLine(QPointF(cx, y0 + h * 0.22), QPointF(cx, y0 + h * 0.58))
        arrow = QPainterPath(QPointF(cx - w * 0.15, y0 + h * 0.44))
        arrow.lineTo(cx, y0 + h * 0.59)
        arrow.lineTo(cx + w * 0.15, y0 + h * 0.44)
        p.drawPath(arrow)
        tray = QPainterPath(QPointF(x0 + w * 0.25, y0 + h * 0.62))
        tray.lineTo(x0 + w * 0.25, y0 + h * 0.76)
        tray.lineTo(x0 + w * 0.75, y0 + h * 0.76)
        tray.lineTo(x0 + w * 0.75, y0 + h * 0.62)
        p.drawPath(tray)
    elif kind == "check":
        path = QPainterPath(QPointF(x0 + w * 0.28, y0 + h * 0.52))
        path.lineTo(x0 + w * 0.44, y0 + h * 0.67)
        path.lineTo(x0 + w * 0.73, y0 + h * 0.35)
        p.drawPath(path)
    elif kind == "pause":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        bw = w * 0.12
        p.drawRoundedRect(QRectF(cx - bw * 1.6, y0 + h * 0.3, bw, h * 0.4), bw / 2, bw / 2)
        p.drawRoundedRect(QRectF(cx + bw * 0.6, y0 + h * 0.3, bw, h * 0.4), bw / 2, bw / 2)
    elif kind == "stop":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        s = w * 0.34
        p.drawRoundedRect(QRectF(cx - s / 2, y0 + h / 2 - s / 2, s, s), s * 0.2, s * 0.2)
    elif kind == "error":
        p.drawLine(QPointF(cx, y0 + h * 0.3), QPointF(cx, y0 + h * 0.56))
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        d = w * 0.1
        p.drawEllipse(QRectF(cx - d / 2, y0 + h * 0.66, d, d))
    elif kind == "sparkles":
        pm = symbol_pixmap("sparkles", color, int(w * 0.5))
        if not pm.isNull():
            p.drawPixmap(QPointF(cx - w * 0.25, y0 + h * 0.25), pm)
    p.restore()


def render_app_icon(size: int, margin: bool = True) -> QPixmap:
    """Иконка приложения: красный «squircle» со стрелкой загрузки в лоток."""
    dpr = _dpr()
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    inset = size * 0.1 if margin else 0.0
    r = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    radius = r.width() * 0.225
    if margin:
        shadow = QPainterPath()
        shadow.addRoundedRect(r.translated(0, size * 0.012), radius, radius)
        p.fillPath(shadow, QColor(0, 0, 0, 60))
    body = QPainterPath()
    body.addRoundedRect(r, radius, radius)
    grad = QLinearGradient(r.topLeft(), r.bottomLeft())
    grad.setColorAt(0.0, QColor("#FF5A6B"))
    grad.setColorAt(1.0, QColor("#D1001F"))
    p.fillPath(body, grad)
    shine = QLinearGradient(r.topLeft(), QPointF(r.left(), r.center().y()))
    shine.setColorAt(0.0, QColor(255, 255, 255, 60))
    shine.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(body, shine)
    draw_glyph(p, "download", r, QColor("white"))
    p.end()
    return pm


def make_app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 32, 64, 128, 256, 512):
        icon.addPixmap(render_app_icon(size))
    return icon


def make_menu(parent: QWidget) -> QMenu:
    """Всплывающее меню со скруглёнными углами в стиле приложения."""
    menu = QMenu(parent)
    menu.setObjectName("popup")
    menu.setAttribute(Qt.WA_TranslucentBackground, True)
    menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint)
    return menu


def menu_item(
    menu: QMenu, text: str, symbol: str, slot: Callable[[], Any], enabled: bool = True
) -> QAction:
    action = menu.addAction(symbol_icon(symbol, THEME.secondary, 16), text)
    action.triggered.connect(lambda _=False: slot())
    action.setEnabled(enabled)
    return action


def menu_danger(
    menu: QMenu, text: str, symbol: str, slot: Callable[[], Any], enabled: bool = True
) -> None:
    """Красный пункт меню (обычный QAction нельзя перекрасить отдельно от остальных)."""
    btn = QPushButton(symbol_icon(symbol, THEME.danger, 16), text)
    btn.setObjectName("menuDanger")
    btn.setIconSize(QSize(16, 16))
    btn.setEnabled(enabled)

    def fire() -> None:
        menu.close()
        QTimer.singleShot(0, slot)

    btn.clicked.connect(fire)
    wa = QWidgetAction(menu)
    wa.setDefaultWidget(btn)
    menu.addAction(wa)


# ---------------------------------------------------------------- виджеты


class ToggleSwitch(QAbstractButton):
    """Переключатель в стиле macOS/iOS."""

    def __init__(self, checked: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self._pos = 1.0 if checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return QSize(44, 26)

    def _animate(self, on: bool) -> None:
        target = 1.0 if on else 0.0
        self._anim.stop()
        if not self.isVisible():
            self._pos = target
            self.update()
            return
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(target)
        self._anim.start()

    def _on_anim(self, value: Any) -> None:
        self._pos = float(value)
        self.update()

    def hitButton(self, pos) -> bool:  # type: ignore[override]
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        track = QRectF(0, (self.height() - 24) / 2, 42, 24)
        p.setPen(Qt.NoPen)
        p.setBrush(blend(THEME.track, THEME.accent, self._pos))
        p.drawRoundedRect(track, 12, 12)
        d = 20.0
        x = track.left() + 2 + self._pos * (track.width() - d - 4)
        p.setBrush(QColor(0, 0, 0, 45))
        p.drawEllipse(QRectF(x, track.top() + 2.6, d, d))
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, track.top() + 2, d, d))


class SegmentedControl(QWidget):
    """Сегментированный переключатель (как NSSegmentedControl) с анимацией выделения."""

    changed = Signal(object)

    def __init__(
        self, options: List[Tuple[Any, str]], value: Any = None, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._options = options
        self._index = 0
        self._anim_pos = 0.0
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        f = self.font()
        f.setPixelSize(13)
        self.setFont(f)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        if value is not None:
            self.setValue(value)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        seg = max(max(fm.horizontalAdvance(label) for _, label in self._options) + 30, 56)
        return QSize(seg * len(self._options) + 4, 32)

    def value(self) -> Any:
        return self._options[self._index][0]

    def setValue(self, value: Any) -> None:
        for i, (v, _) in enumerate(self._options):
            if v == value:
                self._set_index(i, animate=False, emit=False)
                return

    def _set_index(self, index: int, animate: bool = True, emit: bool = True) -> None:
        index = max(0, min(len(self._options) - 1, index))
        changed = index != self._index
        self._index = index
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._anim_pos)
            self._anim.setEndValue(float(index))
            self._anim.start()
        else:
            self._anim_pos = float(index)
            self.update()
        if changed and emit:
            self.changed.emit(self.value())

    def _on_anim(self, value: Any) -> None:
        self._anim_pos = float(value)
        self.update()

    def _segment_width(self) -> float:
        return (self.width() - 4) / len(self._options)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._set_index(int((event.position().x() - 2) // self._segment_width()))

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Left:
            self._set_index(self._index - 1)
        elif event.key() == Qt.Key_Right:
            self._set_index(self._index + 1)
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(THEME.control)
        p.drawRoundedRect(r, 9, 9)
        seg_w = self._segment_width()
        sel = QRectF(2 + self._anim_pos * seg_w, 2, seg_w, r.height() - 4)
        if not THEME.dark:
            p.setBrush(QColor(0, 0, 0, 26))
            p.drawRoundedRect(sel.translated(0, 0.6), 7, 7)
        p.setBrush(THEME.segment)
        p.drawRoundedRect(sel, 7, 7)
        for i, (_, label) in enumerate(self._options):
            seg = QRectF(2 + i * seg_w, 0, seg_w, r.height())
            weight = max(0.0, 1.0 - abs(self._anim_pos - i))
            f = self.font()
            f.setWeight(QFont.DemiBold if weight > 0.5 else QFont.Normal)
            p.setFont(f)
            p.setPen(blend(THEME.secondary, THEME.text, weight))
            p.drawText(seg, Qt.AlignCenter, label)


class LimitPicker(QAbstractButton):
    """Выбор лимита картинок: кнопка-«поп-ап» с готовыми значениями и «Другое…»."""

    changed = Signal(int)

    def __init__(self, value: int = 0, compact: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._value = int(value)
        self._compact = compact
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAttribute(Qt.WA_Hover, True)
        f = self.font()
        f.setPixelSize(14 if compact else 15)
        self.setFont(f)
        self.setFixedSize(self.sizeHint())
        self.clicked.connect(self._open_menu)

    @staticmethod
    def text_for(value: int) -> str:
        return "Все" if value <= 0 else str(value)

    def sizeHint(self) -> QSize:
        return QSize(96, 34) if self._compact else QSize(112, 46)

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:
        self._value = max(0, int(value))
        self.update()

    def _set(self, value: int) -> None:
        value = max(0, int(value))
        if value != self._value:
            self._value = value
            self.update()
            self.changed.emit(value)

    def _open_menu(self) -> None:
        menu = make_menu(self)
        values = sorted(set(LIMIT_PRESETS) | {self._value}, key=lambda v: (v != 0, v))
        for v in values:
            icon = symbol_icon("checkmark", THEME.accent, 14) if v == self._value else QIcon()
            action = menu.addAction(icon, self.text_for(v))
            action.triggered.connect(lambda _=False, v=v: self._set(v))
        menu.addSeparator()
        menu_item(menu, "Другое…", "pencil", self._ask)
        menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))

    def _ask(self) -> None:
        v, ok = QInputDialog.getInt(
            self.window(), "Лимит", "Сколько изображений скачать (0 — все):", self._value, 0, 100000, 1
        )
        if ok:
            self._set(v)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key_Up, Qt.Key_Down):
            presets = sorted(set(LIMIT_PRESETS) | {self._value})
            i = presets.index(self._value) + (1 if event.key() == Qt.Key_Up else -1)
            self._set(presets[max(0, min(len(presets) - 1, i))])
        elif event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._open_menu()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.5)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 9 if self._compact else 12
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.fillPath(path, THEME.field)
        if self.underMouse() and self.isEnabled():
            p.fillPath(path, THEME.hover)
        border = THEME.accent if self.hasFocus() else THEME.field_border
        p.setPen(QPen(border, 2 if self.hasFocus() else 1))
        p.drawPath(path)
        p.setPen(THEME.text)
        p.setFont(self.font())
        p.drawText(r.adjusted(12, 0, -28, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text_for(self._value))
        symbol = "chevron.down" if self._compact else "chevron.up.chevron.down"
        size = 12 if self._compact else 14
        pm = symbol_pixmap(symbol, THEME.secondary, size)
        if not pm.isNull():
            p.drawPixmap(QPointF(r.right() - 12 - size, r.center().y() - size / 2), pm)


class SidebarItem(QAbstractButton):
    """Пункт боковой панели: иконка, название, счётчик и подсказка сочетания клавиш."""

    def __init__(self, title: str, symbol: str, shortcut: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setText(title)
        self._symbol = symbol
        self._shortcut = shortcut
        self._badge = ""
        self.setFixedHeight(46)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        f = self.font()
        f.setPixelSize(15)
        self.setFont(f)

    def set_badge(self, text: str) -> None:
        if text != self._badge:
            self._badge = text
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(220, 46)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        on = self.isChecked()
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        if on:
            grad = QLinearGradient(r.topLeft(), r.bottomLeft())
            grad.setColorAt(0.0, THEME.accent_top)
            grad.setColorAt(1.0, THEME.accent_bottom)
            p.fillPath(path, grad)
        elif self.underMouse():
            p.fillPath(path, THEME.hover)

        icon = symbol_pixmap(self._symbol, THEME.on_accent if on else THEME.accent, 20)
        if not icon.isNull():
            p.drawPixmap(QPointF(16, (r.height() - 20) / 2), icon)

        right = r.width() - 14
        sf = QFont(self.font())
        sf.setPixelSize(12)
        sw = QFontMetrics(sf).horizontalAdvance(self._shortcut)
        p.setFont(sf)
        p.setPen(QColor(255, 255, 255, 190) if on else THEME.tertiary)
        p.drawText(QRectF(right - sw, 0, sw, r.height()), Qt.AlignVCenter | Qt.AlignRight, self._shortcut)
        right -= sw + 12

        if self._badge:
            bf = QFont(self.font())
            bf.setPixelSize(12)
            bf.setWeight(QFont.DemiBold)
            bw = max(24.0, QFontMetrics(bf).horizontalAdvance(self._badge) + 14.0)
            br = QRectF(right - bw, (r.height() - 22) / 2, bw, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 64) if on else THEME.chip)
            p.drawRoundedRect(br, 11, 11)
            p.setFont(bf)
            p.setPen(THEME.on_accent if on else THEME.secondary)
            p.drawText(br, Qt.AlignCenter, self._badge)
            right = br.left() - 8

        p.setFont(self.font())
        p.setPen(THEME.on_accent if on else THEME.text)
        x = 16 + 20 + 14
        text = QFontMetrics(self.font()).elidedText(self.text(), Qt.ElideRight, int(right - x))
        p.drawText(QRectF(x, 0, right - x, r.height()), Qt.AlignVCenter | Qt.AlignLeft, text)


class StatusBadge(QWidget):
    """Круглый значок состояния загрузки."""

    def __init__(self, size: int = 48, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._glyph = "download"
        self._tone = "idle"

    def set_status(self, glyph: str, tone: str) -> None:
        self._glyph, self._tone = glyph, tone
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fg = THEME.on_accent
        if self._tone == "active":
            brush: Any = QLinearGradient(r.topLeft(), r.bottomLeft())
            brush.setColorAt(0.0, THEME.accent_top)
            brush.setColorAt(1.0, THEME.accent_bottom)
        elif self._tone == "paused":
            brush = THEME.warning
        elif self._tone == "done":
            brush = THEME.success
        elif self._tone == "error":
            brush = THEME.danger
        else:
            brush = THEME.control
            fg = THEME.secondary
        p.setPen(Qt.NoPen)
        p.setBrush(brush)
        p.drawEllipse(r)
        draw_glyph(p, self._glyph, r, fg)


class Separator(QWidget):
    def __init__(self, inset: int = 0, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._inset = inset
        self.setFixedHeight(1)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        QPainter(self).fillRect(QRect(self._inset, 0, self.width() - self._inset, 1), THEME.separator)


class ElidedLabel(QLabel):
    """Однострочная подпись, которая сокращается многоточием вместо растягивания окна."""

    def __init__(self, text: str = "", mode=Qt.ElideMiddle, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self._refresh()

    def setFullText(self, text: str) -> None:
        self._full = text
        self._refresh()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh()

    def changeEvent(self, event) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self._refresh()

    def _refresh(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full, self._mode, max(0, self.width())))


class ClickableFrame(QFrame):
    clicked = Signal()

    def __init__(self, name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName(name)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class UrlBox(QFrame):
    """Поле ссылки: иконка в плашке, ввод, кнопка «из буфера» и очистка."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("urlBox")
        self.setFixedHeight(46)
        h = QHBoxLayout(self)
        h.setContentsMargins(7, 0, 8, 0)
        h.setSpacing(10)
        self.icon = QLabel()
        self.icon.setObjectName("urlIcon")
        self.icon.setFixedSize(32, 32)
        self.icon.setAlignment(Qt.AlignCenter)
        h.addWidget(self.icon)
        self.edit = QLineEdit()
        self.edit.setObjectName("urlInput")
        self.edit.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.edit.setPlaceholderText("Вставьте ссылку на доску Pinterest или pin.it…")
        self.edit.installEventFilter(self)
        h.addWidget(self.edit, 1)
        self.btn_paste = QToolButton()
        self.btn_paste.setObjectName("square")
        self.btn_paste.setFixedSize(32, 32)
        self.btn_paste.setIconSize(QSize(16, 16))
        self.btn_paste.setCursor(Qt.PointingHandCursor)
        self.btn_paste.setToolTip("Добавить ссылки из буфера обмена")
        h.addWidget(self.btn_paste)
        self.btn_clear = QToolButton()
        self.btn_clear.setObjectName("iconButton")
        self.btn_clear.setFixedSize(28, 28)
        self.btn_clear.setIconSize(QSize(14, 14))
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setToolTip("Очистить поле")
        self.btn_clear.clicked.connect(self.edit.clear)
        h.addWidget(self.btn_clear)
        self.retheme()

    def retheme(self) -> None:
        self.icon.setPixmap(symbol_pixmap("link", THEME.secondary, 16))
        self.btn_paste.setIcon(symbol_icon("doc.on.clipboard", THEME.text, 16))
        self.btn_clear.setIcon(symbol_icon("xmark", THEME.secondary, 14))

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is self.edit and event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.setProperty("focused", event.type() == QEvent.FocusIn)
            repolish(self)
        return super().eventFilter(obj, event)


class Thumbnail(QWidget):
    def __init__(self, w: int, h: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(w, h)
        self._pm: Optional[QPixmap] = None

    def set_image(self, image: Optional[QImage]) -> None:
        self._pm = QPixmap.fromImage(image) if image is not None and not image.isNull() else None
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        if self._pm is not None:
            p.setClipPath(path)
            p.drawPixmap(self.rect(), self._pm)
            p.setClipping(False)
        else:
            p.fillPath(path, THEME.chip)
            icon = symbol_pixmap("photo", THEME.tertiary, 22)
            if not icon.isNull():
                p.drawPixmap(QPointF((self.width() - 22) / 2, (self.height() - 22) / 2), icon)
        p.setPen(QPen(THEME.border, 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


class DragHandle(QWidget):
    """Шесть точек слева от строки — потянуть, чтобы поменять порядок досок."""

    pressed = Signal()
    moved = Signal(QPoint)
    released = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 40)
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("Потяните, чтобы изменить порядок")

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self.setCursor(Qt.ClosedHandCursor)
            self.pressed.emit()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.buttons() & Qt.LeftButton:
            self.moved.emit(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self.setCursor(Qt.OpenHandCursor)
            self.released.emit()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(THEME.tertiary)
        cy = self.height() / 2
        for col in (5.0, 11.0):
            for row in (-6.0, 0.0, 6.0):
                p.drawEllipse(QPointF(col, cy + row), 1.6, 1.6)


class Group(QFrame):
    """Сгруппированная карточка настроек как в «Системных настройках»."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self._rows = 0

    def add_row(
        self, title: str, control: Optional[QWidget] = None, subtitle: Optional[str] = None
    ) -> Tuple[QWidget, QLabel, Optional[QLabel]]:
        if self._rows:
            self._lay.addWidget(Separator(inset=16))
        row = QWidget()
        row.setMinimumHeight(52)
        h = QHBoxLayout(row)
        h.setContentsMargins(16, 11, 16, 11)
        h.setSpacing(16)
        col = QVBoxLayout()
        col.setSpacing(2)
        lt = QLabel(title)
        lt.setObjectName("rowTitle")
        col.addWidget(lt)
        ls = None
        if subtitle is not None:
            ls = QLabel(subtitle)
            ls.setObjectName("groupSubtitle")
            ls.setWordWrap(True)
            col.addWidget(ls)
        h.addLayout(col, 1)
        if control is not None:
            h.addWidget(control, 0, Qt.AlignRight | Qt.AlignVCenter)
        self._lay.addWidget(row)
        self._rows += 1
        return row, lt, ls


class AccountDialog(QDialog):
    """Вход в Pinterest: логин и пароль (пароль уходит в Связку ключей macOS)."""

    def __init__(self, parent: QWidget, email: str = "") -> None:
        super().__init__(parent)
        self.setObjectName("account")
        self.setWindowTitle("Аккаунт Pinterest")
        self.setModal(True)
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 22)
        v.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(render_app_icon(48, margin=False))
        head.addWidget(logo, 0, Qt.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(4)
        title = QLabel("Вход в Pinterest")
        title.setObjectName("cardTitle")
        text.addWidget(title)
        hint = QLabel(
            "Нужен, чтобы Pinterest не закрывал доску окном входа. Пароль хранится "
            "в Связке ключей macOS и используется только для входа на pinterest.com."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        text.addWidget(hint)
        head.addLayout(text, 1)
        v.addLayout(head)
        v.addSpacing(4)
        self.ed_email = QLineEdit(email)
        self.ed_email.setObjectName("field")
        self.ed_email.setPlaceholderText("Email или имя пользователя")
        self.ed_email.setMinimumHeight(38)
        self.ed_email.setAttribute(Qt.WA_MacShowFocusRect, False)
        v.addWidget(self.ed_email)
        self.ed_password = QLineEdit()
        self.ed_password.setObjectName("field")
        self.ed_password.setPlaceholderText("Пароль")
        self.ed_password.setEchoMode(QLineEdit.Password)
        self.ed_password.setMinimumHeight(38)
        self.ed_password.setAttribute(Qt.WA_MacShowFocusRect, False)
        v.addWidget(self.ed_password)
        v.addSpacing(6)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setObjectName("outline")
        cancel.setProperty("size", "large")
        cancel.clicked.connect(self.reject)
        self.btn_save = QPushButton("Сохранить")
        self.btn_save.setObjectName("primary")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.btn_save)
        v.addLayout(buttons)
        for ed in (self.ed_email, self.ed_password):
            ed.textChanged.connect(self._validate)
        self._validate()
        (self.ed_password if email else self.ed_email).setFocus()

    def _validate(self) -> None:
        self.btn_save.setEnabled(bool(self.ed_email.text().strip() and self.ed_password.text()))

    def values(self) -> Tuple[str, str]:
        return self.ed_email.text().strip(), self.ed_password.text()


class RowProgress(QWidget):
    """Прогресс доски в строке очереди: полоса, «29/50» и скорость или итог."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(230)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.bar = QProgressBar()
        self.bar.setObjectName("row")
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1)
        v.addWidget(self.bar)
        h = QHBoxLayout()
        h.setSpacing(6)
        self.left = QLabel()
        self.left.setObjectName("progressText")
        h.addWidget(self.left)
        h.addStretch()
        self.icon = QLabel()
        self.icon.setFixedSize(16, 16)
        h.addWidget(self.icon)
        self.right = QLabel()
        self.right.setObjectName("progressText")
        h.addWidget(self.right)
        v.addLayout(h)

    def show_state(self, state: str, done: int, total: int, speed: float, limit: int, paused: bool) -> None:
        total_text = str(total) if total else (str(limit) if limit else "")
        counter = f"{done}/{total_text}" if total_text else str(done)
        icon: Optional[Tuple[str, QColor]] = None
        tone, right, bar_visible = "", "", True
        if state == "idle":
            bar_visible, counter = False, ""
        elif state == "queued":
            counter = f"0/{total_text}" if total_text else "Ожидает"
        elif state == "running":
            if not total:
                counter = "Ищу пины…"
            right = "Пауза" if paused else format_speed(speed)
        elif state == "done":
            icon, tone, right = ("checkmark.circle.fill", THEME.success), "done", "Готово"
        elif state == "error":
            icon, tone, right = ("exclamationmark.triangle", THEME.warning), "error", "Ошибка"
            bar_visible = bool(total)
            counter = f"{done}/{total}" if total else ""
        elif state == "stopped":
            right = "Остановлено"
        self.bar.setVisible(bar_visible)
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(min(done, total) if state != "done" else max(1, total))
        self.left.setText(counter)
        self.right.setText(right)
        set_tone(self.right, tone)
        if icon:
            self.icon.setPixmap(symbol_pixmap(icon[0], icon[1], 16))
            self.icon.show()
        else:
            self.icon.hide()


class QueueRow(QWidget):
    remove_clicked = Signal(object)
    limit_changed = Signal(object)
    action_clicked = Signal(object)
    context_requested = Signal(object, object)
    drag_started = Signal(object)
    drag_moved = Signal(object, QPoint)
    drag_finished = Signal(object)

    ACTIONS = {
        "idle": ("play.fill", "Скачать только эту доску"),
        "queued": ("play.fill", "Скачать следующей"),
        "done": ("ellipsis", "Ещё"),
        "error": ("arrow.clockwise", "Повторить"),
        "stopped": ("arrow.clockwise", "Докачать"),
    }

    def __init__(self, data: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.data = data
        self.state = "idle"
        self.first = True
        self.paused = False
        self.dragging = False
        self.image_count = 0
        self.done = 0
        self.total = 0
        self.speed = 0.0
        self.lookup_done = bool(data.get("board_name"))
        self.setMinimumHeight(78)
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 10, 16, 10)
        h.setSpacing(12)
        self.handle = DragHandle()
        self.handle.pressed.connect(lambda: self.drag_started.emit(self))
        self.handle.moved.connect(lambda pos: self.drag_moved.emit(self, pos))
        self.handle.released.connect(lambda: self.drag_finished.emit(self))
        h.addWidget(self.handle)
        self.thumb = Thumbnail(THUMB_W, THUMB_H)
        h.addWidget(self.thumb)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.lbl_name = ElidedLabel(mode=Qt.ElideRight)
        self.lbl_name.setObjectName("rowTitleStrong")
        self.lbl_sub = ElidedLabel()
        self.lbl_sub.setObjectName("rowSubtitle")
        col.addWidget(self.lbl_name)
        col.addWidget(self.lbl_sub)
        h.addLayout(col, 1)
        h.addSpacing(8)
        self.limit = LimitPicker(int(data.get("max_images") or 0), compact=True)
        self.limit.setToolTip("Сколько изображений скачать с этой доски")
        self.limit.changed.connect(self._on_limit)
        h.addWidget(self.limit)
        h.addSpacing(10)
        self.progress = RowProgress()
        h.addWidget(self.progress)
        h.addSpacing(6)
        self.btn_action = QToolButton()
        self.btn_action.setObjectName("square")
        self.btn_action.setFixedSize(36, 36)
        self.btn_action.setIconSize(QSize(16, 16))
        self.btn_action.setCursor(Qt.PointingHandCursor)
        self.btn_action.clicked.connect(lambda: self.action_clicked.emit(self))
        h.addWidget(self.btn_action)
        self.btn_remove = QToolButton()
        self.btn_remove.setObjectName("iconButton")
        self.btn_remove.setFixedSize(28, 28)
        self.btn_remove.setIconSize(QSize(13, 13))
        self.btn_remove.setCursor(Qt.PointingHandCursor)
        self.btn_remove.setToolTip("Убрать из очереди")
        self.btn_remove.clicked.connect(lambda: self.remove_clicked.emit(self))
        h.addWidget(self.btn_remove)
        self.refresh()
        self.retheme()

    @property
    def key(self) -> str:
        return self.data["url"]

    def job(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "url": self.data["url"],
            "board_name": self.data.get("board_name"),
            "max_images": int(self.data.get("max_images") or 0),
        }

    def _on_limit(self, value: int) -> None:
        self.data["max_images"] = int(value)
        self.refresh()
        self.limit_changed.emit(self)

    def set_first(self, first: bool) -> None:
        self.first = first
        self.update()

    def set_state(self, state: str) -> None:
        self.state = state
        if state in ("queued", "idle"):
            self.done = self.total = 0
            self.speed = 0.0
        self.paused = False
        self.btn_remove.setEnabled(state != "running")
        self.refresh()
        self.retheme()

    def set_progress(self, done: int, total: int, speed: float) -> None:
        self.done, self.total, self.speed = done, total, speed
        self.refresh()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.refresh()
        self.retheme()

    def set_image_count(self, count: int) -> None:
        self.image_count = count
        self.refresh()

    def set_dragging(self, dragging: bool) -> None:
        self.dragging = dragging
        self.update()

    def refresh(self) -> None:
        name = display_board(self.data.get("board_name"))
        if not name:
            fallback = pretty_url(self.data["url"]).split("/")[-1] or "Без названия"
            name = fallback if self.lookup_done else "Определяю название…"
        self.lbl_name.setFullText(name)
        status = {
            "running": "Скачивается…",
            "queued": "В очереди",
            "error": "Ошибка",
            "stopped": "Остановлено",
        }.get(self.state, "")
        if not status and self.image_count:
            status = f"{self.image_count} {plural(self.image_count, 'файл', 'файла', 'файлов')} в папке"
        parts = [pretty_url(self.data["url"])] + ([status] if status else [])
        self.lbl_sub.setFullText("  •  ".join(parts))
        set_tone(self.lbl_sub, "danger" if self.state == "error" else "")
        self.progress.show_state(
            self.state, self.done, self.total, self.speed, self.limit.value(), self.paused
        )
        self.setToolTip(self.data["url"])

    def retheme(self) -> None:
        self.btn_remove.setIcon(symbol_icon("xmark", THEME.secondary, 13))
        if self.state == "running":
            symbol, tip = ("play.fill", "Продолжить") if self.paused else ("pause.fill", "Пауза")
        else:
            symbol, tip = self.ACTIONS.get(self.state, self.ACTIONS["idle"])
        self.btn_action.setIcon(symbol_icon(symbol, THEME.text, 16))
        self.btn_action.setToolTip(tip)
        self.refresh()
        self.update()

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        self.context_requested.emit(self, event.globalPos())

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        if self.state == "running":
            p.fillRect(self.rect(), THEME.row_active)
        elif self.dragging:
            p.fillRect(self.rect(), THEME.hover)
        if not self.first:
            inset = 10 + 16 + 12
            p.fillRect(QRect(inset, 0, self.width() - inset, 1), THEME.separator)


class StatTile(QWidget):
    def __init__(self, caption: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.value = QLabel("0")
        self.value.setObjectName("statValue")
        self.caption = QLabel(caption)
        self.caption.setObjectName("statCaption")
        v.addWidget(self.value)
        v.addWidget(self.caption)

    def set_value(self, n: int, danger: bool = False) -> None:
        self.value.setText(f"{n:,}".replace(",", " "))
        set_tone(self.value, "danger" if danger and n > 0 else "")


class RootView(QWidget):
    """
    Фон окна: боковая панель на всю высоту (в том числе под titlebar), область контента
    и мягкие тени под карточками.
    """

    titlebar_height = 0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.shadow_targets: List[QWidget] = []

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.fillRect(self.rect(), THEME.content)
        p.fillRect(QRect(0, 0, SIDEBAR_WIDTH, self.height()), THEME.sidebar)
        p.fillRect(QRect(SIDEBAR_WIDTH, 0, 1, self.height()), THEME.separator)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        for w in self.shadow_targets:
            if not w.isVisible():
                continue
            r = QRectF(QRect(w.mapTo(self, QPoint(0, 0)), w.size()))
            for i in range(4):
                c = QColor(THEME.shadow)
                c.setAlpha(max(1, THEME.shadow.alpha() - i * THEME.shadow.alpha() // 4))
                p.setBrush(c)
                grow = 1.0 + i * 1.5
                p.drawRoundedRect(r.adjusted(-grow + 1, -grow + 2, grow - 1, grow + 1.5), 14 + grow, 14 + grow)

    def _in_titlebar(self, y: float) -> bool:
        return y < self.titlebar_height

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        handle = self.window().windowHandle()
        if event.button() == Qt.LeftButton and handle and self._in_titlebar(event.position().y()):
            handle.startSystemMove()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if self._in_titlebar(event.position().y()):
            win = self.window()
            win.showNormal() if win.isMaximized() else win.showMaximized()
            return
        super().mouseDoubleClickEvent(event)


# ---------------------------------------------------------------- фоновая работа


class Bridge(QObject):
    log = Signal(str)
    progress_status = Signal(str)
    progress_bar = Signal(int, int)
    stats = Signal(dict)
    upscale_progress = Signal(str, int, int)
    download_timer = Signal(str)
    upscale_timer = Signal(str)
    notify = Signal(str, str)
    urls_found = Signal(list)
    job_started = Signal(str)
    job_finished = Signal(str, bool)
    job_progress = Signal(str, int, int, float)
    finished = Signal()
    board_ready = Signal(str, str)
    repair_finished = Signal()
    engine_ready = Signal(str)
    thumb_ready = Signal(str, object, int)


def _engine_callbacks(bridge: Bridge) -> Dict[str, Callable]:
    return dict(
        log=bridge.log.emit,
        progress_status=bridge.progress_status.emit,
        progress_bar=bridge.progress_bar.emit,
        stats_line=lambda _line: None,
        stats_changed=bridge.stats.emit,
        upscale_progress=lambda t, v, m: bridge.upscale_progress.emit(t, v, m),
        download_timer=bridge.download_timer.emit,
        upscale_timer=bridge.upscale_timer.emit,
        notify=bridge.notify.emit,
    )


class DownloadThread(QThread):
    def __init__(self, settings: Dict[str, Any], jobs: Any, control: Any, bridge: Bridge):
        super().__init__()
        self.settings = settings
        self.jobs = jobs
        self.control = control
        self.bridge = bridge

    def run(self) -> None:
        try:
            from pinterest_download_engine import DownloadSettings, PinterestDownloadEngine

            eng = PinterestDownloadEngine(
                DownloadSettings(**self.settings),
                self.control,
                urls_discovered=lambda urls: self.bridge.urls_found.emit(list(urls)),
                job_started=self.bridge.job_started.emit,
                job_finished=self.bridge.job_finished.emit,
                job_progress=lambda k, d, t, s: self.bridge.job_progress.emit(k, d, t, float(s)),
                **_engine_callbacks(self.bridge),
            )
            eng.run_multi(self.jobs)
        except Exception as e:
            self.bridge.log.emit(f"❌ Ошибка: {e}\n{traceback.format_exc()}")
        finally:
            self.bridge.finished.emit()


# ---------------------------------------------------------------- окно


class MainWindow(QMainWindow):
    PAGES = [
        ("download", "Загрузка", "arrow.down.to.line"),
        ("upscale", "Upscale", "sparkles"),
        ("history", "История", "clock"),
        ("settings", "Настройки", "gearshape"),
        ("log", "Журнал", "doc.text"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1060, 700)
        self.resize(1320, 880)
        self.setAcceptDrops(True)
        if EXPANDED_CLIENT_AREA is not None:
            # Контент под прозрачным titlebar — боковая панель на всю высоту окна
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self.setWindowFlag(EXPANDED_CLIENT_AREA, True)
            self.setWindowFlag(Qt.WindowType.NoTitleBarBackgroundHint, True)
            self.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)

        self._bridge = Bridge()
        self._control: Any = None
        self._job_queue: Any = None
        self._worker: Optional[DownloadThread] = None
        self._repair_running = False
        self._downloading = False
        self._paused = False
        self._jobs_started = 0
        self._current_key: Optional[str] = None
        self._timer_text = ""
        self._last_image_urls: List[str] = []
        self._url_rows: List[Dict[str, Any]] = []
        self._rows: List[QueueRow] = []
        self._chips: List[QPushButton] = []
        self._history_cache: List[dict] = []
        self._drag_row: Optional[QueueRow] = None
        self._retheme_fns: List[Callable[[], None]] = []
        self._folder = DEFAULT_FOLDER
        self._account_email = ""
        self._upscale_exe: Optional[str] = None
        self._engine_loaded = False
        self._safe_area_hooked = False
        self._status = ("download", "idle")
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ui-bg")
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_ui_settings)

        self._build_ui()
        self._build_menu()
        self._wire_bridge()
        self._load_ui_settings()
        self._history_cache = self._read_history()
        self._load_saved_urls()
        self._connect_autosave()
        self._set_idle_status()

        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_scheme_changed)
        threading.Thread(target=self._warm_up, daemon=True).start()

    # ------------------------------------------------------------ построение UI

    def _themed(self, fn: Callable[[], None]) -> None:
        fn()
        self._retheme_fns.append(fn)

    def _label(self, text: str, name: str, wrap: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName(name)
        lbl.setWordWrap(wrap)
        return lbl

    def _button(
        self,
        text: str,
        kind: str = "outline",
        symbol: Optional[str] = None,
        size: Optional[str] = None,
        slot: Optional[Callable[[], Any]] = None,
    ) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName(kind)
        btn.setCursor(Qt.PointingHandCursor)
        if size:
            btn.setProperty("size", size)
        if symbol:
            px = 14 if kind in ("outline", "destructive", "link", "chip") and size != "large" else 16
            btn.setIconSize(QSize(px, px))

            def apply(b: QPushButton = btn, k: str = kind, s: str = symbol, n: int = px) -> None:
                color = {
                    "primary": THEME.on_accent,
                    "destructive": THEME.danger,
                    "link": THEME.accent,
                }.get(k, THEME.text)
                b.setIcon(symbol_icon(s, color, n))

            self._themed(apply)
        if slot:
            btn.clicked.connect(lambda _=False, s=slot: s())
        return btn

    def _square(self, symbol: str, tip: str, slot: Callable[[], Any], size: int = 34) -> QToolButton:
        btn = QToolButton()
        btn.setObjectName("square")
        btn.setFixedSize(size, size)
        btn.setIconSize(QSize(16, 16))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(tip)
        btn.clicked.connect(lambda _=False: slot())
        self._themed(lambda: btn.setIcon(symbol_icon(symbol, THEME.text, 16)))
        return btn

    def _page(
        self, title: str, subtitle: str = "", scroll: bool = False
    ) -> Tuple[QWidget, QVBoxLayout, QHBoxLayout, QLabel]:
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 8, 32, 26)
        v.setSpacing(18)
        head = QHBoxLayout()
        head.setSpacing(10)
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        tcol.addWidget(self._label(title, "largeTitle"))
        sub = self._label(subtitle, "subtitle", wrap=True)
        tcol.addWidget(sub)
        head.addLayout(tcol, 1)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        head.addLayout(actions)
        v.addLayout(head)
        if not scroll:
            return page, v, actions, sub
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("scrollBody")
        body = QVBoxLayout(inner)
        body.setContentsMargins(0, 0, 14, 8)
        body.setSpacing(8)
        sa.setWidget(inner)
        v.addWidget(sa, 1)
        return page, body, actions, sub

    def _section(self, text: str, top: int = 12) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, top, 0, 2)
        h.addWidget(self._label(text, "sectionHeader"))
        return w

    def _empty_state(self, symbol: str, title: str, text: str) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(24, 36, 24, 36)
        v.setSpacing(6)
        v.addStretch()
        icon = QLabel()
        icon.setAlignment(Qt.AlignCenter)
        self._themed(lambda: icon.setPixmap(symbol_pixmap(symbol, THEME.tertiary, 48)))
        v.addWidget(icon)
        v.addSpacing(8)
        t = self._label(title, "emptyTitle")
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        d = self._label(text, "emptyText", wrap=True)
        d.setAlignment(Qt.AlignCenter)
        v.addWidget(d)
        v.addStretch()
        return w

    def _add_shadow(self, widget: QWidget) -> None:
        self.root.shadow_targets.append(widget)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj in self.root.shadow_targets and event.type() in (
            QEvent.Move, QEvent.Resize, QEvent.Show, QEvent.Hide
        ):
            self.root.update()
        return super().eventFilter(obj, event)

    def _build_ui(self) -> None:
        self.root = RootView()
        self.setCentralWidget(self.root)
        outer = QHBoxLayout(self.root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        self.stack.setObjectName("stack")
        outer.addWidget(self.stack, 1)

        self.stack.addWidget(self._build_download_page())
        self.stack.addWidget(self._build_upscale_page())
        self.stack.addWidget(self._build_history_page())
        self.stack.addWidget(self._build_settings_page())
        self.stack.addWidget(self._build_log_page())
        self.stack.currentChanged.connect(self._on_page_changed)
        self._nav.button(0).setChecked(True)

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(SIDEBAR_WIDTH)
        v = QVBoxLayout(side)
        v.setContentsMargins(18, 14, 18, 18)
        v.setSpacing(6)

        brand = QHBoxLayout()
        brand.setContentsMargins(6, 0, 0, 0)
        brand.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(render_app_icon(52, margin=False))
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addStretch()
        names.addWidget(self._label("Pinterest", "appName"))
        names.addWidget(self._label("Image Downloader", "appCaption"))
        names.addStretch()
        brand.addLayout(names, 1)
        v.addLayout(brand)
        v.addSpacing(22)

        self._nav = QButtonGroup(self)
        self._nav.setExclusive(True)
        self._nav_items: List[SidebarItem] = []
        for i, (_key, title, symbol) in enumerate(self.PAGES):
            shortcut = QKeySequence(f"Ctrl+{i + 1}").toString(QKeySequence.NativeText)
            item = SidebarItem(title, symbol, shortcut)
            self._nav.addButton(item, i)
            self._nav_items.append(item)
            v.addWidget(item)
        self._nav.idClicked.connect(self._go_to)

        v.addStretch(1)

        self.side_card = QFrame()
        self.side_card.setObjectName("sideCard")
        sc = QVBoxLayout(self.side_card)
        sc.setContentsMargins(14, 12, 14, 12)
        sc.setSpacing(7)
        self.lbl_side_title = ElidedLabel(mode=Qt.ElideRight)
        self.lbl_side_title.setObjectName("sideTitle")
        sc.addWidget(self.lbl_side_title)
        bar_row = QHBoxLayout()
        bar_row.setSpacing(10)
        self.side_bar = QProgressBar()
        self.side_bar.setObjectName("thin")
        self.side_bar.setTextVisible(False)
        bar_row.addWidget(self.side_bar, 1)
        self.lbl_side_pct = self._label("", "percent")
        bar_row.addWidget(self.lbl_side_pct)
        sc.addLayout(bar_row)
        self.lbl_side_info = ElidedLabel(mode=Qt.ElideRight)
        self.lbl_side_info.setObjectName("sideText")
        sc.addWidget(self.lbl_side_info)
        self.side_card.hide()
        v.addWidget(self.side_card)
        v.addSpacing(6)

        self.folder_chip = ClickableFrame("folderChip")
        self.folder_chip.setFixedHeight(46)
        fh = QHBoxLayout(self.folder_chip)
        fh.setContentsMargins(14, 0, 12, 0)
        fh.setSpacing(10)
        folder_icon = QLabel()
        folder_icon.setFixedSize(20, 20)
        open_icon = QLabel()
        open_icon.setFixedSize(16, 16)
        self._themed(lambda: folder_icon.setPixmap(symbol_pixmap("folder", THEME.accent, 20)))
        self._themed(lambda: open_icon.setPixmap(symbol_pixmap("arrow.up.forward.square", THEME.secondary, 16)))
        self.lbl_folder_chip = ElidedLabel(mode=Qt.ElideMiddle)
        self.lbl_folder_chip.setObjectName("rowTitle")
        fh.addWidget(folder_icon)
        fh.addWidget(self.lbl_folder_chip, 1)
        fh.addWidget(open_icon)
        self.folder_chip.clicked.connect(self._open_folder)
        v.addWidget(self.folder_chip)
        return side

    # --- Загрузка

    def _build_download_page(self) -> QWidget:
        page, body, actions, self.lbl_dl_subtitle = self._page("Загрузка")

        self.btn_pause = self._button("Пауза", "secondary", "pause.fill", slot=self._pause)
        self.btn_stop = self._button("Остановить", "primary", "stop.fill", slot=self._stop)
        self.btn_start = self._button("Скачать", "primary", "arrow.down", slot=self._start)
        self.btn_start.setToolTip(
            "Скачать все доски из очереди  "
            + QKeySequence("Ctrl+Return").toString(QKeySequence.NativeText)
        )
        self.btn_pause.hide()
        self.btn_stop.hide()
        for b in (self.btn_pause, self.btn_stop, self.btn_start):
            b.setMinimumWidth(128)
            actions.addWidget(b)

        input_col = QVBoxLayout()
        input_col.setSpacing(10)
        url_row = QHBoxLayout()
        url_row.setSpacing(12)
        self.url_box = UrlBox()
        self.ed_url = self.url_box.edit
        self.ed_url.returnPressed.connect(self._add_from_field)
        self.url_box.btn_paste.clicked.connect(self._paste_from_clipboard)
        self._themed(self.url_box.retheme)
        url_row.addWidget(self.url_box, 1)
        url_row.addSpacing(6)
        url_row.addWidget(self._label("Лимит", "fieldLabel"))
        self.limit_default = LimitPicker(0, compact=False)
        self.limit_default.setToolTip("Сколько изображений брать с новых досок")
        url_row.addWidget(self.limit_default)
        self.btn_add = self._button("Добавить", "primary", "plus", "large", self._add_from_field)
        self.btn_add.setMinimumWidth(140)
        url_row.addWidget(self.btn_add)
        input_col.addLayout(url_row)

        self.chips_row = QWidget()
        self.chips_layout = QHBoxLayout(self.chips_row)
        self.chips_layout.setContentsMargins(0, 0, 0, 0)
        self.chips_layout.setSpacing(8)
        self.chips_layout.addStretch()
        self.chips_row.hide()
        input_col.addWidget(self.chips_row)
        body.addLayout(input_col)

        self.queue_card = QFrame()
        self.queue_card.setObjectName("card")
        qc = QVBoxLayout(self.queue_card)
        qc.setContentsMargins(1, 1, 1, 1)
        qc.setSpacing(0)
        head = QWidget()
        hh = QHBoxLayout(head)
        hh.setContentsMargins(20, 12, 14, 12)
        hh.setSpacing(10)
        hh.addWidget(self._label("Очередь досок", "cardTitle"))
        self.lbl_queue_badge = self._label("0", "badge")
        self.lbl_queue_badge.setAlignment(Qt.AlignCenter)
        hh.addWidget(self.lbl_queue_badge)
        hh.addStretch()
        self.btn_refresh_names = self._button(
            "Обновить названия", "outline", "arrow.clockwise", slot=self._refresh_names
        )
        self.btn_clear_queue = self._button("Очистить", "outline", "trash", slot=self._clear_urls)
        self.btn_queue_more = self._square("ellipsis", "Ещё", self._queue_menu)
        hh.addWidget(self.btn_refresh_names)
        hh.addWidget(self.btn_clear_queue)
        hh.addWidget(self.btn_queue_more)
        qc.addWidget(head)
        qc.addWidget(Separator())
        self.queue_scroll = QScrollArea()
        self.queue_scroll.setWidgetResizable(True)
        self.queue_scroll.setFrameShape(QFrame.NoFrame)
        self.queue_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.queue_body = QWidget()
        self.queue_body.setObjectName("queueBody")
        self.rows_box = QVBoxLayout(self.queue_body)
        self.rows_box.setContentsMargins(0, 0, 0, 8)
        self.rows_box.setSpacing(0)
        self.rows_box.addStretch()
        self.queue_scroll.setWidget(self.queue_body)
        qc.addWidget(self.queue_scroll)
        self.queue_empty = self._empty_state(
            "square.and.arrow.down.on.square",
            "Очередь пуста",
            "Вставьте ссылку на доску Pinterest в поле выше\nили просто перетащите её в окно.",
        )
        qc.addWidget(self.queue_empty)
        body.addWidget(self.queue_card, 1)

        self.activity_card = self._build_activity_card()
        body.addWidget(self.activity_card)
        self._add_shadow(self.queue_card)
        self._add_shadow(self.activity_card)
        return page

    def _build_activity_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(14)
        self.status_badge = StatusBadge(48)
        top.addWidget(self.status_badge, 0, Qt.AlignTop)
        tcol = QVBoxLayout()
        tcol.setSpacing(3)
        self.lbl_status = ElidedLabel(mode=Qt.ElideRight)
        self.lbl_status.setObjectName("statusTitle")
        self.lbl_status_text = ElidedLabel(mode=Qt.ElideRight)
        self.lbl_status_text.setObjectName("statusText")
        tcol.addWidget(self.lbl_status)
        tcol.addWidget(self.lbl_status_text)
        top.addLayout(tcol, 1)
        self.lbl_timer = self._label("", "timer")
        top.addWidget(self.lbl_timer, 0, Qt.AlignTop)
        v.addLayout(top)

        bar_row = QHBoxLayout()
        bar_row.setSpacing(12)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        bar_row.addWidget(self.bar, 1)
        self.lbl_pct = self._label("", "percent")
        self.lbl_pct.setMinimumWidth(34)
        self.lbl_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bar_row.addWidget(self.lbl_pct)
        v.addLayout(bar_row)

        self.up_box = QWidget()
        ub = QVBoxLayout(self.up_box)
        ub.setContentsMargins(0, 0, 0, 0)
        ub.setSpacing(6)
        up_top = QHBoxLayout()
        self.lbl_up = self._label("", "muted")
        up_top.addWidget(self.lbl_up)
        up_top.addStretch()
        self.lbl_timer_up = self._label("", "muted")
        up_top.addWidget(self.lbl_timer_up)
        ub.addLayout(up_top)
        self.bar_up = QProgressBar()
        self.bar_up.setObjectName("thin")
        self.bar_up.setTextVisible(False)
        ub.addWidget(self.bar_up)
        self.up_box.hide()
        v.addWidget(self.up_box)

        bottom = QHBoxLayout()
        bottom.setSpacing(48)
        self.tile_found = StatTile("Найдено")
        self.tile_done = StatTile("Скачано")
        self.tile_skipped = StatTile("Пропущено")
        self.tile_failed = StatTile("Ошибки")
        for tile in (self.tile_found, self.tile_done, self.tile_skipped, self.tile_failed):
            bottom.addWidget(tile)
        bottom.addStretch()
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self._button("Открыть папку", "outline", "folder", "large", self._open_folder))
        self.btn_export = self._button(
            "Экспорт ссылок…", "outline", "square.and.arrow.up", "large", self._export_urls
        )
        self.btn_export.setEnabled(False)
        buttons.addWidget(self.btn_export)
        bottom.addLayout(buttons)
        v.addLayout(bottom)
        return card

    # --- Upscale

    def _build_upscale_page(self) -> QWidget:
        page, body, _actions, _ = self._page(
            "Upscale",
            "Улучшение качества через Real-ESRGAN — работает локально, на вашей видеокарте.",
            scroll=True,
        )
        body.addWidget(self._section("Параметры", top=0))
        g = Group()
        self.sw_upscale = ToggleSwitch()
        g.add_row(
            "Улучшать после скачивания",
            self.sw_upscale,
            "Результат сохраняется в подпапку upscale рядом с оригиналами",
        )
        self.seg_scale = SegmentedControl([(2, "×2"), (3, "×3"), (4, "×4")], 3)
        g.add_row("Масштаб", self.seg_scale)
        self.seg_model = SegmentedControl(
            [("auto", "Авто"), ("photo", "Фото"), ("anime", "Аниме")], "auto"
        )
        g.add_row("Модель", self.seg_model, "«Аниме» лучше подходит для рисунков и иллюстраций")
        self.sp_tile = QSpinBox()
        self.sp_tile.setRange(50, 500)
        self.sp_tile.setSingleStep(10)
        self.sp_tile.setValue(200)
        self.sp_tile.setFixedWidth(96)
        g.add_row("Размер тайла", self.sp_tile, "Меньше значение — меньше нужно видеопамяти")
        self.sp_gpu = QSpinBox()
        self.sp_gpu.setRange(0, 10)
        self.sp_gpu.setFixedWidth(96)
        g.add_row("Видеокарта", self.sp_gpu, "Номер GPU; 0 — первая")
        body.addWidget(g)

        body.addWidget(self._section("Real-ESRGAN"))
        g2 = Group()
        self.lbl_esrgan_icon = QLabel()
        self.lbl_esrgan_icon.setFixedSize(22, 22)
        _row, self.lbl_esrgan_title, self.lbl_esrgan_sub = g2.add_row(
            "Проверяю…", self.lbl_esrgan_icon, ""
        )
        body.addWidget(g2)

        body.addWidget(self._section("Инструменты"))
        g3 = Group()
        self.btn_repair = self._button(
            "Запустить", "outline", "play.fill", slot=self._start_upscale_repair
        )
        g3.add_row(
            "Дозаполнить недостающие",
            self.btn_repair,
            "Найдёт изображения без upscale-версии во всех папках и обработает только их",
        )
        self.btn_clear_upscale = self._button(
            "Удалить…", "destructive", "trash", slot=self._clear_upscale_outputs
        )
        g3.add_row(
            "Удалить результаты upscale",
            self.btn_clear_upscale,
            "Очистит папки upscale; оригинальные файлы не затрагиваются",
        )
        body.addWidget(g3)
        body.addStretch()
        return page

    # --- История

    def _build_history_page(self) -> QWidget:
        page, body, actions, self.lbl_hist_sub = self._page("История")
        self.btn_hist_add = self._button(
            "Добавить в очередь", "primary", "plus", slot=self._add_history_selection
        )
        self.btn_hist_add.setEnabled(False)
        actions.addWidget(self.btn_hist_add)

        self.ed_hist_search = QLineEdit()
        self.ed_hist_search.setObjectName("field")
        self.ed_hist_search.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.ed_hist_search.setPlaceholderText("Поиск по названию или ссылке")
        self.ed_hist_search.setClearButtonEnabled(True)
        self.ed_hist_search.setMaximumWidth(420)
        self.ed_hist_search.setMinimumHeight(38)
        search_action = self.ed_hist_search.addAction(QIcon(), QLineEdit.LeadingPosition)
        self._themed(
            lambda: search_action.setIcon(symbol_icon("magnifyingglass", THEME.secondary, 15))
        )
        self.ed_hist_search.textChanged.connect(self._filter_history)
        body.addWidget(self.ed_hist_search)

        self.tree = QTreeWidget()
        self.tree.setObjectName("history")
        self.tree.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Доска", "Скачано", "Дата"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._history_menu)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.tree.itemSelectionChanged.connect(
            lambda: self.btn_hist_add.setEnabled(bool(self.tree.selectedItems()))
        )
        self.tree.itemDoubleClicked.connect(lambda *_: self._add_history_selection())
        self.hist_empty = self._empty_state(
            "clock",
            "История пуста",
            "Здесь появятся доски, которые вы уже скачивали.",
        )
        body.addWidget(self.tree, 1)
        body.addWidget(self.hist_empty, 1)
        return page

    # --- Настройки

    def _build_settings_page(self) -> QWidget:
        page, body, _actions, _ = self._page(
            "Настройки", "Куда и как сохранять изображения.", scroll=True
        )

        body.addWidget(self._section("Аккаунт Pinterest", top=0))
        g = Group()
        account_ctrl = QWidget()
        ah = QHBoxLayout(account_ctrl)
        ah.setContentsMargins(0, 0, 0, 0)
        ah.setSpacing(8)
        self.btn_logout = self._button("Выйти", "link", slot=self._logout)
        self.btn_account = self._button("Войти…", "outline", "person.crop.circle", slot=self._edit_account)
        ah.addWidget(self.btn_logout)
        ah.addWidget(self.btn_account)
        _row, _t, self.lbl_account = g.add_row("Вход в Pinterest", account_ctrl, "")
        body.addWidget(g)

        body.addWidget(self._section("Сохранение"))
        g = Group()
        folder_ctrl = QWidget()
        fh = QHBoxLayout(folder_ctrl)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.setSpacing(12)
        self.lbl_folder = ElidedLabel()
        self.lbl_folder.setObjectName("muted")
        self.lbl_folder.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.lbl_folder.setFixedWidth(280)
        self.lbl_folder.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        fh.addWidget(self.lbl_folder)
        fh.addWidget(self._button("Выбрать…", "outline", "folder", slot=self._pick_folder))
        g.add_row("Папка загрузки", folder_ctrl)
        self.sw_subfolder = ToggleSwitch(True)
        g.add_row("Отдельная папка для каждой доски", self.sw_subfolder)
        self.sw_resume = ToggleSwitch(True)
        g.add_row(
            "Пропускать уже скачанные",
            self.sw_resume,
            "Позволяет докачать доску, не загружая файлы повторно",
        )
        body.addWidget(g)

        body.addWidget(self._section("Изображения"))
        g = Group()
        self.seg_quality = SegmentedControl(
            [("full", "Оригинал"), ("medium", "Среднее"), ("small", "Маленькое")], "full"
        )
        g.add_row("Качество", self.seg_quality, "Оригинал — максимальное разрешение, которое отдаёт Pinterest")
        size_ctrl = QWidget()
        sh = QHBoxLayout(size_ctrl)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(6)
        self.sp_min_mb = QDoubleSpinBox()
        self.sp_min_mb.setRange(0, 10000)
        self.sp_min_mb.setDecimals(1)
        self.sp_min_mb.setFixedWidth(84)
        self.sp_max_mb = QDoubleSpinBox()
        self.sp_max_mb.setRange(0, 10000)
        self.sp_max_mb.setDecimals(1)
        self.sp_max_mb.setValue(1000)
        self.sp_max_mb.setFixedWidth(84)
        sh.addWidget(self._label("от", "muted"))
        sh.addWidget(self.sp_min_mb)
        sh.addWidget(self._label("до", "muted"))
        sh.addWidget(self.sp_max_mb)
        sh.addWidget(self._label("МБ", "muted"))
        g.add_row("Размер файла", size_ctrl, "Файлы вне диапазона не сохраняются")
        body.addWidget(g)

        body.addWidget(self._section("Имена файлов"))
        g = Group()
        self.sw_autorename = ToggleSwitch(True)
        g.add_row("Называть по шаблону", self.sw_autorename)
        self.ed_template = QLineEdit(DEFAULT_TEMPLATE)
        self.ed_template.setObjectName("field")
        self.ed_template.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.ed_template.setFixedWidth(240)
        g.add_row("Шаблон", self.ed_template, "Доступно: {index}, {index04}, {hash}, {url_hash}")
        self.sw_autorename.toggled.connect(self.ed_template.setEnabled)
        body.addWidget(g)

        body.addWidget(self._section("Скорость"))
        g = Group()
        self.sp_scroll = QDoubleSpinBox()
        self.sp_scroll.setRange(0.2, 30)
        self.sp_scroll.setDecimals(1)
        self.sp_scroll.setSingleStep(0.5)
        self.sp_scroll.setValue(2.0)
        self.sp_scroll.setSuffix(" с")
        self.sp_scroll.setFixedWidth(96)
        g.add_row("Пауза при прокрутке", self.sp_scroll, "Больше — надёжнее на длинных досках")
        self.sp_dldelay = QDoubleSpinBox()
        self.sp_dldelay.setRange(0, 30)
        self.sp_dldelay.setDecimals(1)
        self.sp_dldelay.setSingleStep(0.1)
        self.sp_dldelay.setValue(0.5)
        self.sp_dldelay.setSuffix(" с")
        self.sp_dldelay.setFixedWidth(96)
        g.add_row("Пауза между файлами", self.sp_dldelay, "0 — максимальная скорость")
        body.addWidget(g)

        body.addWidget(self._section("Прочее"))
        g = Group()
        self.sw_notify = ToggleSwitch(True)
        g.add_row("Уведомление по завершении", self.sw_notify)
        self.sw_meta = ToggleSwitch(False)
        g.add_row("Сохранять метаданные", self.sw_meta, "JSON-файл со ссылками и статистикой рядом с изображениями")
        self.seg_appearance = SegmentedControl(
            [("auto", "Авто"), ("light", "Светлое"), ("dark", "Тёмное")], "auto"
        )
        self.seg_appearance.changed.connect(self._on_appearance_changed)
        g.add_row("Оформление", self.seg_appearance)
        _row, _t, self.lbl_data_dir = g.add_row(
            "Данные приложения",
            self._button("Показать", "outline", "folder", slot=lambda: self._reveal(Path.cwd())),
            "",
        )
        body.addWidget(g)
        body.addStretch()
        return page

    # --- Журнал

    def _build_log_page(self) -> QWidget:
        page, body, actions, _ = self._page("Журнал", "Подробности о каждом шаге загрузки")
        actions.addWidget(self._button("Скопировать", "outline", "doc.on.doc", "large", self._copy_log))
        actions.addWidget(self._button("Очистить", "outline", "trash", "large", lambda: self.log.clear()))
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(20000)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        body.addWidget(self.log, 1)
        return page

    def _build_menu(self) -> None:
        mb = self.menuBar()

        def act(menu: QMenu, text: str, shortcut: Optional[str], slot: Callable[[], Any]) -> QAction:
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(lambda _=False: slot())
            menu.addAction(a)
            return a

        m_file = mb.addMenu("Файл")
        act(m_file, "Добавить ссылку", "Ctrl+L", self._focus_url)
        act(m_file, "Добавить ссылки из буфера", "Ctrl+Shift+V", self._paste_from_clipboard)
        m_file.addSeparator()
        act(m_file, "Скачать", "Ctrl+Return", self._start)
        act(m_file, "Пауза / продолжить", "Ctrl+Shift+P", self._pause)
        act(m_file, "Остановить", "Ctrl+.", self._stop)
        m_file.addSeparator()
        act(m_file, "Открыть папку загрузки", "Ctrl+Shift+O", self._open_folder)
        act(m_file, "Экспорт ссылок…", "Ctrl+Shift+E", self._export_urls)
        prefs = act(m_file, "Настройки…", "Ctrl+,", lambda: self._go_to(3))
        prefs.setMenuRole(QAction.PreferencesRole)
        about = act(m_file, f"О программе {APP_NAME}", None, self._about)
        about.setMenuRole(QAction.AboutRole)

        m_view = mb.addMenu("Вид")
        for i, (_key, title, _symbol) in enumerate(self.PAGES):
            act(m_view, title, f"Ctrl+{i + 1}", lambda idx=i: self._go_to(idx))

    def _wire_bridge(self) -> None:
        b = self._bridge
        b.log.connect(self._append_log)
        b.progress_status.connect(self._on_status)
        b.stats.connect(self._on_stats)
        b.upscale_progress.connect(self._on_upscale_prog)
        b.download_timer.connect(self._on_download_timer)
        b.upscale_timer.connect(lambda s: self.lbl_timer_up.setText(s.replace(" | ", "  •  ")))
        b.notify.connect(self._on_notify)
        b.urls_found.connect(self._on_urls_found)
        b.job_started.connect(self._on_job_started)
        b.job_finished.connect(self._on_job_finished)
        b.job_progress.connect(self._on_job_progress)
        b.finished.connect(self._on_worker_finished)
        b.board_ready.connect(self._on_board_ready)
        b.repair_finished.connect(self._on_repair_finished)
        b.engine_ready.connect(self._on_engine_ready)
        b.thumb_ready.connect(self._on_thumb_ready)

    # ------------------------------------------------------------ тема и навигация

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if EXPANDED_CLIENT_AREA is None:
            return
        handle = self.windowHandle()
        if handle is not None and hasattr(handle, "safeAreaMargins") and not self._safe_area_hooked:
            self._safe_area_hooked = True
            handle.safeAreaMarginsChanged.connect(self._apply_safe_area)
        self._apply_safe_area()
        QTimer.singleShot(0, lambda: _mac_hide_window_title(self))

    def _apply_safe_area(self, *_args) -> None:
        """Отступ сверху под кнопки окна: контент рисуется под прозрачным titlebar."""
        handle = self.windowHandle()
        top = handle.safeAreaMargins().top() if handle is not None and hasattr(handle, "safeAreaMargins") else 0
        self.root.titlebar_height = top
        self.root.layout().setContentsMargins(0, top, 0, 0)

    def _on_scheme_changed(self, *_args) -> None:
        apply_theme(QApplication.instance())
        for fn in self._retheme_fns:
            fn()
        for row in self._rows:
            row.retheme()
        self._update_status_badge()
        self._update_esrgan_status()
        self._update_pause_button()
        for w in self.findChildren(QWidget):
            w.update()
        self.root.update()

    def _on_appearance_changed(self, value: Any) -> None:
        hints = QGuiApplication.styleHints()
        if not hasattr(hints, "setColorScheme"):
            return
        if value == "light":
            hints.setColorScheme(Qt.ColorScheme.Light)
        elif value == "dark":
            hints.setColorScheme(Qt.ColorScheme.Dark)
        else:
            hints.unsetColorScheme()
        self._on_scheme_changed()

    def _go_to(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self._nav.button(index).setChecked(True)

    def _on_page_changed(self, index: int) -> None:
        key = self.PAGES[index][0]
        if key == "history":
            self._load_history()
        elif key == "download":
            self.ed_url.setFocus()

    def _focus_url(self) -> None:
        self._go_to(0)
        self.ed_url.setFocus()
        self.ed_url.selectAll()

    # ------------------------------------------------------------ фон: движок, миниатюры, названия

    def _warm_up(self) -> None:
        """Импорт Selenium и движка занимает время — делаем это после показа окна."""
        try:
            import pinterest_download_engine as engine

            exe = engine.find_upscale_binary()
            self._bridge.engine_ready.emit(str(exe) if exe else "")
        except Exception as e:
            self._bridge.log.emit(f"⚠️ Не удалось загрузить модуль скачивания: {e}")
            self._bridge.engine_ready.emit("")

    @Slot(str)
    def _on_engine_ready(self, exe: str) -> None:
        self._engine_loaded = True
        self._upscale_exe = exe or None
        self._update_esrgan_status()

    def _update_esrgan_status(self) -> None:
        if not self._engine_loaded:
            return
        if self._upscale_exe:
            try:
                shown = str(Path(self._upscale_exe).relative_to(APP_DIR))
            except ValueError:
                shown = self._upscale_exe
            self.lbl_esrgan_title.setText("Установлен")
            self.lbl_esrgan_sub.setText(shown)
            self.lbl_esrgan_icon.setPixmap(symbol_pixmap("checkmark.circle.fill", THEME.success, 22))
        else:
            name = "realesrgan-ncnn-vulkan.exe" if sys.platform == "win32" else "realesrgan-ncnn-vulkan"
            self.lbl_esrgan_title.setText("Не найден")
            self.lbl_esrgan_sub.setText(f"Положите {name} и папку models в upscale/tools/")
            self.lbl_esrgan_icon.setPixmap(
                symbol_pixmap("exclamationmark.triangle.fill", THEME.warning, 22)
            )

    def _board_folder(self, board_name: Optional[str]) -> Optional[Path]:
        name = display_board(board_name)
        if not name or not self.sw_subfolder.isChecked():
            return None
        return Path(self._folder or DEFAULT_FOLDER) / name

    def _request_thumb(self, row: QueueRow) -> None:
        folder = self._board_folder(row.data.get("board_name"))
        url = row.data["url"]
        if folder is None:
            row.thumb.set_image(None)
            row.set_image_count(0)
            return
        dpr = _dpr()
        w, h = round(THUMB_W * dpr), round(THUMB_H * dpr)

        def job() -> None:
            first, count = scan_board_folder(folder)
            image = load_cover_thumbnail(first, w, h) if first else None
            self._bridge.thumb_ready.emit(url, image, count)

        self._pool.submit(job)

    @Slot(str, object, int)
    def _on_thumb_ready(self, url: str, image: object, count: int) -> None:
        row = self._row_by_key(url)
        if row is None:
            return
        if isinstance(image, QImage):
            image.setDevicePixelRatio(_dpr())
            row.thumb.set_image(image)
        else:
            row.thumb.set_image(None)
        row.set_image_count(count)
        self._update_queue_view()

    def _fetch_board(self, url: str) -> None:
        folder = self._folder or DEFAULT_FOLDER

        def job() -> None:
            try:
                from pinterest_parser import PinterestParser

                tp = PinterestParser(download_folder=folder)
                try:
                    name = tp.get_board_name_from_url(tp.expand_short_url(url))
                finally:
                    tp.close()
                self._bridge.board_ready.emit(url, name or "")
            except Exception:
                self._bridge.board_ready.emit(url, "")

        self._pool.submit(job)

    @Slot(str, str)
    def _on_board_ready(self, url: str, board: str) -> None:
        row = self._row_by_key(url)
        if row is not None:
            row.data["board_name"] = board or None
            row.lookup_done = True
            row.refresh()
            self._request_thumb(row)
            self._sync_pending()
        self._save_urls_state()

    # ------------------------------------------------------------ очередь

    def _row_by_key(self, key: str) -> Optional[QueueRow]:
        return next((r for r in self._rows if r.key == key), None)

    def _has_url(self, url: str) -> bool:
        return any(r["url"] == url for r in self._url_rows)

    def _add_urls(self, urls: List[str], board_names: Optional[Dict[str, str]] = None) -> int:
        added = 0
        for url in urls:
            if self._has_url(url):
                continue
            board = (board_names or {}).get(url)
            data = {"url": url, "board_name": board or None, "max_images": self.limit_default.value()}
            self._url_rows.append(data)
            row = self._append_row(data)
            if self._downloading:
                # Добавленная во время загрузки доска скачается в этом же запуске
                row.set_state("queued")
            if board:
                self._request_thumb(row)
            else:
                self._fetch_board(url)
            added += 1
        if added:
            self._sync_pending()
            self._save_urls_state()
        return added

    def _append_row(self, data: Dict[str, Any]) -> QueueRow:
        row = QueueRow(data)
        row.set_first(not self._rows)
        row.remove_clicked.connect(self._remove_row)
        row.limit_changed.connect(self._on_row_limit)
        row.action_clicked.connect(self._row_action)
        row.context_requested.connect(self._row_menu)
        row.drag_started.connect(self._drag_start)
        row.drag_moved.connect(self._drag_move)
        row.drag_finished.connect(self._drag_end)
        self.rows_box.insertWidget(self.rows_box.count() - 1, row)
        self._rows.append(row)
        return row

    def _on_row_limit(self, _row: QueueRow) -> None:
        self._sync_pending()
        self._save_urls_state()

    def _add_from_field(self) -> None:
        text = self.ed_url.text().strip()
        if not text:
            self.ed_url.setFocus()
            return
        urls = extract_pinterest_urls(text)
        if not urls:
            QMessageBox.warning(
                self,
                "Это не ссылка Pinterest",
                "Вставьте ссылку вида pinterest.com/имя/доска или pin.it/…",
            )
            return
        if self._add_urls(urls) == 0:
            QMessageBox.information(self, "Уже в очереди", "Эта доска уже есть в очереди.")
        self.ed_url.clear()

    def _paste_from_clipboard(self) -> None:
        urls = extract_pinterest_urls(QGuiApplication.clipboard().text())
        self._go_to(0)
        if not urls:
            QMessageBox.information(
                self, "Нет ссылок", "В буфере обмена нет ссылок на Pinterest."
            )
            return
        added = self._add_urls(urls)
        if added:
            self._append_log(f"📋 Из буфера добавлено: {added}")

    def _remove_row(self, row: QueueRow) -> None:
        if row not in self._rows or row.state == "running":
            return
        idx = self._rows.index(row)
        self._rows.pop(idx)
        self._url_rows.pop(idx)
        row.deleteLater()
        self._update_row_firsts()
        self._sync_pending()
        self._save_urls_state()

    def _clear_urls(self) -> None:
        removable = [r for r in self._rows if r.state != "running"]
        if not removable:
            return
        if len(removable) > 1:
            confirm = QMessageBox.question(
                self,
                "Очистить очередь?",
                f"Из очереди будут убраны {len(removable)} "
                f"{plural(len(removable), 'доска', 'доски', 'досок')}. "
                "Скачанные файлы останутся на месте.",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if confirm != QMessageBox.Yes:
                return
        for row in removable:
            self._remove_row(row)

    def _remove_finished(self) -> None:
        for row in [r for r in self._rows if r.state == "done"]:
            self._remove_row(row)

    def _sort_by_name(self) -> None:
        order = sorted(
            range(len(self._rows)),
            key=lambda i: (display_board(self._rows[i].data.get("board_name")) or "").lower(),
        )
        self._rows = [self._rows[i] for i in order]
        self._url_rows = [self._url_rows[i] for i in order]
        self._relayout_rows()
        self._sync_pending()
        self._save_urls_state()

    def _refresh_names(self) -> None:
        for row in self._rows:
            self._fetch_board(row.data["url"])

    def _relayout_rows(self) -> None:
        for row in self._rows:
            self.rows_box.removeWidget(row)
        for i, row in enumerate(self._rows):
            self.rows_box.insertWidget(i, row)
        self._update_row_firsts()

    def _update_row_firsts(self) -> None:
        for i, row in enumerate(self._rows):
            row.set_first(i == 0)

    def _move_row(self, row: QueueRow, target: int) -> None:
        cur = self._rows.index(row)
        target = max(0, min(len(self._rows) - 1, target))
        if cur == target:
            return
        self._rows.insert(target, self._rows.pop(cur))
        self._url_rows.insert(target, self._url_rows.pop(cur))
        self.rows_box.removeWidget(row)
        self.rows_box.insertWidget(target, row)
        self._update_row_firsts()

    def _drag_start(self, row: QueueRow) -> None:
        self._drag_row = row
        row.set_dragging(True)

    def _drag_move(self, row: QueueRow, global_pos: QPoint) -> None:
        if self._drag_row is not row:
            return
        y = self.queue_body.mapFromGlobal(global_pos).y()
        target = len(self._rows) - 1
        for i, r in enumerate(self._rows):
            if y < r.geometry().center().y():
                target = i
                break
        self._move_row(row, target)
        self.queue_scroll.ensureVisible(0, y, 0, 40)

    def _drag_end(self, row: QueueRow) -> None:
        row.set_dragging(False)
        self._drag_row = None
        self._sync_pending()
        self._save_urls_state()

    def _row_action(self, row: QueueRow) -> None:
        if row.state == "running":
            self._pause()
        elif row.state == "done":
            self._row_menu(row, row.btn_action.mapToGlobal(QPoint(0, row.btn_action.height() + 4)))
        elif not self._downloading:
            self._start(only=[row])
        elif row.state == "queued":
            # «Скачать следующей» — сразу за текущей доской
            cur = self._rows.index(row)
            running = next((i for i, r in enumerate(self._rows) if r.state == "running"), -1)
            self._move_row(row, running + 1 if cur > running else running)
            self._sync_pending()
            self._save_urls_state()
        else:
            if self._job_queue is not None:
                self._job_queue.release(row.key)
            row.set_state("queued")
            self._sync_pending()

    def _row_menu(self, row: QueueRow, pos) -> None:
        menu = make_menu(self)
        menu_item(menu, "Открыть доску в браузере", "safari", lambda: QDesktopServices.openUrl(QUrl(row.data["url"])))
        folder = self._board_folder(row.data.get("board_name"))
        if folder is not None and folder.is_dir():
            menu_item(menu, "Показать в Finder", "folder", lambda: self._reveal(folder))
        menu_item(menu, "Скопировать ссылку", "doc.on.doc", lambda: QGuiApplication.clipboard().setText(row.data["url"]))
        menu.addSeparator()
        menu_danger(menu, "Убрать из очереди", "trash", lambda: self._remove_row(row), row.state != "running")
        menu.exec(pos)

    def _queue_menu(self) -> None:
        menu = make_menu(self)
        has_done = any(r.state == "done" for r in self._rows)
        menu_item(menu, "Убрать скачанные", "checkmark.circle", self._remove_finished, has_done)
        menu_item(menu, "Сортировать по названию", "arrow.up.arrow.down", self._sort_by_name, len(self._rows) > 1)
        menu.addSeparator()
        menu_item(menu, "Открыть папку загрузки", "folder", self._open_folder)
        menu_item(menu, "Экспорт ссылок…", "square.and.arrow.up", self._export_urls, bool(self._last_image_urls))
        b = self.btn_queue_more
        menu.exec(b.mapToGlobal(QPoint(b.width() - menu.sizeHint().width(), b.height() + 4)))

    def _update_queue_view(self) -> None:
        n = len(self._rows)
        self.queue_scroll.setVisible(n > 0)
        self.queue_empty.setVisible(n == 0)
        self.lbl_queue_badge.setText(str(n))
        self._nav_items[0].set_badge(str(n) if n else "")
        self.btn_clear_queue.setEnabled(any(r.state != "running" for r in self._rows))
        self.btn_refresh_names.setEnabled(n > 0)
        total = sum(r.image_count for r in self._rows)
        if n:
            text = f"{n} {plural(n, 'доска', 'доски', 'досок')} в очереди"
            if total:
                text += f"  •  Скачано {total} {plural(total, 'изображение', 'изображения', 'изображений')}"
            self.lbl_dl_subtitle.setText(text)
        else:
            self.lbl_dl_subtitle.setText("Добавьте доски — приложение само найдёт и скачает все пины.")
        self._refresh_recent()

    def _refresh_recent(self) -> None:
        """Чипы с недавними досками из истории, которых нет в очереди."""
        in_queue = {url_key(r["url"]) for r in self._url_rows}
        seen: set = set()
        picks: List[Tuple[str, str]] = []
        for it in reversed(self._history_cache):
            url = str(it.get("url") or "")
            key = url_key(url) if url else ""
            if not key or key in seen or key in in_queue:
                continue
            seen.add(key)
            picks.append((url, str(it.get("board_name") or "")))
            if len(picks) == 3:
                break
        if [c.property("url") for c in self._chips] == [u for u, _ in picks]:
            return
        for chip in self._chips:
            chip.deleteLater()
        self._chips = []
        for url, board in picks:
            text = pretty_url(url)
            if len(text) > 44:
                text = text[:43] + "…"
            chip = self._button(text, "chip", slot=lambda u=url, b=board: self._add_urls([u], {u: b}))
            chip.setProperty("url", url)
            chip.setToolTip(f"Недавняя доска «{display_board(board) or url}» — нажмите, чтобы добавить")
            self.chips_layout.insertWidget(len(self._chips), chip)
            self._chips.append(chip)
        self.chips_row.setVisible(bool(picks))

    def _save_urls_state(self) -> None:
        try:
            payload = {"default_max": int(self.limit_default.value()), "rows": self._url_rows}
            SAVED_URLS_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            self._append_log(f"⚠️ Не удалось сохранить список URL: {e}")
        self._update_queue_view()

    def _load_saved_urls(self) -> None:
        if SAVED_URLS_FILE.exists():
            try:
                data = json.loads(SAVED_URLS_FILE.read_text(encoding="utf-8"))
                self.limit_default.setValue(max(0, int(data.get("default_max", 0))))
                for item in data.get("rows", []) if isinstance(data.get("rows"), list) else []:
                    if not isinstance(item, dict) or not str(item.get("url", "")).strip():
                        continue
                    row = {
                        "url": str(item["url"]).strip(),
                        "board_name": item.get("board_name") or None,
                        "max_images": int(item.get("max_images") or 0),
                    }
                    if self._has_url(row["url"]):
                        continue
                    self._url_rows.append(row)
                    self._append_row(row)
            except Exception as e:
                self._append_log(f"⚠️ Не удалось загрузить список URL: {e}")
        for row in self._rows:
            if row.data["board_name"]:
                self._request_thumb(row)
            else:
                self._fetch_board(row.data["url"])
        self._update_queue_view()

    # ------------------------------------------------------------ история

    def _read_history(self) -> List[dict]:
        if not HISTORY_FILE.exists():
            return []
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            return [h for h in data if isinstance(h, dict) and h.get("url")] if isinstance(data, list) else []
        except Exception as e:
            self._append_log(f"⚠️ Не удалось прочитать историю: {e}")
            return []

    def _load_history(self) -> None:
        hist = self._history_cache = self._read_history()
        self.tree.clear()
        for it in reversed(hist[-500:]):
            board = display_board(it.get("board_name")) or pretty_url(str(it["url"]))
            count, total = it.get("count", ""), it.get("total")
            done = f"{count} из {total}" if total not in (None, "") else str(count)
            item = QTreeWidgetItem([board, done, human_date(str(it.get("date") or ""))])
            item.setData(0, Qt.UserRole, str(it["url"]))
            item.setData(1, Qt.UserRole, it.get("board_name") or "")
            item.setToolTip(0, str(it["url"]))
            self.tree.addTopLevelItem(item)
        n = len(hist)
        self.tree.setVisible(n > 0)
        self.hist_empty.setVisible(n == 0)
        self.ed_hist_search.setVisible(n > 0)
        self.lbl_hist_sub.setText(
            f"{n} {plural(n, 'загрузка', 'загрузки', 'загрузок')}. Дважды щёлкните, чтобы добавить доску в очередь."
            if n
            else "Здесь хранятся все ваши загрузки."
        )
        self._filter_history(self.ed_hist_search.text())
        self._refresh_recent()

    def _filter_history(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            hay = f"{item.text(0)} {unquote(str(item.data(0, Qt.UserRole)))}".lower()
            item.setHidden(bool(needle) and needle not in hay)

    def _add_history_selection(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        urls, names = [], {}
        for item in items:
            url = str(item.data(0, Qt.UserRole))
            if url not in urls:
                urls.append(url)
                names[url] = str(item.data(1, Qt.UserRole) or "")
        added = self._add_urls(urls, names)
        if added:
            self._go_to(0)
        else:
            QMessageBox.information(self, "Уже в очереди", "Выбранные доски уже есть в очереди.")

    def _history_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        url = str(item.data(0, Qt.UserRole))
        menu = make_menu(self)
        menu_item(menu, "Добавить в очередь", "plus", self._add_history_selection)
        menu_item(menu, "Открыть в браузере", "safari", lambda: QDesktopServices.openUrl(QUrl(url)))
        menu_item(menu, "Скопировать ссылку", "doc.on.doc", lambda: QGuiApplication.clipboard().setText(url))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ настройки

    def _ui_settings(self) -> Dict[str, Any]:
        return {
            "download_folder": self._folder,
            "image_quality": self.seg_quality.value(),
            "min_size_mb": float(self.sp_min_mb.value()),
            "max_size_mb": float(self.sp_max_mb.value()),
            "auto_subfolder": self.sw_subfolder.isChecked(),
            "resume_download": self.sw_resume.isChecked(),
            "auto_rename": self.sw_autorename.isChecked(),
            "filename_template": self.ed_template.text().strip() or DEFAULT_TEMPLATE,
            "export_metadata": self.sw_meta.isChecked(),
            "scroll_delay": float(self.sp_scroll.value()),
            "download_delay": float(self.sp_dldelay.value()),
            "enable_upscale": self.sw_upscale.isChecked(),
            "upscale_scale": int(self.seg_scale.value()),
            "upscale_model": str(self.seg_model.value()),
            "upscale_tile": int(self.sp_tile.value()),
            "upscale_gpu": int(self.sp_gpu.value()),
            "notify_on_complete": self.sw_notify.isChecked(),
        }

    def _build_settings(self) -> Dict[str, Any]:
        s = self._ui_settings()
        s["download_folder"] = s["download_folder"] or DEFAULT_FOLDER
        s["max_images_default"] = int(self.limit_default.value())
        return s

    def _load_ui_settings(self) -> None:
        data: Dict[str, Any] = {}
        if UI_SETTINGS_FILE.exists():
            try:
                data = json.loads(UI_SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        def get(key: str, default: Any) -> Any:
            v = data.get(key, default)
            return v if isinstance(v, type(default)) or (isinstance(default, float) and isinstance(v, int)) else default

        self._set_folder(get("download_folder", DEFAULT_FOLDER) or DEFAULT_FOLDER, save=False)
        self.seg_quality.setValue(get("image_quality", "full"))
        self.sp_min_mb.setValue(get("min_size_mb", 0.0))
        self.sp_max_mb.setValue(get("max_size_mb", 1000.0))
        self.sw_subfolder.setChecked(get("auto_subfolder", True))
        self.sw_resume.setChecked(get("resume_download", True))
        self.sw_autorename.setChecked(get("auto_rename", True))
        self.ed_template.setText(get("filename_template", DEFAULT_TEMPLATE))
        self.ed_template.setEnabled(self.sw_autorename.isChecked())
        self.sw_meta.setChecked(get("export_metadata", False))
        self.sp_scroll.setValue(get("scroll_delay", 2.0))
        self.sp_dldelay.setValue(get("download_delay", 0.5))
        self.sw_upscale.setChecked(get("enable_upscale", False))
        self.seg_scale.setValue(get("upscale_scale", 3))
        self.seg_model.setValue(get("upscale_model", "auto"))
        self.sp_tile.setValue(get("upscale_tile", 200))
        self.sp_gpu.setValue(get("upscale_gpu", 0))
        self.sw_notify.setChecked(get("notify_on_complete", True))
        self._account_email = get("pinterest_email", "")
        self._update_account_view()
        appearance = get("appearance", "auto")
        self.seg_appearance.setValue(appearance)
        if appearance != "auto":
            self._on_appearance_changed(appearance)
        geometry = get("geometry", "")
        if geometry:
            try:
                self.restoreGeometry(QByteArray(base64.b64decode(geometry)))
            except Exception:
                pass

    def _save_ui_settings(self) -> None:
        data = self._ui_settings()
        data["appearance"] = self.seg_appearance.value()
        data["pinterest_email"] = self._account_email
        data["geometry"] = base64.b64encode(bytes(self.saveGeometry())).decode("ascii")
        try:
            UI_SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            self._append_log(f"⚠️ Не удалось сохранить настройки: {e}")

    def _connect_autosave(self) -> None:
        save = self._save_timer.start
        for sw in (self.sw_subfolder, self.sw_resume, self.sw_autorename, self.sw_meta, self.sw_upscale, self.sw_notify):
            sw.toggled.connect(lambda *_: save())
        for seg in (self.seg_quality, self.seg_scale, self.seg_model, self.seg_appearance):
            seg.changed.connect(lambda *_: save())
        for sp in (self.sp_min_mb, self.sp_max_mb, self.sp_scroll, self.sp_dldelay, self.sp_tile, self.sp_gpu):
            sp.valueChanged.connect(lambda *_: save())
        self.ed_template.textChanged.connect(lambda *_: save())
        self.limit_default.changed.connect(lambda *_: self._save_urls_state())
        self.sw_subfolder.toggled.connect(lambda *_: [self._request_thumb(r) for r in self._rows])

    def _set_folder(self, folder: str, save: bool = True) -> None:
        self._folder = folder
        path = Path(folder).expanduser()
        full = path if path.is_absolute() else APP_DIR / path
        home = str(Path.home())
        shown = str(full)
        if shown.startswith(home):
            shown = "~" + shown[len(home):]
        self.lbl_folder.setFullText(shown)
        self.lbl_folder.setToolTip(str(full))
        self.lbl_folder_chip.setFullText(path.name or folder)
        self.folder_chip.setToolTip(f"Открыть папку загрузки\n{full}")
        if save:
            self._save_timer.start()
            for row in self._rows:
                self._request_thumb(row)

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка для загрузок", self._folder)
        if d:
            self._set_folder(d)

    # ------------------------------------------------------------ аккаунт и «о программе»

    def _update_account_view(self) -> None:
        env_email = os.environ.get("PINTEREST_EMAIL") or os.environ.get("PINTEREST_LOGIN") or ""
        if self._account_email:
            text = f"{mask_email(self._account_email)} — пароль в Связке ключей"
        elif env_email:
            text = f"{mask_email(env_email)} — из файла .env"
        else:
            text = "Не выполнен — Pinterest может закрывать доски окном входа"
        self.lbl_account.setText(text)
        self.btn_account.setText("Изменить…" if self._account_email else "Войти…")
        self.btn_logout.setVisible(bool(self._account_email))
        self.lbl_data_dir.setText(f"Очередь, история и настройки: {Path.cwd()}")

    def _edit_account(self) -> None:
        dlg = AccountDialog(self, self._account_email)
        if dlg.exec() != QDialog.Accepted:
            return
        email, password = dlg.values()
        if not keychain_set(email, password):
            QMessageBox.warning(self, "Не удалось сохранить", "Связка ключей macOS не приняла пароль.")
            return
        if self._account_email and self._account_email != email:
            keychain_delete(self._account_email)
        self._account_email = email
        self._save_ui_settings()
        self._update_account_view()

    def _logout(self) -> None:
        if not self._account_email:
            return
        keychain_delete(self._account_email)
        if os.environ.get("PINTEREST_EMAIL") == self._account_email:
            os.environ.pop("PINTEREST_EMAIL", None)
            os.environ.pop("PINTEREST_PASSWORD", None)
        self._account_email = ""
        self._save_ui_settings()
        self._update_account_view()

    def _inject_credentials(self) -> None:
        """Парсер читает логин из окружения — подставляем его из Связки ключей."""
        if not self._account_email:
            return
        password = keychain_get(self._account_email)
        if password:
            os.environ["PINTEREST_EMAIL"] = self._account_email
            os.environ["PINTEREST_PASSWORD"] = password
        else:
            self._append_log("⚠️ Пароль Pinterest не найден в Связке ключей — войдите заново в Настройках.")

    def _about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(f"О программе {APP_NAME}")
        box.setIconPixmap(render_app_icon(72, margin=False))
        box.setText(f"<b>{APP_NAME}</b><br>Версия {APP_VERSION}")
        box.setInformativeText(
            "Скачивание досок Pinterest целиком — с очередью, историей и улучшением "
            "качества через Real-ESRGAN.\n\n"
            f"Данные приложения: {Path.cwd()}"
        )
        box.exec()

    # ------------------------------------------------------------ скачивание

    def _is_busy(self) -> bool:
        return self._downloading or self._repair_running

    def _set_busy_ui(self, busy: bool) -> None:
        downloading = self._downloading
        self.btn_start.setVisible(not downloading)
        self.btn_start.setEnabled(not busy)
        self.btn_pause.setVisible(downloading)
        self.btn_stop.setVisible(downloading)
        self.btn_repair.setEnabled(not busy)
        self.btn_clear_upscale.setEnabled(not busy)
        self.side_card.setVisible(busy)
        self._update_queue_view()

    def _sync_pending(self) -> None:
        """Передаёт движку актуальный порядок и состав ещё не начатых досок."""
        if self._downloading and self._job_queue is not None:
            self._job_queue.set_pending([r.job() for r in self._rows if r.state == "queued"])

    def _set_status(self, title: str, text: str, glyph: str, tone: str) -> None:
        self._status = (glyph, tone)
        self.lbl_status.setFullText(title)
        self.lbl_status_text.setFullText(text)
        self.lbl_side_title.setFullText(title)
        self._update_status_badge()

    def _update_status_badge(self) -> None:
        self.status_badge.set_status(*self._status)

    def _set_idle_status(self) -> None:
        self._set_status(
            "Готово к загрузке",
            "Добавьте доски в очередь и нажмите «Скачать».",
            "download",
            "idle",
        )
        self._set_percent(0, 0)

    def _set_percent(self, done: int, total: int) -> None:
        for bar in (self.bar, self.side_bar):
            bar.setRange(0, max(1, total))
            bar.setValue(min(done, total))
        pct = f"{round(done * 100 / total)}%" if total else ""
        self.lbl_pct.setText(pct)
        self.lbl_side_pct.setText(pct)

    def _update_side_info(self) -> None:
        row = self._row_by_key(self._current_key) if self._current_key else None
        parts = []
        m = re.search(r"Осталось: ([^|]+)", self._timer_text)
        if m:
            parts.append(f"Осталось: {m.group(1).strip()}")
        elif self._timer_text:
            parts.append(self._timer_text.split(" | ")[0])
        if row is not None and row.speed > 0 and not self._paused:
            parts.append(format_speed(row.speed))
        self.lbl_side_info.setFullText("  •  ".join(parts))

    def _start(self, only: Optional[List[QueueRow]] = None) -> None:
        if self._repair_running:
            QMessageBox.warning(self, "Подождите", "Сначала дождитесь окончания дозаполнения Upscale.")
            return
        if self._downloading:
            return
        pending = extract_pinterest_urls(self.ed_url.text())
        if pending and not only:
            self._add_urls(pending)
            self.ed_url.clear()
        if not self._rows:
            self._go_to(0)
            self.ed_url.setFocus()
            QMessageBox.information(
                self, "Очередь пуста", "Сначала добавьте хотя бы одну ссылку на доску Pinterest."
            )
            return

        from pinterest_download_engine import DownloadControl, JobQueue

        self._inject_credentials()
        targets = [r for r in (only or self._rows) if r in self._rows]
        self._control = DownloadControl()
        self._paused = False
        self._jobs_started = 0
        self._current_key = None
        self._timer_text = ""
        for row in targets:
            row.set_state("queued")
        self._job_queue = JobQueue([r.job() for r in targets])
        self._on_stats({"found": 0, "downloaded": 0, "failed": 0, "skipped": 0})
        self.bar.setRange(0, 0)
        self.side_bar.setRange(0, 0)
        self.lbl_pct.setText("")
        self.lbl_side_pct.setText("")
        self.bar_up.setValue(0)
        self.up_box.hide()
        self.lbl_timer.setText("")
        self.lbl_side_info.setFullText("")
        self._update_pause_button()
        self._set_status("Подготовка…", "Запускаем браузер и открываем Pinterest.", "download", "active")

        self._downloading = True
        self._worker = DownloadThread(self._build_settings(), self._job_queue, self._control, self._bridge)
        self._worker.start()
        self._set_busy_ui(True)

    def _pause(self) -> None:
        if not (self._control and self._downloading):
            return
        self._control.toggle_pause()
        self._paused = self._control.is_paused()
        self._update_pause_button()
        row = self._row_by_key(self._current_key) if self._current_key else None
        if row is not None:
            row.set_paused(self._paused)
        if self._paused:
            self._set_status(
                "Пауза", "Загрузка приостановлена — нажмите «Продолжить».", "pause", "paused"
            )
        else:
            self._set_status("Продолжаю…", "Загружаем изображения из доски.", "download", "active")
        self._update_side_info()

    def _update_pause_button(self) -> None:
        self.btn_pause.setText("Продолжить" if self._paused else "Пауза")
        self.btn_pause.setIcon(symbol_icon("play.fill" if self._paused else "pause.fill", THEME.text, 16))

    def _stop(self) -> None:
        if not (self._control and self._downloading):
            return
        self._control.request_stop()
        self._set_status("Останавливаю…", "Дожидаемся текущего файла и закрываем браузер.", "stop", "idle")
        self._append_log("Остановка…")

    @Slot(str)
    def _on_status(self, text: str) -> None:
        # Прогресс «Скачивание: i/n» рисуем сами по job_progress
        if self._paused or not self._is_busy() or text.startswith("Скачивание"):
            return
        hints = {
            "Инициализация браузера": "Запускаем браузер и открываем Pinterest.",
            "Открытие страницы": "Открываем доску.",
            "Поиск": "Прокручиваем доску и собираем ссылки на пины.",
        }
        desc = next((v for k, v in hints.items() if text.startswith(k)), "")
        self._set_status(text.rstrip("."), desc, "download", "active")

    @Slot(str)
    def _on_download_timer(self, text: str) -> None:
        self._timer_text = text
        self.lbl_timer.setText(text.replace(" | ", "  •  "))
        self._update_side_info()

    @Slot(dict)
    def _on_stats(self, stats: dict) -> None:
        self.tile_found.set_value(int(stats.get("found", 0)))
        self.tile_done.set_value(int(stats.get("downloaded", 0)))
        self.tile_skipped.set_value(int(stats.get("skipped", 0)))
        self.tile_failed.set_value(int(stats.get("failed", 0)), danger=True)

    @Slot(str, int, int)
    def _on_upscale_prog(self, text: str, value: int, maximum: int) -> None:
        self.up_box.show()
        self.lbl_up.setText(text)
        self.bar_up.setRange(0, max(1, maximum))
        self.bar_up.setValue(min(value, maximum))
        if self._is_busy() and self._current_key is None and not self._paused:
            self._set_status(text, "Улучшаем качество изображений — это может занять время.", "sparkles", "active")
            self._set_percent(value, maximum)

    @Slot(str, str)
    def _on_notify(self, title: str, msg: str) -> None:
        if self.sw_notify.isChecked():
            threading.Thread(target=_mac_notify, args=(title, msg), daemon=True).start()

    @Slot(list)
    def _on_urls_found(self, urls: object) -> None:
        if isinstance(urls, list):
            self._last_image_urls = [str(u) for u in urls]
            self.btn_export.setEnabled(bool(self._last_image_urls))

    @Slot(str)
    def _on_job_started(self, key: str) -> None:
        self._jobs_started += 1
        self._current_key = key
        self._timer_text = ""
        row = self._row_by_key(key)
        if row is not None:
            row.set_state("running")
            self.queue_scroll.ensureWidgetVisible(row)
        self._update_queue_view()

    @Slot(str, int, int, float)
    def _on_job_progress(self, key: str, done: int, total: int, speed: float) -> None:
        row = self._row_by_key(key)
        if row is None:
            return
        row.set_progress(done, total, speed)
        if key != self._current_key or self._paused:
            return
        name = row.lbl_name.fullText()
        self._set_status(
            f"Скачивание: {done}/{total} ({name})",
            "Загружаем изображения из доски. Пожалуйста, не закрывайте приложение.",
            "download",
            "active",
        )
        self._set_percent(done, total)
        self._update_side_info()

    @Slot(str, bool)
    def _on_job_finished(self, key: str, ok: bool) -> None:
        if key == self._current_key:
            self._current_key = None
        row = self._row_by_key(key)
        if row is None:
            return
        stopped = bool(self._control and self._control.should_stop())
        if stopped and (not row.total or row.done < row.total):
            row.set_state("stopped")
        else:
            row.set_state("done" if ok else "error")
        self._request_thumb(row)

    @Slot()
    def _on_worker_finished(self) -> None:
        stopped = bool(self._control and self._control.should_stop())
        self._downloading = False
        self._paused = False
        self._current_key = None
        self._job_queue = None
        self._update_pause_button()
        self.lbl_timer.setText("")
        self.lbl_timer_up.setText("")
        for row in self._rows:
            if row.state == "queued":
                row.set_state("idle")
            elif row.state == "running":
                row.set_state("stopped")
        self._set_busy_ui(False)
        done = self.tile_done.value.text()
        if stopped:
            self._set_status("Остановлено", "Уже скачанные файлы сохранены — можно докачать позже.", "stop", "idle")
        elif self._jobs_started == 0:
            self._set_status("Загрузка не началась", "Подробности — в разделе «Журнал».", "error", "error")
            self._set_percent(0, 0)
        else:
            errors = sum(1 for r in self._rows if r.state == "error")
            text = "Все доски обработаны." if not errors else (
                f"{errors} {plural(errors, 'доска', 'доски', 'досок')} с ошибкой — подробности в журнале."
            )
            self._set_status(f"Готово — скачано {done}", text, "check", "done")
            self._set_percent(1, 1)
        self._history_cache = self._read_history()
        if self.stack.currentIndex() == 2:
            self._load_history()
        self._update_queue_view()

    def _open_folder(self) -> None:
        p = self._folder or DEFAULT_FOLDER
        try:
            os.makedirs(p, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Нет доступа к папке", f"{p}\n{e}")
            return
        self._reveal(Path(p))

    def _reveal(self, path: Path) -> None:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)

    def _export_urls(self) -> None:
        if not self._last_image_urls:
            QMessageBox.information(
                self, "Пока нечего экспортировать", "Ссылки появятся после того, как начнётся скачивание."
            )
            return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить ссылки", "pinterest_urls.txt", "Текст (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self._last_image_urls))
            self._append_log(f"💾 Сохранено ссылок: {len(self._last_image_urls)} → {path}")

    # ------------------------------------------------------------ upscale: дозаполнение и очистка

    def _is_image_file(self, p: Path) -> bool:
        return p.is_file() and p.suffix.lower() in IMAGE_EXTS

    def _has_images(self, folder: Path) -> bool:
        try:
            return any(self._is_image_file(p) for p in folder.iterdir())
        except Exception:
            return False

    def _collect_download_folders_for_repair(self, root: Path) -> List[Path]:
        folders: List[Path] = []
        seen: set[Path] = set()
        for dirpath, dirnames, _filenames in os.walk(root):
            current = Path(dirpath)
            # Не спускаемся в служебные папки
            dirnames[:] = [
                d for d in dirnames if not d.startswith(".") and d != ".upscale_missing_input"
            ]
            if current.name.lower() == "upscale":
                continue
            if self._has_images(current) and current not in seen:
                folders.append(current)
                seen.add(current)
        return sorted(folders)

    def _find_missing_upscale_files(self, folder: Path) -> List[Path]:
        originals = [p for p in sorted(folder.iterdir()) if self._is_image_file(p)]
        up_dir = folder / "upscale"
        if not up_dir.exists():
            return originals
        up_stems = {p.stem.lower() for p in up_dir.iterdir() if self._is_image_file(p)}
        missing: List[Path] = []
        for src in originals:
            stem = src.stem.lower()
            found = any(
                s == stem or s.startswith(f"{stem}_") or s.startswith(f"{stem}-")
                for s in up_stems
            )
            if not found:
                missing.append(src)
        return missing

    def _start_upscale_repair(self) -> None:
        if self._is_busy():
            QMessageBox.information(self, "Подождите", "Дождитесь окончания текущей задачи.")
            return
        root = Path(self._folder or DEFAULT_FOLDER)
        if not root.exists():
            QMessageBox.warning(self, "Папка не найдена", str(root))
            return
        if root.name.lower() == "upscale" and root.parent.exists():
            # Если выбрана папка upscale, берём родительскую папку доски
            root = root.parent
            self._append_log(f"ℹ️ Выбрана папка upscale, проверяю родительскую папку: {root}")

        self._repair_running = True
        self._set_busy_ui(True)
        self._set_status("Проверка Upscale…", "Ищем изображения без улучшенной версии.", "sparkles", "active")
        settings = self._build_settings()
        settings["download_folder"] = str(root)
        threading.Thread(target=self._run_upscale_repair, args=(settings,), daemon=True).start()

    def _run_upscale_repair(self, settings: Dict[str, Any]) -> None:
        try:
            from pinterest_download_engine import (
                DownloadControl,
                DownloadSettings,
                PinterestDownloadEngine,
            )

            root = Path(settings["download_folder"])
            folders = self._collect_download_folders_for_repair(root)
            if not folders:
                self._bridge.log.emit("⚠️ Не найдено папок с изображениями для проверки Upscale.")
                return

            self._bridge.log.emit("\n=== Дозаполнение Upscale ===")
            eng = PinterestDownloadEngine(
                DownloadSettings(**settings),
                DownloadControl(),
                urls_discovered=lambda _urls: None,
                **_engine_callbacks(self._bridge),
            )

            total_missing = 0
            total_done = 0
            for idx, folder in enumerate(folders, start=1):
                self._bridge.progress_status.emit(f"Проверка Upscale: {idx}/{len(folders)}")
                missing = self._find_missing_upscale_files(folder)
                if not missing:
                    self._bridge.log.emit(f"✓ {folder.name}: все файлы уже имеют upscale")
                    continue

                total_missing += len(missing)
                self._bridge.log.emit(
                    f"🔎 {folder.name}: отсутствует upscale для {len(missing)} файлов — запускаю дозаполнение"
                )

                temp_in = folder / ".upscale_missing_input"
                if temp_in.exists():
                    shutil.rmtree(temp_in, ignore_errors=True)
                temp_in.mkdir(parents=True, exist_ok=True)
                for src in missing:
                    shutil.copy2(src, temp_in / src.name)

                ok = eng.run_upscale(str(temp_in))
                produced_dir = temp_in / "upscale"
                target_up = folder / "upscale"
                target_up.mkdir(exist_ok=True)
                moved = 0
                if ok and produced_dir.exists():
                    for out in produced_dir.iterdir():
                        if self._is_image_file(out):
                            shutil.move(str(out), str(target_up / out.name))
                            moved += 1
                total_done += moved
                shutil.rmtree(temp_in, ignore_errors=True)

                if ok:
                    self._bridge.log.emit(f"✅ {folder.name}: добавлено upscale файлов: {moved}")
                else:
                    self._bridge.log.emit(f"❌ {folder.name}: дозаполнение не удалось")
                    break

            self._bridge.log.emit(
                f"=== Дозаполнение завершено: найдено недостающих {total_missing}, создано {total_done} ==="
            )
        except Exception as e:
            self._bridge.log.emit(f"❌ Ошибка дозаполнения Upscale: {e}")
        finally:
            self._bridge.repair_finished.emit()

    @Slot()
    def _on_repair_finished(self) -> None:
        self._repair_running = False
        self._set_busy_ui(False)
        self.lbl_timer_up.setText("")
        self._set_status("Дозаполнение Upscale завершено", "Подробности — в разделе «Журнал».", "check", "done")

    def _clear_upscale_outputs(self) -> None:
        if self._is_busy():
            QMessageBox.information(self, "Подождите", "Дождитесь окончания текущей задачи.")
            return
        root = Path(self._folder or DEFAULT_FOLDER)
        if not root.exists():
            QMessageBox.warning(self, "Папка не найдена", str(root))
            return

        confirm = QMessageBox.question(
            self,
            "Удалить результаты upscale?",
            "Будут удалены все улучшенные изображения из папок upscale.\n"
            "Оригинальные файлы останутся на месте.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return

        deleted = 0
        touched_dirs = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            cur = Path(dirpath)
            if cur.name.lower() != "upscale":
                continue
            removed_here = 0
            for name in filenames:
                p = cur / name
                if self._is_image_file(p):
                    try:
                        p.unlink()
                        deleted += 1
                        removed_here += 1
                    except Exception as e:
                        self._append_log(f"⚠️ Не удалось удалить {p}: {e}")
            if removed_here > 0:
                touched_dirs += 1

        self._append_log(
            f"🧹 Очистка Upscale завершена: удалено {deleted} файлов в {touched_dirs} папках."
        )
        QMessageBox.information(
            self,
            "Готово",
            f"Удалено {deleted} {plural(deleted, 'изображение', 'изображения', 'изображений')} "
            f"из {touched_dirs} {plural(touched_dirs, 'папки', 'папок', 'папок')} upscale.",
        )

    # ------------------------------------------------------------ журнал

    @Slot(str)
    def _append_log(self, s: str) -> None:
        self.log.appendPlainText(s)

    def _copy_log(self) -> None:
        QGuiApplication.clipboard().setText(self.log.toPlainText())

    # ------------------------------------------------------------ drag & drop, закрытие

    def _dropped_urls(self, event) -> List[str]:
        md = event.mimeData()
        text = "\n".join(u.toString() for u in md.urls()) if md.hasUrls() else ""
        if md.hasText():
            text += "\n" + md.text()
        return extract_pinterest_urls(text)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if self._dropped_urls(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        urls = self._dropped_urls(event)
        if urls:
            event.acceptProposedAction()
            self._go_to(0)
            self._add_urls(urls)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_urls_state()
        self._save_ui_settings()
        if self._control:
            self._control.request_stop()
        if self._worker and self._worker.isRunning():
            self._worker.wait(3000)
        self._pool.shutdown(wait=False)
        event.accept()


def apply_theme(app: QApplication) -> None:
    global THEME
    dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    THEME = DARK if dark else LIGHT
    app.setStyleSheet(build_qss(THEME))


def self_test() -> int:
    """`--self-test`: проверка, что в сборке на месте всё нужное для работы (для build_macos.sh)."""
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    report: Dict[str, Any] = {"version": APP_VERSION, "frozen": FROZEN, "data_dir": str(data_dir())}
    required: Dict[str, bool] = {}
    try:
        import pinterest_download_engine as engine
        from selenium.webdriver.common.selenium_manager import SeleniumManager

        required["engine"] = True
        manager = SeleniumManager._get_binary()
        report["selenium_manager"] = str(manager)
        required["selenium_manager"] = Path(manager).is_file()
        exe = engine.find_upscale_binary()
        report["upscale_binary"] = str(exe) if exe else None
        if exe:
            out = subprocess.run([str(exe), "-h"], capture_output=True, text=True, timeout=20)
            required["upscale_runs"] = "Usage" in (out.stdout + out.stderr)
            models = engine.PinterestDownloadEngine._find_models_dir(None, exe)  # type: ignore[arg-type]
            report["upscale_models"] = str(models) if models else None
            required["upscale_models"] = models is not None
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"
        required["engine"] = False
    required["jpeg_thumbnails"] = b"jpeg" in [bytes(f) for f in QImageReader.supportedImageFormats()]
    report["sf_symbols"] = not QIcon.fromTheme("folder").isNull()
    report["checks"] = required
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if required and all(required.values()) else 1


def main() -> None:
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    # Очередь, история и настройки: из исходников — рядом с кодом,
    # в собранном .app — в ~/Library/Application Support/Pinterest Downloader
    target = data_dir()
    target.mkdir(parents=True, exist_ok=True)
    moved = migrate_legacy_data(target) if FROZEN else []
    os.chdir(target)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_TITLE)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(make_app_icon())
    apply_theme(app)
    w = MainWindow()
    if moved:
        w._append_log("📦 Перенесено из папки проекта: " + ", ".join(moved))
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
