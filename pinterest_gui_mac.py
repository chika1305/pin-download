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
        QObject,
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
        QDoubleSpinBox,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QHeaderView,
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
    )
except ImportError as e:
    print(
        "Нужен PySide6:  pip install PySide6\n"
        "На macOS: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from e


APP_DIR = Path(__file__).resolve().parent
APP_TITLE = "Pinterest Image Downloader"
SAVED_URLS_FILE = Path("saved_urls.json")
UI_SETTINGS_FILE = Path("ui_settings.json")
HISTORY_FILE = Path("download_history.json")
DEFAULT_FOLDER = "pinterest_images"
DEFAULT_TEMPLATE = "{index04}_{hash}.jpg"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
SIDEBAR_WIDTH = 224
THUMB_SIZE = 40
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


def load_square_thumbnail(path: Path, px: int) -> Optional[QImage]:
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        scale = max(px / size.width(), px / size.height())
        reader.setScaledSize(
            QSize(math.ceil(size.width() * scale), math.ceil(size.height() * scale))
        )
    img = reader.read()
    if img.isNull():
        return None
    side = min(img.width(), img.height())
    return img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)


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
    segment: QColor
    accent: QColor
    accent_hover: QColor
    accent_soft: QColor
    on_accent: QColor
    success: QColor
    warning: QColor
    danger: QColor
    track: QColor


LIGHT = Theme(
    dark=False,
    sidebar=_c("#E9E9ED"),
    content=_c("#F5F5F7"),
    card=_c("#FFFFFF"),
    border=_c("#000000", 16),
    separator=_c("#000000", 22),
    text=_c("#1D1D1F"),
    secondary=_c("#6E6E73"),
    tertiary=_c("#A1A1A6"),
    field=_c("#FFFFFF"),
    field_border=_c("#000000", 34),
    hover=_c("#000000", 12),
    pressed=_c("#000000", 26),
    control=_c("#000000", 15),
    segment=_c("#FFFFFF"),
    accent=_c("#E60023"),
    accent_hover=_c("#C8001E"),
    accent_soft=_c("#E60023", 26),
    on_accent=_c("#FFFFFF"),
    success=_c("#34C759"),
    warning=_c("#FF9500"),
    danger=_c("#FF3B30"),
    track=_c("#000000", 22),
)

DARK = Theme(
    dark=True,
    sidebar=_c("#242427"),
    content=_c("#1C1C1E"),
    card=_c("#2B2B2E"),
    border=_c("#FFFFFF", 18),
    separator=_c("#FFFFFF", 22),
    text=_c("#F5F5F7"),
    secondary=_c("#A1A1A6"),
    tertiary=_c("#6C6C70"),
    field=_c("#FFFFFF", 14),
    field_border=_c("#FFFFFF", 36),
    hover=_c("#FFFFFF", 16),
    pressed=_c("#FFFFFF", 30),
    control=_c("#FFFFFF", 24),
    segment=_c("#636366"),
    accent=_c("#FF3B55"),
    accent_hover=_c("#FF5C71"),
    accent_soft=_c("#FF3B55", 44),
    on_accent=_c("#FFFFFF"),
    success=_c("#30D158"),
    warning=_c("#FF9F0A"),
    danger=_c("#FF453A"),
    track=_c("#FFFFFF", 30),
)

THEME = LIGHT


def css(color: QColor) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


QSS_TEMPLATE = """
QWidget#sidebar, QWidget#page, QWidget#scrollBody, QWidget#queueBody,
QStackedWidget#stack { background: transparent; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }

QLabel#appName { font-size: 13px; font-weight: 600; color: @text; }
QLabel#appCaption { font-size: 11px; color: @secondary; }
QLabel#largeTitle { font-size: 26px; font-weight: 700; color: @text; }
QLabel#subtitle { font-size: 13px; color: @secondary; }
QLabel#sectionHeader { font-size: 13px; font-weight: 600; color: @text; }
QLabel#muted { font-size: 12px; color: @secondary; }
QLabel#rowTitle { font-size: 13px; color: @text; }
QLabel#rowTitleStrong { font-size: 13px; font-weight: 600; color: @text; }
QLabel#rowSubtitle { font-size: 11px; color: @secondary; }
QLabel#statusTitle { font-size: 14px; font-weight: 600; color: @text; }
QLabel#statValue { font-size: 22px; font-weight: 600; color: @text; }
QLabel#statValue[tone="danger"] { color: @danger; }
QLabel#statCaption { font-size: 11px; color: @secondary; }
QLabel#emptyTitle { font-size: 17px; font-weight: 600; color: @text; }
QLabel#emptyText { font-size: 13px; color: @secondary; }
QLabel#sideStatus { font-size: 11px; color: @secondary; }
QLabel#bannerText { font-size: 12px; color: @text; }

QFrame#card { background: @card; border: 1px solid @border; border-radius: 12px; }

QPushButton#sidebarItem {
    text-align: left; padding: 0 10px; border: none; border-radius: 7px;
    font-size: 13px; color: @text; background: transparent;
}
QPushButton#sidebarItem:hover { background: @hover; }
QPushButton#sidebarItem:checked { background: @accent; color: @on_accent; }
QPushButton#folderChip {
    text-align: left; padding: 0 8px; border: none; border-radius: 7px;
    font-size: 12px; color: @secondary; background: transparent;
}
QPushButton#folderChip:hover { background: @hover; color: @text; }

QPushButton#primary {
    background: @accent; color: @on_accent; border: none; border-radius: 15px;
    padding: 0 18px; min-height: 30px; font-size: 13px; font-weight: 600;
}
QPushButton#primary:hover { background: @accent_hover; }
QPushButton#primary:disabled { background: @control; color: @tertiary; }
QPushButton#secondary, QPushButton#destructive {
    background: @control; color: @text; border: none; border-radius: 15px;
    padding: 0 16px; min-height: 30px; font-size: 13px;
}
QPushButton#destructive { color: @danger; }
QPushButton#secondary:hover, QPushButton#destructive:hover { background: @pressed; }
QPushButton#secondary:disabled, QPushButton#destructive:disabled { color: @tertiary; }
QPushButton#primary[size="large"], QPushButton#secondary[size="large"] {
    min-height: 38px; border-radius: 19px;
}
QPushButton#primary[size="small"], QPushButton#secondary[size="small"],
QPushButton#destructive[size="small"] {
    min-height: 24px; border-radius: 12px; padding: 0 12px; font-size: 12px;
}
QPushButton#link {
    background: transparent; border: none; color: @accent; font-size: 12px; padding: 2px 4px;
}
QPushButton#link:hover { color: @accent_hover; }
QPushButton#link:disabled { color: @tertiary; }
QToolButton#iconButton { background: transparent; border: none; border-radius: 12px; padding: 3px; }
QToolButton#iconButton:hover { background: @hover; }

QLineEdit#urlField {
    background: @field; border: 1px solid @field_border; border-radius: 19px;
    padding: 0 12px; min-height: 36px; font-size: 14px; color: @text;
}
QLineEdit#urlField:focus { border: 2px solid @accent; padding: 0 11px; }
QLineEdit#field {
    background: @field; border: 1px solid @field_border; border-radius: 7px;
    padding: 3px 8px; font-size: 13px; color: @text;
}
QLineEdit#field:focus { border: 2px solid @accent; padding: 2px 7px; }

QProgressBar { background: @track; border: none; border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { background: @accent; border-radius: 3px; }
QProgressBar#thin { border-radius: 2px; min-height: 4px; max-height: 4px; }
QProgressBar#thin::chunk { border-radius: 2px; }

QPlainTextEdit#log {
    background: @card; border: 1px solid @border; border-radius: 12px;
    padding: 10px; color: @text; font-size: 12px;
}
QTreeWidget#history {
    background: @card; border: 1px solid @border; border-radius: 12px;
    padding: 6px; color: @text; font-size: 13px; outline: 0;
}
QTreeWidget#history::item { min-height: 32px; border: none; padding: 0 6px; }
QTreeWidget#history::item:hover { background: @hover; }
QTreeWidget#history::item:selected { background: @accent; color: @on_accent; }
QTreeWidget#history QHeaderView::section {
    background: transparent; color: @secondary; border: none;
    border-bottom: 1px solid @separator; padding: 4px 6px 6px 6px;
    font-size: 11px; font-weight: 600;
}
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


# ---------------------------------------------------------------- иконки (SF Symbols)

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

    w, h, x0, y0 = r.width(), r.height(), r.left(), r.top()
    pen = QPen(QColor("white"), w * 0.085, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    cx = x0 + w / 2
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
    p.end()
    return pm


def make_app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 32, 64, 128, 256, 512):
        icon.addPixmap(render_app_icon(size))
    return icon


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
        return QSize(40, 24)

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
        track = QRectF(0, (self.height() - 22) / 2, 38, 22)
        p.setPen(Qt.NoPen)
        p.setBrush(blend(THEME.track, THEME.accent, self._pos))
        p.drawRoundedRect(track, 11, 11)
        d = 18.0
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
        f.setPixelSize(12)
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
        seg = max(max(fm.horizontalAdvance(label) for _, label in self._options) + 28, 52)
        return QSize(seg * len(self._options) + 4, 26)

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
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        seg_w = self._segment_width()
        sel = QRectF(2 + self._anim_pos * seg_w, 2, seg_w, r.height() - 4)
        if not THEME.dark:
            p.setBrush(QColor(0, 0, 0, 28))
            p.drawRoundedRect(sel.translated(0, 0.6), sel.height() / 2, sel.height() / 2)
        p.setBrush(THEME.segment)
        p.drawRoundedRect(sel, sel.height() / 2, sel.height() / 2)
        for i, (_, label) in enumerate(self._options):
            seg = QRectF(2 + i * seg_w, 0, seg_w, r.height())
            weight = max(0.0, 1.0 - abs(self._anim_pos - i))
            f = self.font()
            f.setWeight(QFont.DemiBold if weight > 0.5 else QFont.Normal)
            p.setFont(f)
            p.setPen(blend(THEME.secondary, THEME.text, weight))
            p.drawText(seg, Qt.AlignCenter, label)


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
        if event.type() == event.Type.FontChange:
            self._refresh()

    def _refresh(self) -> None:
        super().setText(self.fontMetrics().elidedText(self._full, self._mode, max(0, self.width())))


class Thumbnail(QWidget):
    def __init__(self, size: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
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
        path.addRoundedRect(r, 8, 8)
        if self._pm is not None:
            p.setClipPath(path)
            p.drawPixmap(self.rect(), self._pm)
            p.setClipping(False)
        else:
            p.fillPath(path, THEME.control)
            icon = symbol_pixmap("photo", THEME.tertiary, 18)
            if not icon.isNull():
                p.drawPixmap(QPointF((self.width() - 18) / 2, (self.height() - 18) / 2), icon)
        p.setPen(QPen(THEME.border, 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)


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
            self._lay.addWidget(Separator(inset=14))
        row = QWidget()
        row.setMinimumHeight(44)
        h = QHBoxLayout(row)
        h.setContentsMargins(14, 9, 14, 9)
        h.setSpacing(16)
        col = QVBoxLayout()
        col.setSpacing(2)
        lt = QLabel(title)
        lt.setObjectName("rowTitle")
        col.addWidget(lt)
        ls = None
        if subtitle is not None:
            ls = QLabel(subtitle)
            ls.setObjectName("rowSubtitle")
            ls.setWordWrap(True)
            col.addWidget(ls)
        h.addLayout(col, 1)
        if control is not None:
            h.addWidget(control, 0, Qt.AlignRight | Qt.AlignVCenter)
        self._lay.addWidget(row)
        self._rows += 1
        return row, lt, ls


class QueueRow(QWidget):
    remove_clicked = Signal(object)
    limit_changed = Signal(object)
    context_requested = Signal(object, object)

    def __init__(self, data: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.data = data
        self.state = "idle"
        self.first = True
        self.image_count = 0
        self.lookup_done = bool(data.get("board_name"))
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 12, 10)
        h.setSpacing(12)
        self.thumb = Thumbnail(THUMB_SIZE)
        h.addWidget(self.thumb)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.lbl_name = ElidedLabel(mode=Qt.ElideRight)
        self.lbl_name.setObjectName("rowTitleStrong")
        self.lbl_sub = ElidedLabel()
        self.lbl_sub.setObjectName("rowSubtitle")
        col.addWidget(self.lbl_name)
        col.addWidget(self.lbl_sub)
        h.addLayout(col, 1)
        self.lbl_state = QLabel()
        self.lbl_state.setFixedSize(18, 18)
        self.lbl_state.hide()
        h.addWidget(self.lbl_state)
        self.sp_limit = QSpinBox()
        self.sp_limit.setRange(0, 100000)
        self.sp_limit.setSpecialValueText("Все")
        self.sp_limit.setValue(int(data.get("max_images") or 0))
        self.sp_limit.setFixedWidth(84)
        self.sp_limit.setToolTip("Сколько изображений скачать с этой доски (Все — без ограничения)")
        self.sp_limit.valueChanged.connect(self._on_limit)
        h.addWidget(self.sp_limit)
        self.btn_remove = QToolButton()
        self.btn_remove.setObjectName("iconButton")
        self.btn_remove.setToolTip("Убрать из очереди")
        self.btn_remove.setIconSize(QSize(16, 16))
        self.btn_remove.clicked.connect(lambda: self.remove_clicked.emit(self))
        h.addWidget(self.btn_remove)
        self.refresh()
        self.retheme()

    def _on_limit(self, value: int) -> None:
        self.data["max_images"] = int(value)
        self.limit_changed.emit(self)

    def set_first(self, first: bool) -> None:
        self.first = first
        self.update()

    def set_state(self, state: str) -> None:
        self.state = state
        self.refresh()
        self.retheme()

    def set_image_count(self, count: int) -> None:
        self.image_count = count
        self.refresh()

    def refresh(self) -> None:
        name = display_board(self.data.get("board_name"))
        if not name:
            fallback = pretty_url(self.data["url"]).split("/")[-1] or "Без названия"
            name = fallback if self.lookup_done else "Определяю название…"
        self.lbl_name.setFullText(name)
        parts = [pretty_url(self.data["url"])]
        if self.state == "running":
            parts = ["Скачивается…"]
        elif self.image_count:
            parts.append(
                f"{self.image_count} {plural(self.image_count, 'файл', 'файла', 'файлов')} в папке"
            )
        self.lbl_sub.setFullText("  ·  ".join(parts))
        self.setToolTip(self.data["url"])

    def retheme(self) -> None:
        self.btn_remove.setIcon(symbol_icon("xmark.circle.fill", THEME.tertiary, 16))
        symbol, color = {
            "running": ("arrow.down.circle.fill", THEME.accent),
            "done": ("checkmark.circle.fill", THEME.success),
            "error": ("exclamationmark.triangle.fill", THEME.warning),
        }.get(self.state, ("", THEME.tertiary))
        if symbol:
            self.lbl_state.setPixmap(symbol_pixmap(symbol, color, 18))
            self.lbl_state.show()
        else:
            self.lbl_state.hide()
        self.update()

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        self.context_requested.emit(self, event.globalPos())

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        if self.state == "running":
            p.fillRect(self.rect(), THEME.accent_soft)
        if not self.first:
            inset = 14 + THUMB_SIZE + 12
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
        tone = "danger" if danger and n > 0 else ""
        if self.value.property("tone") != tone:
            self.value.setProperty("tone", tone)
            self.value.style().unpolish(self.value)
            self.value.style().polish(self.value)


class RootView(QWidget):
    """Фон окна: боковая панель на всю высоту (в том числе под titlebar) и область контента."""

    titlebar_height = 0

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.fillRect(self.rect(), THEME.content)
        p.fillRect(QRect(0, 0, SIDEBAR_WIDTH, self.height()), THEME.sidebar)
        p.fillRect(QRect(SIDEBAR_WIDTH, 0, 1, self.height()), THEME.separator)

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
    job_started = Signal(int)
    job_finished = Signal(int, bool)
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
    def __init__(
        self,
        settings: Dict[str, Any],
        url_jobs: List[Dict[str, Any]],
        control: Any,
        bridge: Bridge,
    ):
        super().__init__()
        self.settings = settings
        self.url_jobs = url_jobs
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
                **_engine_callbacks(self.bridge),
            )
            eng.run_multi(self.url_jobs)
        except Exception as e:
            self.bridge.log.emit(f"❌ Ошибка: {e}\n{traceback.format_exc()}")
        finally:
            self.bridge.finished.emit()


# ---------------------------------------------------------------- окно


class MainWindow(QMainWindow):
    PAGES = [
        ("download", "Загрузка", "arrow.down.circle"),
        ("upscale", "Upscale", "sparkles"),
        ("history", "История", "clock.arrow.circlepath"),
        ("settings", "Настройки", "gearshape"),
        ("log", "Журнал", "text.alignleft"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(880, 600)
        self.resize(1060, 740)
        self.setAcceptDrops(True)
        if hasattr(Qt.WindowType, "ExpandedClientAreaHint"):
            # Контент под прозрачным titlebar — боковая панель на всю высоту окна
            self.setWindowFlag(Qt.WindowType.ExpandedClientAreaHint, True)
            self.setWindowFlag(Qt.WindowType.NoTitleBarBackgroundHint, True)
            self.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)

        self._bridge = Bridge()
        self._control: Any = None
        self._worker: Optional[DownloadThread] = None
        self._repair_running = False
        self._downloading = False
        self._paused = False
        self._last_image_urls: List[str] = []
        self._url_rows: List[Dict[str, Any]] = []
        self._rows: List[QueueRow] = []
        self._job_urls: List[str] = []
        self._retheme_fns: List[Callable[[], None]] = []
        self._folder = DEFAULT_FOLDER
        self._upscale_exe: Optional[str] = None
        self._engine_loaded = False
        self._safe_area_hooked = False
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ui-bg")
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_ui_settings)

        self._build_ui()
        self._build_menu()
        self._wire_bridge()
        self._load_ui_settings()
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
        kind: str = "secondary",
        symbol: Optional[str] = None,
        size: Optional[str] = None,
        slot: Optional[Callable] = None,
    ) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName(kind)
        btn.setCursor(Qt.PointingHandCursor)
        if size:
            btn.setProperty("size", size)
        if symbol:
            btn.setIconSize(QSize(14, 14))

            def apply(b: QPushButton = btn, k: str = kind, s: str = symbol) -> None:
                color = {
                    "primary": THEME.on_accent,
                    "destructive": THEME.danger,
                    "link": THEME.accent,
                }.get(k, THEME.text)
                b.setIcon(symbol_icon(s, color, 14))

            self._themed(apply)
        if slot:
            btn.clicked.connect(slot)
        return btn

    def _page(
        self, title: str, subtitle: str = "", scroll: bool = False
    ) -> Tuple[QWidget, QVBoxLayout, QHBoxLayout, QLabel]:
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(28, 6, 28, 22)
        v.setSpacing(16)
        head = QHBoxLayout()
        head.setSpacing(8)
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        tcol.addWidget(self._label(title, "largeTitle"))
        sub = self._label(subtitle, "subtitle", wrap=True)
        tcol.addWidget(sub)
        head.addLayout(tcol, 1)
        actions = QHBoxLayout()
        actions.setSpacing(8)
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

    def _section(self, text: str, top: int = 10) -> QWidget:
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
        self._themed(lambda: icon.setPixmap(symbol_pixmap(symbol, THEME.tertiary, 44)))
        v.addWidget(icon)
        v.addSpacing(6)
        t = self._label(title, "emptyTitle")
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        d = self._label(text, "emptyText", wrap=True)
        d.setAlignment(Qt.AlignCenter)
        v.addWidget(d)
        v.addStretch()
        return w

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
        v.setContentsMargins(12, 10, 12, 12)
        v.setSpacing(2)

        brand = QHBoxLayout()
        brand.setContentsMargins(6, 0, 0, 0)
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(render_app_icon(32, margin=False))
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(self._label("Pinterest", "appName"))
        names.addWidget(self._label("Image Downloader", "appCaption"))
        brand.addLayout(names, 1)
        v.addLayout(brand)
        v.addSpacing(18)

        self._nav = QButtonGroup(self)
        self._nav.setExclusive(True)
        for i, (_key, title, symbol) in enumerate(self.PAGES):
            btn = QPushButton(title)
            btn.setObjectName("sidebarItem")
            btn.setCheckable(True)
            btn.setFixedHeight(30)
            btn.setIconSize(QSize(16, 16))
            shortcut = QKeySequence(f"Ctrl+{i + 1}").toString(QKeySequence.NativeText)
            btn.setToolTip(f"{title}  {shortcut}")
            self._themed(
                lambda b=btn, s=symbol: b.setIcon(symbol_icon(s, THEME.accent, 16, THEME.on_accent))
            )
            self._nav.addButton(btn, i)
            v.addWidget(btn)
        self._nav.idClicked.connect(self._go_to)

        v.addStretch(1)

        self.side_activity = QWidget()
        sa = QVBoxLayout(self.side_activity)
        sa.setContentsMargins(8, 0, 8, 8)
        sa.setSpacing(6)
        self.lbl_side_status = ElidedLabel(mode=Qt.ElideRight)
        self.lbl_side_status.setObjectName("sideStatus")
        sa.addWidget(self.lbl_side_status)
        self.side_bar = QProgressBar()
        self.side_bar.setObjectName("thin")
        self.side_bar.setTextVisible(False)
        sa.addWidget(self.side_bar)
        self.side_activity.hide()
        v.addWidget(self.side_activity)

        self.btn_folder_chip = QPushButton()
        self.btn_folder_chip.setObjectName("folderChip")
        self.btn_folder_chip.setFixedHeight(28)
        self.btn_folder_chip.setIconSize(QSize(14, 14))
        self.btn_folder_chip.setCursor(Qt.PointingHandCursor)
        self.btn_folder_chip.clicked.connect(self._open_folder)
        self._themed(
            lambda: self.btn_folder_chip.setIcon(symbol_icon("folder", THEME.secondary, 14))
        )
        v.addWidget(self.btn_folder_chip)
        return side

    # --- Загрузка

    def _build_download_page(self) -> QWidget:
        page, body, actions, self.lbl_dl_subtitle = self._page("Загрузка")

        self.btn_pause = self._button("Пауза", symbol="pause.fill", slot=self._pause)
        self.btn_stop = self._button("Остановить", symbol="stop.fill", slot=self._stop)
        self.btn_start = self._button("Скачать", "primary", "arrow.down", slot=self._start)
        self.btn_start.setToolTip(
            "Скачать все доски из очереди  "
            + QKeySequence("Ctrl+Return").toString(QKeySequence.NativeText)
        )
        self.btn_pause.hide()
        self.btn_stop.hide()
        actions.addWidget(self.btn_pause)
        actions.addWidget(self.btn_stop)
        actions.addWidget(self.btn_start)

        url_row = QHBoxLayout()
        url_row.setSpacing(10)
        self.ed_url = QLineEdit()
        self.ed_url.setObjectName("urlField")
        self.ed_url.setAttribute(Qt.WA_MacShowFocusRect, False)
        self.ed_url.setPlaceholderText("Вставьте ссылку на доску Pinterest или pin.it")
        self.ed_url.setClearButtonEnabled(True)
        self.ed_url.returnPressed.connect(self._add_from_field)
        link_action = self.ed_url.addAction(QIcon(), QLineEdit.LeadingPosition)
        paste_action = self.ed_url.addAction(QIcon(), QLineEdit.TrailingPosition)
        paste_action.setToolTip("Добавить ссылки из буфера обмена")
        paste_action.triggered.connect(self._paste_from_clipboard)
        self._themed(lambda: link_action.setIcon(symbol_icon("link", THEME.secondary, 16)))
        self._themed(
            lambda: paste_action.setIcon(symbol_icon("doc.on.clipboard", THEME.secondary, 16))
        )
        url_row.addWidget(self.ed_url, 1)

        url_row.addWidget(self._label("Лимит", "muted"))
        self.sp_default_max = QSpinBox()
        self.sp_default_max.setRange(0, 100000)
        self.sp_default_max.setSpecialValueText("Все")
        self.sp_default_max.setFixedWidth(84)
        self.sp_default_max.setToolTip("Сколько изображений брать с новых досок (Все — без ограничения)")
        url_row.addWidget(self.sp_default_max)
        self.btn_add = self._button("Добавить", "secondary", "plus", "large", self._add_from_field)
        url_row.addWidget(self.btn_add)
        body.addLayout(url_row)

        qh = QHBoxLayout()
        qh.setContentsMargins(4, 4, 0, 0)
        qh.setSpacing(6)
        qh.addWidget(self._label("Очередь", "sectionHeader"))
        self.lbl_queue_count = self._label("", "muted")
        qh.addWidget(self.lbl_queue_count)
        qh.addStretch()
        self.btn_refresh_names = self._button("Обновить названия", "link", slot=self._refresh_names)
        self.btn_clear_queue = self._button("Очистить", "link", slot=self._clear_urls)
        qh.addWidget(self.btn_refresh_names)
        qh.addWidget(self.btn_clear_queue)
        body.addLayout(qh)

        self.queue_card = QFrame()
        self.queue_card.setObjectName("card")
        qc = QVBoxLayout(self.queue_card)
        qc.setContentsMargins(1, 1, 1, 1)
        qc.setSpacing(0)
        self.queue_scroll = QScrollArea()
        self.queue_scroll.setWidgetResizable(True)
        self.queue_scroll.setFrameShape(QFrame.NoFrame)
        self.queue_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        qbody = QWidget()
        qbody.setObjectName("queueBody")
        self.rows_box = QVBoxLayout(qbody)
        self.rows_box.setContentsMargins(0, 2, 0, 2)
        self.rows_box.setSpacing(0)
        self.rows_box.addStretch()
        self.queue_scroll.setWidget(qbody)
        qc.addWidget(self.queue_scroll)
        self.queue_empty = self._empty_state(
            "square.and.arrow.down.on.square",
            "Очередь пуста",
            "Вставьте ссылку на доску Pinterest в поле выше\nили просто перетащите её в окно.",
        )
        qc.addWidget(self.queue_empty)
        body.addWidget(self.queue_card, 1)

        body.addWidget(self._build_activity_card())
        return page

    def _build_activity_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.lbl_status_icon = QLabel()
        self.lbl_status_icon.setFixedSize(20, 20)
        top.addWidget(self.lbl_status_icon)
        self.lbl_status = ElidedLabel(mode=Qt.ElideRight)
        self.lbl_status.setObjectName("statusTitle")
        top.addWidget(self.lbl_status, 1)
        self.lbl_timer = self._label("", "muted")
        top.addWidget(self.lbl_timer)
        v.addLayout(top)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        v.addWidget(self.bar)

        self.up_box = QWidget()
        ub = QVBoxLayout(self.up_box)
        ub.setContentsMargins(0, 2, 0, 0)
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
        bottom.setSpacing(28)
        self.tile_found = StatTile("Найдено")
        self.tile_done = StatTile("Скачано")
        self.tile_skipped = StatTile("Пропущено")
        self.tile_failed = StatTile("Ошибки")
        for tile in (self.tile_found, self.tile_done, self.tile_skipped, self.tile_failed):
            bottom.addWidget(tile)
        bottom.addStretch()
        links = QVBoxLayout()
        links.setSpacing(0)
        links.addWidget(
            self._button("Открыть папку", "link", slot=self._open_folder), 0, Qt.AlignRight
        )
        self.btn_export = self._button("Экспорт ссылок…", "link", slot=self._export_urls)
        self.btn_export.setEnabled(False)
        links.addWidget(self.btn_export, 0, Qt.AlignRight)
        bottom.addLayout(links)
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
        self.sp_tile.setFixedWidth(90)
        g.add_row("Размер тайла", self.sp_tile, "Меньше значение — меньше нужно видеопамяти")
        self.sp_gpu = QSpinBox()
        self.sp_gpu.setRange(0, 10)
        self.sp_gpu.setFixedWidth(90)
        g.add_row("Видеокарта", self.sp_gpu, "Номер GPU; 0 — первая")
        body.addWidget(g)

        body.addWidget(self._section("Real-ESRGAN"))
        g2 = Group()
        self.lbl_esrgan_icon = QLabel()
        self.lbl_esrgan_icon.setFixedSize(20, 20)
        _row, self.lbl_esrgan_title, self.lbl_esrgan_sub = g2.add_row(
            "Проверяю…", self.lbl_esrgan_icon, ""
        )
        body.addWidget(g2)

        body.addWidget(self._section("Инструменты"))
        g3 = Group()
        self.btn_repair = self._button(
            "Запустить", size="small", slot=self._start_upscale_repair
        )
        g3.add_row(
            "Дозаполнить недостающие",
            self.btn_repair,
            "Найдёт изображения без upscale-версии во всех папках и обработает только их",
        )
        self.btn_clear_upscale = self._button(
            "Удалить…", "destructive", size="small", slot=self._clear_upscale_outputs
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
        self.ed_hist_search.setMaximumWidth(360)
        search_action = self.ed_hist_search.addAction(QIcon(), QLineEdit.LeadingPosition)
        self._themed(
            lambda: search_action.setIcon(symbol_icon("magnifyingglass", THEME.secondary, 14))
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
            "clock.arrow.circlepath",
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

        body.addWidget(self._section("Сохранение", top=0))
        g = Group()
        folder_ctrl = QWidget()
        fh = QHBoxLayout(folder_ctrl)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.setSpacing(10)
        self.lbl_folder = ElidedLabel()
        self.lbl_folder.setObjectName("muted")
        self.lbl_folder.setMinimumWidth(160)
        self.lbl_folder.setMaximumWidth(280)
        self.lbl_folder.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        fh.addWidget(self.lbl_folder)
        fh.addWidget(self._button("Выбрать…", size="small", slot=self._pick_folder))
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
        self.sp_min_mb.setFixedWidth(80)
        self.sp_max_mb = QDoubleSpinBox()
        self.sp_max_mb.setRange(0, 10000)
        self.sp_max_mb.setDecimals(1)
        self.sp_max_mb.setValue(1000)
        self.sp_max_mb.setFixedWidth(80)
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
        self.ed_template.setFixedWidth(220)
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
        self.sp_scroll.setFixedWidth(90)
        g.add_row("Пауза при прокрутке", self.sp_scroll, "Больше — надёжнее на длинных досках")
        self.sp_dldelay = QDoubleSpinBox()
        self.sp_dldelay.setRange(0, 30)
        self.sp_dldelay.setDecimals(1)
        self.sp_dldelay.setSingleStep(0.1)
        self.sp_dldelay.setValue(0.5)
        self.sp_dldelay.setSuffix(" с")
        self.sp_dldelay.setFixedWidth(90)
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
        body.addWidget(g)
        body.addStretch()
        return page

    # --- Журнал

    def _build_log_page(self) -> QWidget:
        page, body, actions, _ = self._page("Журнал", "Подробности о каждом шаге загрузки")
        actions.addWidget(self._button("Скопировать", symbol="doc.on.doc", slot=self._copy_log))
        actions.addWidget(self._button("Очистить", symbol="trash", slot=lambda: self.log.clear()))
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(20000)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        body.addWidget(self.log, 1)
        return page

    def _build_menu(self) -> None:
        mb = self.menuBar()

        def act(menu: QMenu, text: str, shortcut: Optional[str], slot: Callable) -> QAction:
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
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

        m_view = mb.addMenu("Вид")
        for i, (_key, title, _symbol) in enumerate(self.PAGES):
            act(m_view, title, f"Ctrl+{i + 1}", lambda _=False, idx=i: self._go_to(idx))

    def _wire_bridge(self) -> None:
        b = self._bridge
        b.log.connect(self._append_log)
        b.progress_status.connect(self._on_status)
        b.progress_bar.connect(self._on_progress_bar)
        b.stats.connect(self._on_stats)
        b.upscale_progress.connect(self._on_upscale_prog)
        b.download_timer.connect(lambda s: self.lbl_timer.setText(s.replace(" | ", "  ·  ")))
        b.upscale_timer.connect(lambda s: self.lbl_timer_up.setText(s.replace(" | ", "  ·  ")))
        b.notify.connect(self._on_notify)
        b.urls_found.connect(self._on_urls_found)
        b.job_started.connect(self._on_job_started)
        b.job_finished.connect(self._on_job_finished)
        b.finished.connect(self._on_worker_finished)
        b.board_ready.connect(self._on_board_ready)
        b.repair_finished.connect(self._on_repair_finished)
        b.engine_ready.connect(self._on_engine_ready)
        b.thumb_ready.connect(self._on_thumb_ready)

    # ------------------------------------------------------------ тема и навигация

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if not hasattr(Qt.WindowType, "ExpandedClientAreaHint"):
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
        self._update_status_icon()
        self._update_esrgan_status()
        self._themed_pause_icon()
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
            self.lbl_esrgan_icon.setPixmap(symbol_pixmap("checkmark.circle.fill", THEME.success, 20))
        else:
            name = "realesrgan-ncnn-vulkan.exe" if sys.platform == "win32" else "realesrgan-ncnn-vulkan"
            self.lbl_esrgan_title.setText("Не найден")
            self.lbl_esrgan_sub.setText(f"Положите {name} и папку models в upscale/tools/")
            self.lbl_esrgan_icon.setPixmap(
                symbol_pixmap("exclamationmark.triangle.fill", THEME.warning, 20)
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
        px = round(THUMB_SIZE * _dpr())

        def job() -> None:
            first, count = scan_board_folder(folder)
            image = load_square_thumbnail(first, px) if first else None
            self._bridge.thumb_ready.emit(url, image, count)

        self._pool.submit(job)

    @Slot(str, object, int)
    def _on_thumb_ready(self, url: str, image: object, count: int) -> None:
        for row in self._rows:
            if row.data["url"] == url:
                if isinstance(image, QImage):
                    image.setDevicePixelRatio(_dpr())
                    row.thumb.set_image(image)
                else:
                    row.thumb.set_image(None)
                row.set_image_count(count)

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
        for row in self._rows:
            if row.data["url"] == url:
                row.data["board_name"] = board or None
                row.lookup_done = True
                row.refresh()
                self._request_thumb(row)
        self._save_urls_state()

    # ------------------------------------------------------------ очередь

    def _has_url(self, url: str) -> bool:
        return any(r["url"] == url for r in self._url_rows)

    def _add_urls(self, urls: List[str], board_names: Optional[Dict[str, str]] = None) -> int:
        added = 0
        for url in urls:
            if self._has_url(url):
                continue
            board = (board_names or {}).get(url)
            data = {"url": url, "board_name": board or None, "max_images": self.sp_default_max.value()}
            self._url_rows.append(data)
            self._append_row(data)
            if board:
                self._request_thumb(self._rows[-1])
            else:
                self._fetch_board(url)
            added += 1
        if added:
            self._save_urls_state()
            self._update_queue_view()
        return added

    def _append_row(self, data: Dict[str, Any]) -> None:
        row = QueueRow(data)
        row.set_first(not self._rows)
        row.remove_clicked.connect(self._remove_row)
        row.limit_changed.connect(lambda _r: self._save_urls_state())
        row.context_requested.connect(self._row_menu)
        row.btn_remove.setEnabled(not self._is_busy())
        self.rows_box.insertWidget(self.rows_box.count() - 1, row)
        self._rows.append(row)

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
        if self._is_busy() or row not in self._rows:
            return
        idx = self._rows.index(row)
        self._rows.pop(idx)
        self._url_rows.pop(idx)
        row.deleteLater()
        if self._rows:
            self._rows[0].set_first(True)
        self._save_urls_state()
        self._update_queue_view()

    def _clear_urls(self) -> None:
        if not self._rows or self._is_busy():
            return
        if len(self._rows) > 1:
            confirm = QMessageBox.question(
                self,
                "Очистить очередь?",
                f"Из очереди будут убраны {len(self._rows)} "
                f"{plural(len(self._rows), 'доска', 'доски', 'досок')}. "
                "Скачанные файлы останутся на месте.",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if confirm != QMessageBox.Yes:
                return
        for row in self._rows:
            row.deleteLater()
        self._rows.clear()
        self._url_rows.clear()
        self._save_urls_state()
        self._update_queue_view()

    def _refresh_names(self) -> None:
        for row in self._rows:
            self._fetch_board(row.data["url"])

    def _row_menu(self, row: QueueRow, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Открыть доску в браузере", lambda: QDesktopServices.openUrl(QUrl(row.data["url"])))
        folder = self._board_folder(row.data.get("board_name"))
        if folder is not None and folder.is_dir():
            menu.addAction("Показать в Finder", lambda: self._reveal(folder))
        menu.addAction("Скопировать ссылку", lambda: QGuiApplication.clipboard().setText(row.data["url"]))
        menu.addSeparator()
        rm = menu.addAction("Убрать из очереди", lambda: self._remove_row(row))
        rm.setEnabled(not self._is_busy())
        menu.exec(pos)

    def _update_queue_view(self) -> None:
        n = len(self._rows)
        self.queue_scroll.setVisible(n > 0)
        self.queue_empty.setVisible(n == 0)
        self.lbl_queue_count.setText(str(n) if n else "")
        self.btn_clear_queue.setEnabled(n > 0 and not self._is_busy())
        self.btn_refresh_names.setEnabled(n > 0)
        if n:
            total = sum(r.image_count for r in self._rows)
            text = f"{n} {plural(n, 'доска', 'доски', 'досок')} в очереди"
            if total:
                text += f"  ·  {total} {plural(total, 'файл', 'файла', 'файлов')} уже на диске"
            self.lbl_dl_subtitle.setText(text)
        else:
            self.lbl_dl_subtitle.setText("Добавьте доски — приложение само найдёт и скачает все пины.")

    def _save_urls_state(self) -> None:
        try:
            payload = {"default_max": int(self.sp_default_max.value()), "rows": self._url_rows}
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
                self.sp_default_max.setValue(max(0, int(data.get("default_max", 0))))
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
            return data if isinstance(data, list) else []
        except Exception as e:
            self._append_log(f"⚠️ Не удалось прочитать историю: {e}")
            return []

    def _load_history(self) -> None:
        hist = [h for h in self._read_history() if isinstance(h, dict) and h.get("url")]
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
        menu = QMenu(self)
        menu.addAction("Добавить в очередь", self._add_history_selection)
        menu.addAction("Открыть в браузере", lambda: QDesktopServices.openUrl(QUrl(url)))
        menu.addAction("Скопировать ссылку", lambda: QGuiApplication.clipboard().setText(url))
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
        s["max_images_default"] = int(self.sp_default_max.value())
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
        self.sp_default_max.valueChanged.connect(lambda *_: self._save_urls_state())
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
        self.btn_folder_chip.setText(path.name or folder)
        self.btn_folder_chip.setToolTip(f"Открыть папку загрузки\n{full}")
        if save:
            self._save_timer.start()
            for row in self._rows:
                self._request_thumb(row)

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка для загрузок", self._folder)
        if d:
            self._set_folder(d)

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
        self.side_activity.setVisible(busy)
        for row in self._rows:
            row.btn_remove.setEnabled(not busy)
        self._update_queue_view()

    def _set_status(self, text: str, symbol: str, tone: str) -> None:
        self._status_symbol = (symbol, tone)
        self.lbl_status.setFullText(text)
        self.lbl_side_status.setFullText(text)
        self._update_status_icon()

    def _update_status_icon(self) -> None:
        symbol, tone = getattr(self, "_status_symbol", ("arrow.down.circle", "secondary"))
        color = getattr(THEME, tone, THEME.secondary)
        self.lbl_status_icon.setPixmap(symbol_pixmap(symbol, color, 20))

    def _set_idle_status(self) -> None:
        self._set_status("Готово к загрузке", "arrow.down.circle", "secondary")

    def _start(self) -> None:
        if self._repair_running:
            QMessageBox.warning(self, "Подождите", "Сначала дождитесь окончания дозаполнения Upscale.")
            return
        if self._downloading:
            return
        pending = extract_pinterest_urls(self.ed_url.text())
        if pending:
            self._add_urls(pending)
            self.ed_url.clear()
        if not self._url_rows:
            self._go_to(0)
            self.ed_url.setFocus()
            QMessageBox.information(
                self, "Очередь пуста", "Сначала добавьте хотя бы одну ссылку на доску Pinterest."
            )
            return

        from pinterest_download_engine import DownloadControl

        self._control = DownloadControl()
        self._paused = False
        jobs = [
            {"url": r["url"], "board_name": r.get("board_name"), "max_images": int(r.get("max_images") or 0)}
            for r in self._url_rows
        ]
        self._job_urls = [j["url"] for j in jobs]
        for row in self._rows:
            row.set_state("idle")
        self._on_stats({"found": 0, "downloaded": 0, "failed": 0, "skipped": 0})
        self.bar.setRange(0, 0)
        self.side_bar.setRange(0, 0)
        self.bar_up.setValue(0)
        self.up_box.hide()
        self.lbl_timer.setText("")
        self.btn_pause.setText("Пауза")
        self._set_status("Подготовка…", "arrow.down.circle.fill", "accent")

        self._downloading = True
        self._worker = DownloadThread(self._build_settings(), jobs, self._control, self._bridge)
        self._worker.start()
        self._set_busy_ui(True)

    def _pause(self) -> None:
        if not (self._control and self._downloading):
            return
        self._control.toggle_pause()
        self._paused = self._control.is_paused()
        self.btn_pause.setText("Продолжить" if self._paused else "Пауза")
        self._themed_pause_icon()
        if self._paused:
            self._set_status("Пауза", "pause.circle.fill", "warning")
        else:
            self._set_status("Продолжаю…", "arrow.down.circle.fill", "accent")

    def _themed_pause_icon(self) -> None:
        self.btn_pause.setIcon(symbol_icon("play.fill" if self._paused else "pause.fill", THEME.text, 14))

    def _stop(self) -> None:
        if not (self._control and self._downloading):
            return
        self._control.request_stop()
        self._set_status("Останавливаю…", "stop.circle.fill", "secondary")
        self._append_log("Остановка…")

    @Slot(str)
    def _on_status(self, text: str) -> None:
        if self._paused:
            return
        self._set_status(text, "arrow.down.circle.fill", "accent")

    @Slot(int, int)
    def _on_progress_bar(self, value: int, maximum: int) -> None:
        for bar in (self.bar, self.side_bar):
            bar.setRange(0, max(1, maximum))
            bar.setValue(min(value, maximum))

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

    @Slot(str, str)
    def _on_notify(self, title: str, msg: str) -> None:
        if self.sw_notify.isChecked():
            threading.Thread(target=_mac_notify, args=(title, msg), daemon=True).start()

    @Slot(list)
    def _on_urls_found(self, urls: object) -> None:
        if isinstance(urls, list):
            self._last_image_urls = [str(u) for u in urls]
            self.btn_export.setEnabled(bool(self._last_image_urls))

    def _row_for_job(self, idx: int) -> Optional[QueueRow]:
        if 0 <= idx < len(self._job_urls):
            url = self._job_urls[idx]
            return next((r for r in self._rows if r.data["url"] == url), None)
        return None

    @Slot(int)
    def _on_job_started(self, idx: int) -> None:
        row = self._row_for_job(idx)
        if row is not None:
            row.set_state("running")
            self.queue_scroll.ensureWidgetVisible(row)

    @Slot(int, bool)
    def _on_job_finished(self, idx: int, ok: bool) -> None:
        row = self._row_for_job(idx)
        if row is not None:
            row.set_state("done" if ok else "error")
            self._request_thumb(row)

    @Slot()
    def _on_worker_finished(self) -> None:
        stopped = bool(self._control and self._control.should_stop())
        self._downloading = False
        self._paused = False
        self.btn_pause.setText("Пауза")
        self._themed_pause_icon()
        self.lbl_timer.setText("")
        self.lbl_timer_up.setText("")
        for row in self._rows:
            if row.state == "running":
                row.set_state("idle")
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 1)
            self.side_bar.setRange(0, 1)
        self._set_busy_ui(False)
        if stopped:
            self._set_status("Остановлено", "stop.circle.fill", "secondary")
        else:
            done = self.tile_done.value.text()
            self._set_status(f"Готово — скачано {done}", "checkmark.circle.fill", "success")
            self.bar.setValue(self.bar.maximum())
        if self.stack.currentIndex() == 2:
            self._load_history()

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
        self._set_status("Проверка Upscale…", "sparkles", "accent")
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
        self._set_status("Дозаполнение Upscale завершено", "checkmark.circle.fill", "success")

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


def main() -> None:
    # Все данные (очередь, история, настройки, папка по умолчанию) — рядом с приложением
    os.chdir(APP_DIR)
    app = QApplication(sys.argv)
    app.setApplicationName("Pinterest Downloader")
    app.setApplicationDisplayName(APP_TITLE)
    app.setWindowIcon(make_app_icon())
    apply_theme(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
