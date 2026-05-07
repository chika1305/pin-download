#!/usr/bin/env python3
"""
Pinterest Image Downloader — интерфейс на Qt (PySide6) для macOS.
Та же логика скачивания, что и в pinterest_gui.py (через pinterest_download_engine),
без Tcl/Tk.

Запуск:  python pinterest_gui_mac.py
Нужны:  pip install -r requirements.txt  (на Mac подтянется PySide6)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

try:
    from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFileDialog,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QRadioButton,
        QScrollArea,
        QSpinBox,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
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

from pinterest_download_engine import DownloadControl, DownloadSettings, PinterestDownloadEngine
from pinterest_parser import PinterestParser


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


class Bridge(QObject):
    log = Signal(str)
    progress_status = Signal(str)
    progress_bar = Signal(int, int)
    stats_line = Signal(str)
    upscale_progress = Signal(str, int, int)
    download_timer = Signal(str)
    upscale_timer = Signal(str)
    notify = Signal(str, str)
    urls_found = Signal(list)
    finished = Signal()
    board_ready = Signal(int, str, str)
    repair_finished = Signal()


class DownloadThread(QThread):
    def __init__(
        self,
        settings: DownloadSettings,
        url_jobs: List[Dict[str, Any]],
        control: DownloadControl,
        bridge: Bridge,
    ):
        super().__init__()
        self.settings = settings
        self.url_jobs = url_jobs
        self.control = control
        self.bridge = bridge

    def run(self) -> None:
        eng = PinterestDownloadEngine(
            self.settings,
            self.control,
            log=self.bridge.log.emit,
            progress_status=self.bridge.progress_status.emit,
            progress_bar=self.bridge.progress_bar.emit,
            stats_line=self.bridge.stats_line.emit,
            upscale_progress=lambda t, v, m: self.bridge.upscale_progress.emit(t, v, m),
            download_timer=self.bridge.download_timer.emit,
            upscale_timer=self.bridge.upscale_timer.emit,
            notify=self.bridge.notify.emit,
            urls_discovered=lambda urls: self.bridge.urls_found.emit(list(urls)),
        )
        try:
            eng.run_multi(self.url_jobs)
        finally:
            self.bridge.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Pinterest Image Downloader (Qt)")
        self.resize(920, 880)

        self._bridge = Bridge()
        self._control = DownloadControl()
        self._worker: Optional[DownloadThread] = None
        self._last_image_urls: List[str] = []
        self._url_rows: List[Dict[str, Any]] = []
        self._repair_running = False
        self._saved_urls_file = Path("saved_urls.json")

        self._build_ui()
        self._wire_bridge()
        self._load_saved_urls()

    def _save_urls_state(self) -> None:
        try:
            payload = {
                "default_max": int(self.sp_default_max.value()),
                "rows": self._url_rows,
            }
            self._saved_urls_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            self._append_log(f"⚠️ Не удалось сохранить список URL: {e}")

    def _load_saved_urls(self) -> None:
        if not self._saved_urls_file.exists():
            return
        try:
            data = json.loads(self._saved_urls_file.read_text(encoding="utf-8"))
            default_max = int(data.get("default_max", 0))
            self.sp_default_max.setValue(max(0, default_max))
            rows = data.get("rows", [])
            if not isinstance(rows, list):
                return
            self.tbl.setRowCount(0)
            self._url_rows = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url", "")).strip()
                if not url:
                    continue
                row = {
                    "url": url,
                    "board_name": item.get("board_name") or None,
                    "max_images": int(item.get("max_images") or 0),
                }
                self._url_rows.append(row)
                idx = self.tbl.rowCount()
                self.tbl.insertRow(idx)
                board_text = row["board_name"] or "(получение…)"
                self.tbl.setItem(idx, 0, QTableWidgetItem(board_text))
                self.tbl.setItem(idx, 1, QTableWidgetItem(row["url"]))
                self.tbl.setItem(
                    idx, 2, QTableWidgetItem(str(row["max_images"]) if row["max_images"] > 0 else "Все")
                )
                if not row["board_name"]:
                    threading.Thread(
                        target=self._fetch_board_thread, args=(idx, row["url"]), daemon=True
                    ).start()
        except Exception as e:
            self._append_log(f"⚠️ Не удалось загрузить список URL: {e}")

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        outer.addWidget(scroll)

        layout.addWidget(QLabel("<h2>Pinterest Image Downloader</h2>"))
        layout.addWidget(
            QLabel("Скачивание через Qt — без Tk. Логика совпадает с pinterest_gui.py.")
        )

        g1 = QGroupBox("Основные настройки")
        g1l = QVBoxLayout(g1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Количество по умолчанию (0 = все):"))
        self.sp_default_max = QSpinBox()
        self.sp_default_max.setRange(0, 100000)
        self.sp_default_max.setValue(0)
        row.addWidget(self.sp_default_max)
        row.addStretch()
        g1l.addLayout(row)

        g1l.addWidget(QLabel("URL доски / страницы:"))
        ur = QHBoxLayout()
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("https://www.pinterest.com/... или pin.it/...")
        ur.addWidget(self.ed_url, 1)
        b_add = QPushButton("Добавить")
        b_add.clicked.connect(self._add_url)
        ur.addWidget(b_add)
        b_hist = QPushButton("История")
        b_hist.clicked.connect(self._show_history)
        ur.addWidget(b_hist)
        g1l.addLayout(ur)

        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(["Доска", "URL", "Макс."])
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setMinimumHeight(140)
        self.tbl.cellDoubleClicked.connect(self._edit_max_for_row)
        g1l.addWidget(self.tbl)

        br = QHBoxLayout()
        b_rm = QPushButton("Удалить выбранные")
        b_rm.clicked.connect(self._remove_selected)
        br.addWidget(b_rm)
        b_clr = QPushButton("Очистить список")
        b_clr.clicked.connect(self._clear_urls)
        br.addWidget(b_clr)
        b_ref = QPushButton("Обновить названия")
        b_ref.clicked.connect(self._refresh_names)
        br.addWidget(b_ref)
        br.addStretch()
        g1l.addLayout(br)

        fr = QHBoxLayout()
        fr.addWidget(QLabel("Папка сохранения:"))
        self.ed_folder = QLineEdit("pinterest_images")
        fr.addWidget(self.ed_folder, 1)
        b_f = QPushButton("Выбрать…")
        b_f.clicked.connect(self._pick_folder)
        fr.addWidget(b_f)
        g1l.addLayout(fr)
        layout.addWidget(g1)

        g2 = QGroupBox("Дополнительно")
        g2l = QVBoxLayout(g2)
        qrow = QHBoxLayout()
        self.rb_full = QRadioButton("Полный размер")
        self.rb_med = QRadioButton("Средний")
        self.rb_small = QRadioButton("Маленький")
        self.rb_full.setChecked(True)
        qrow.addWidget(self.rb_full)
        qrow.addWidget(self.rb_med)
        qrow.addWidget(self.rb_small)
        g2l.addLayout(qrow)

        sz = QHBoxLayout()
        sz.addWidget(QLabel("Размер файла (МБ) от"))
        self.sp_min_mb = QDoubleSpinBox()
        self.sp_min_mb.setRange(0, 10000)
        self.sp_min_mb.setValue(0)
        sz.addWidget(self.sp_min_mb)
        sz.addWidget(QLabel("до"))
        self.sp_max_mb = QDoubleSpinBox()
        self.sp_max_mb.setRange(0, 10000)
        self.sp_max_mb.setValue(1000)
        sz.addWidget(self.sp_max_mb)
        g2l.addLayout(sz)

        self.cb_upscale = QCheckBox("Upscale после скачивания")
        g2l.addWidget(self.cb_upscale)
        up = QGridLayout()
        up.addWidget(QLabel("Масштаб"), 0, 0)
        hr = QHBoxLayout()
        self.rb_s2 = QRadioButton("x2")
        self.rb_s3 = QRadioButton("x3")
        self.rb_s4 = QRadioButton("x4")
        self.rb_s3.setChecked(True)
        hr.addWidget(self.rb_s2)
        hr.addWidget(self.rb_s3)
        hr.addWidget(self.rb_s4)
        up.addLayout(hr, 0, 1)
        up.addWidget(QLabel("Модель"), 1, 0)
        mr = QHBoxLayout()
        self.rb_mauto = QRadioButton("Авто")
        self.rb_mphoto = QRadioButton("Фото")
        self.rb_manime = QRadioButton("Аниме")
        self.rb_mauto.setChecked(True)
        mr.addWidget(self.rb_mauto)
        mr.addWidget(self.rb_mphoto)
        mr.addWidget(self.rb_manime)
        up.addLayout(mr, 1, 1)
        up.addWidget(QLabel("Тайл"), 2, 0)
        self.sp_tile = QSpinBox()
        self.sp_tile.setRange(50, 500)
        self.sp_tile.setValue(200)
        up.addWidget(self.sp_tile, 2, 1)
        up.addWidget(QLabel("GPU"), 3, 0)
        self.sp_gpu = QSpinBox()
        self.sp_gpu.setRange(0, 10)
        self.sp_gpu.setValue(0)
        up.addWidget(self.sp_gpu, 3, 1)
        g2l.addLayout(up)

        self.cb_subfolder = QCheckBox("Подпапка по названию доски")
        self.cb_subfolder.setChecked(True)
        g2l.addWidget(self.cb_subfolder)
        self.cb_resume = QCheckBox("Пропускать уже скачанные (resume)")
        self.cb_resume.setChecked(True)
        g2l.addWidget(self.cb_resume)
        self.cb_notify = QCheckBox("Уведомление по завершении")
        self.cb_notify.setChecked(True)
        g2l.addWidget(self.cb_notify)
        self.cb_meta = QCheckBox("Экспорт метаданных JSON")
        g2l.addWidget(self.cb_meta)
        self.cb_autorename = QCheckBox("Шаблон имён файлов")
        self.cb_autorename.setChecked(True)
        g2l.addWidget(self.cb_autorename)
        self.ed_template = QLineEdit("{index04}_{hash}.jpg")
        g2l.addWidget(self.ed_template)

        dl = QHBoxLayout()
        dl.addWidget(QLabel("Задержка прокрутки (сек)"))
        self.sp_scroll = QDoubleSpinBox()
        self.sp_scroll.setRange(0.2, 30)
        self.sp_scroll.setValue(2.0)
        dl.addWidget(self.sp_scroll)
        dl.addWidget(QLabel("Задержка скачивания (сек)"))
        self.sp_dldelay = QDoubleSpinBox()
        self.sp_dldelay.setRange(0, 30)
        self.sp_dldelay.setValue(0.5)
        dl.addWidget(self.sp_dldelay)
        g2l.addLayout(dl)
        layout.addWidget(g2)

        g3 = QGroupBox("Прогресс и лог")
        g3l = QVBoxLayout(g3)
        self.lbl_status = QLabel("Готов")
        g3l.addWidget(self.lbl_status)
        self.lbl_timer_dl = QLabel("")
        g3l.addWidget(self.lbl_timer_dl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        g3l.addWidget(self.bar)
        self.lbl_up = QLabel("")
        g3l.addWidget(self.lbl_up)
        self.lbl_timer_up = QLabel("")
        g3l.addWidget(self.lbl_timer_up)
        self.lbl_stats = QLabel("Найдено: 0 | Скачано: 0 | Ошибок: 0 | Пропущено: 0")
        g3l.addWidget(self.lbl_stats)
        self.bar_up = QProgressBar()
        self.bar_up.setRange(0, 100)
        self.bar_up.setValue(0)
        g3l.addWidget(self.bar_up)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(220)
        g3l.addWidget(self.log)

        btn = QHBoxLayout()
        self.btn_start = QPushButton("Запустить")
        self.btn_start.clicked.connect(self._start)
        btn.addWidget(self.btn_start)
        self.btn_pause = QPushButton("Пауза")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._pause)
        btn.addWidget(self.btn_pause)
        self.btn_stop = QPushButton("Стоп")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        btn.addWidget(self.btn_stop)
        self.btn_repair = QPushButton("Дозаполнить Upscale")
        self.btn_repair.clicked.connect(self._start_upscale_repair)
        btn.addWidget(self.btn_repair)
        self.btn_clear_upscale = QPushButton("Очистить Upscale")
        self.btn_clear_upscale.clicked.connect(self._clear_upscale_outputs)
        btn.addWidget(self.btn_clear_upscale)
        b_open = QPushButton("Открыть папку")
        b_open.clicked.connect(self._open_folder)
        btn.addWidget(b_open)
        b_exp = QPushButton("Экспорт URL")
        b_exp.clicked.connect(self._export_urls)
        btn.addWidget(b_exp)
        btn.addStretch()
        g3l.addLayout(btn)
        layout.addWidget(g3)

        layout.addStretch()

    def _wire_bridge(self) -> None:
        self._bridge.log.connect(self._append_log)
        self._bridge.progress_status.connect(self.lbl_status.setText)
        self._bridge.progress_bar.connect(self._on_progress_bar)
        self._bridge.stats_line.connect(self.lbl_stats.setText)
        self._bridge.upscale_progress.connect(self._on_upscale_prog)
        self._bridge.download_timer.connect(self.lbl_timer_dl.setText)
        self._bridge.upscale_timer.connect(self.lbl_timer_up.setText)
        self._bridge.notify.connect(self._on_notify)
        self._bridge.urls_found.connect(self._on_urls_found)
        self._bridge.finished.connect(self._on_worker_finished)
        self._bridge.board_ready.connect(self._on_board_ready)
        self._bridge.repair_finished.connect(self._on_repair_finished)

    @Slot(str)
    def _append_log(self, s: str) -> None:
        self.log.append(s)

    @Slot(int, int)
    def _on_progress_bar(self, value: int, maximum: int) -> None:
        self.bar.setMaximum(max(1, maximum))
        self.bar.setValue(min(value, maximum))

    @Slot(str, int, int)
    def _on_upscale_prog(self, text: str, value: int, maximum: int) -> None:
        self.lbl_up.setText(text)
        self.bar_up.setMaximum(max(1, maximum))
        self.bar_up.setValue(min(value, maximum))

    @Slot(str, str)
    def _on_notify(self, title: str, msg: str) -> None:
        if self.cb_notify.isChecked():
            _mac_notify(title, msg)

    @Slot(list)
    def _on_urls_found(self, urls: object) -> None:
        if isinstance(urls, list):
            self._last_image_urls = [str(u) for u in urls]

    @Slot()
    def _on_worker_finished(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_repair.setEnabled(True)
        self.btn_pause.setText("Пауза")
        self._control.reset()
        self.lbl_status.setText("Готов")
        self.lbl_timer_dl.setText("")
        self.lbl_timer_up.setText("")

    @Slot()
    def _on_repair_finished(self) -> None:
        self._repair_running = False
        if not (self._worker and self._worker.isRunning()):
            self.btn_start.setEnabled(True)
        self.btn_repair.setEnabled(True)
        self.lbl_status.setText("Готов")

    @Slot(int, str, str)
    def _on_board_ready(self, row: int, board: str, url: str) -> None:
        if 0 <= row < len(self._url_rows) and self._url_rows[row]["url"] == url:
            self._url_rows[row]["board_name"] = board or None
            disp = board if board else "(название не найдено)"
            self.tbl.setItem(row, 0, QTableWidgetItem(disp))
            self.tbl.setItem(row, 1, QTableWidgetItem(url))
            mi = self._url_rows[row]["max_images"]
            self.tbl.setItem(
                row, 2, QTableWidgetItem(str(mi) if mi > 0 else "Все")
            )
            self._save_urls_state()

    def _image_quality(self) -> str:
        if self.rb_med.isChecked():
            return "medium"
        if self.rb_small.isChecked():
            return "small"
        return "full"

    def _upscale_model(self) -> str:
        if self.rb_mphoto.isChecked():
            return "photo"
        if self.rb_manime.isChecked():
            return "anime"
        return "auto"

    def _upscale_scale(self) -> int:
        if self.rb_s2.isChecked():
            return 2
        if self.rb_s4.isChecked():
            return 4
        return 3

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка", self.ed_folder.text())
        if d:
            self.ed_folder.setText(d)

    def _add_url(self) -> None:
        url = self.ed_url.text().strip()
        if not url:
            return
        low = url.lower()
        if "pinterest.com" not in low and "pin.it" not in low:
            QMessageBox.warning(self, "Ошибка", "Нужен URL Pinterest или pin.it")
            return
        if any(r["url"] == url for r in self._url_rows):
            QMessageBox.information(self, "Уже есть", "Этот URL уже в списке")
            return
        row = len(self._url_rows)
        mi = self.sp_default_max.value()
        self._url_rows.append({"url": url, "board_name": None, "max_images": mi})
        self.tbl.insertRow(row)
        self.tbl.setItem(row, 0, QTableWidgetItem("(получение…)"))
        self.tbl.setItem(row, 1, QTableWidgetItem(url))
        self.tbl.setItem(row, 2, QTableWidgetItem(str(mi) if mi > 0 else "Все"))
        self._save_urls_state()
        self.ed_url.clear()
        threading.Thread(
            target=self._fetch_board_thread, args=(row, url), daemon=True
        ).start()

    def _fetch_board_thread(self, row: int, url: str) -> None:
        try:
            tp = PinterestParser(download_folder=self.ed_folder.text() or "pinterest_images")
            exp = tp.expand_short_url(url)
            name = tp.get_board_name_from_url(exp)
            tp.close()
            self._bridge.board_ready.emit(row, name or "", url)
        except Exception:
            self._bridge.board_ready.emit(row, "", url)

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl.removeRow(r)
            del self._url_rows[r]
        self._save_urls_state()

    def _clear_urls(self) -> None:
        self.tbl.setRowCount(0)
        self._url_rows.clear()
        self._save_urls_state()

    def _refresh_names(self) -> None:
        if not self._url_rows:
            QMessageBox.information(self, "Список пуст", "Добавьте URL")
            return
        for i, row in enumerate(self._url_rows):
            threading.Thread(
                target=self._fetch_board_thread, args=(i, row["url"]), daemon=True
            ).start()

    def _edit_max_for_row(self, row: int, col: int) -> None:
        if row < 0 or row >= len(self._url_rows):
            return
        cur = self._url_rows[row]["max_images"]
        v, ok = QInputDialog.getInt(
            self,
            "Максимум изображений",
            "0 = все",
            cur,
            0,
            100000,
            1,
        )
        if ok:
            self._url_rows[row]["max_images"] = v
            self.tbl.setItem(
                row, 2, QTableWidgetItem(str(v) if v > 0 else "Все")
            )
            self._save_urls_state()

    def _collect_jobs(self) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        line = self.ed_url.text().strip()
        if line and (
            "pinterest.com" in line.lower() or "pin.it" in line.lower()
        ):
            jobs.append(
                {
                    "url": line,
                    "board_name": None,
                    "max_images": self.sp_default_max.value(),
                }
            )
        for r in self._url_rows:
            jobs.append(
                {
                    "url": r["url"],
                    "board_name": r.get("board_name"),
                    "max_images": int(r.get("max_images") or 0),
                }
            )
        return jobs

    def _build_settings(self) -> DownloadSettings:
        return DownloadSettings(
            download_folder=self.ed_folder.text().strip() or "pinterest_images",
            max_images_default=self.sp_default_max.value(),
            image_quality=self._image_quality(),
            scroll_delay=float(self.sp_scroll.value()),
            download_delay=float(self.sp_dldelay.value()),
            min_size_mb=float(self.sp_min_mb.value()),
            max_size_mb=float(self.sp_max_mb.value()),
            auto_subfolder=self.cb_subfolder.isChecked(),
            resume_download=self.cb_resume.isChecked(),
            auto_rename=self.cb_autorename.isChecked(),
            filename_template=self.ed_template.text().strip() or "{index04}_{hash}.jpg",
            export_metadata=self.cb_meta.isChecked(),
            enable_upscale=self.cb_upscale.isChecked(),
            upscale_scale=self._upscale_scale(),
            upscale_model=self._upscale_model(),
            upscale_tile=int(self.sp_tile.value()),
            upscale_gpu=int(self.sp_gpu.value()),
            notify_on_complete=self.cb_notify.isChecked(),
        )

    def _start(self) -> None:
        if self._repair_running:
            QMessageBox.warning(self, "Занято", "Сначала дождитесь завершения дозаполнения Upscale")
            return
        jobs = self._collect_jobs()
        if not jobs:
            QMessageBox.warning(self, "Нет URL", "Добавьте хотя бы один URL")
            return
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "Занято", "Скачивание уже идёт")
            return

        self._control.reset()
        self.stats_reset()
        self.btn_start.setEnabled(False)
        self.btn_repair.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.bar.setValue(0)
        self.bar_up.setValue(0)

        settings = self._build_settings()
        self._worker = DownloadThread(settings, jobs, self._control, self._bridge)
        self._worker.start()

    def stats_reset(self) -> None:
        self.lbl_stats.setText(
            "Найдено: 0 | Скачано: 0 | Ошибок: 0 | Пропущено: 0"
        )

    def _pause(self) -> None:
        self._control.toggle_pause()
        self.btn_pause.setText(
            "Возобновить" if self._control.is_paused() else "Пауза"
        )

    def _stop(self) -> None:
        self._control.request_stop()
        self.log.append("Остановка…")

    def _is_image_file(self, p: Path) -> bool:
        return p.is_file() and p.suffix.lower() in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
        }

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
        upscaled = [p for p in sorted(up_dir.iterdir()) if self._is_image_file(p)]
        up_stems = {p.stem.lower() for p in upscaled}
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
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "Занято", "Сначала дождитесь завершения скачивания")
            return
        if self._repair_running:
            QMessageBox.information(self, "В работе", "Дозаполнение Upscale уже выполняется")
            return

        root = Path(self.ed_folder.text().strip() or "pinterest_images")
        if not root.exists():
            QMessageBox.warning(self, "Нет папки", str(root))
            return
        if root.name.lower() == "upscale" and root.parent.exists():
            # Если пользователь выбрал папку upscale, берём родительскую папку доски
            root = root.parent
            self._bridge.log.emit(
                f"ℹ️ Выбрана папка upscale, проверяю родительскую папку: {root}"
            )

        self._repair_running = True
        self.btn_repair.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.lbl_status.setText("Проверка Upscale...")
        settings = self._build_settings()
        settings.download_folder = str(root)
        threading.Thread(
            target=self._run_upscale_repair, args=(settings,), daemon=True
        ).start()

    def _clear_upscale_outputs(self) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "Занято", "Сначала дождитесь завершения скачивания")
            return
        if self._repair_running:
            QMessageBox.warning(self, "Занято", "Сначала дождитесь завершения дозаполнения Upscale")
            return

        root = Path(self.ed_folder.text().strip() or "pinterest_images")
        if not root.exists():
            QMessageBox.warning(self, "Нет папки", str(root))
            return

        confirm = QMessageBox.question(
            self,
            "Очистить Upscale",
            "Удалить все улучшенные изображения из папок 'upscale'?\n"
            "Оригинальные файлы затронуты не будут.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
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
                        self._bridge.log.emit(f"⚠️ Не удалось удалить {p}: {e}")
            if removed_here > 0:
                touched_dirs += 1

        self._bridge.log.emit(
            f"🧹 Очистка Upscale завершена: удалено {deleted} файлов в {touched_dirs} папках."
        )
        QMessageBox.information(
            self,
            "Готово",
            f"Удалено {deleted} улучшенных изображений из {touched_dirs} папок upscale.",
        )

    def _run_upscale_repair(self, settings: DownloadSettings) -> None:
        try:
            root = Path(settings.download_folder)
            folders = self._collect_download_folders_for_repair(root)
            if not folders:
                self._bridge.log.emit("⚠️ Не найдено папок с изображениями для проверки Upscale.")
                return

            self._bridge.log.emit("\n=== Дозаполнение Upscale ===")
            ctrl = DownloadControl()
            eng = PinterestDownloadEngine(
                settings,
                ctrl,
                log=self._bridge.log.emit,
                progress_status=self._bridge.progress_status.emit,
                progress_bar=self._bridge.progress_bar.emit,
                stats_line=self._bridge.stats_line.emit,
                upscale_progress=lambda t, v, m: self._bridge.upscale_progress.emit(
                    t, v, m
                ),
                download_timer=self._bridge.download_timer.emit,
                upscale_timer=self._bridge.upscale_timer.emit,
                notify=self._bridge.notify.emit,
                urls_discovered=lambda _urls: None,
            )

            total_missing = 0
            total_done = 0
            for idx, folder in enumerate(folders, start=1):
                self._bridge.progress_status.emit(
                    f"Проверка Upscale: {idx}/{len(folders)}"
                )
                missing = self._find_missing_upscale_files(folder)
                if not missing:
                    self._bridge.log.emit(
                        f"✓ {folder.name}: все файлы уже имеют upscale"
                    )
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
                    self._bridge.log.emit(
                        f"✅ {folder.name}: добавлено upscale файлов: {moved}"
                    )
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

    def _open_folder(self) -> None:
        p = self.ed_folder.text().strip()
        if not os.path.isdir(p):
            QMessageBox.warning(self, "Нет папки", p)
            return
        if sys.platform == "darwin":
            subprocess.run(["open", p], check=False)
        elif sys.platform == "win32":
            os.startfile(p)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", p], check=False)

    def _export_urls(self) -> None:
        if not self._last_image_urls:
            QMessageBox.information(
                self, "Пусто", "Сначала дождитесь сбора URL при скачивании"
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить URL", "", "Text (*.txt)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self._last_image_urls))
            QMessageBox.information(self, "Готово", f"Записано {len(self._last_image_urls)} URL")

    def _show_history(self) -> None:
        path = Path("download_history.json")
        if not path.exists():
            QMessageBox.information(self, "История", "Файл истории пуст")
            return
        try:
            hist = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", str(e))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("История скачиваний")
        dlg.resize(800, 400)
        v = QVBoxLayout(dlg)
        rows_data = list(reversed(hist[-200:]))
        t = QTableWidget(len(rows_data), 4)
        t.setHorizontalHeaderLabels(["Доска", "URL", "Скачано", "Дата"])
        t.setSelectionBehavior(QTableWidget.SelectRows)
        for i, it in enumerate(rows_data):
            t.setItem(i, 0, QTableWidgetItem(str(it.get("board_name") or "")))
            t.setItem(i, 1, QTableWidgetItem(str(it.get("url") or "")))
            t.setItem(i, 2, QTableWidgetItem(str(it.get("count", ""))))
            t.setItem(i, 3, QTableWidgetItem(str(it.get("date") or "")))
        v.addWidget(t)

        def add_selected() -> None:
            sel = t.selectionModel().selectedRows()
            added = 0
            for idx in sel:
                r = idx.row()
                url_item = t.item(r, 1)
                board_item = t.item(r, 0)
                if not url_item:
                    continue
                u = url_item.text()
                b_raw = board_item.text() if board_item else ""
                b = b_raw if b_raw and b_raw != "(не указано)" else None
                if "%" in (b or ""):
                    try:
                        b = unquote(b or "")
                    except Exception:
                        pass
                if any(x["url"] == u for x in self._url_rows):
                    continue
                row = len(self._url_rows)
                mi = self.sp_default_max.value()
                self._url_rows.append({"url": u, "board_name": b, "max_images": mi})
                self.tbl.insertRow(row)
                self.tbl.setItem(
                    row,
                    0,
                    QTableWidgetItem(b or "(получение…)")
                    if not b
                    else QTableWidgetItem(b),
                )
                self.tbl.setItem(row, 1, QTableWidgetItem(u))
                self.tbl.setItem(
                    row, 2, QTableWidgetItem(str(mi) if mi > 0 else "Все")
                )
                if not b:
                    threading.Thread(
                        target=self._fetch_board_thread, args=(row, u), daemon=True
                    ).start()
                added += 1
            if added > 0:
                self._save_urls_state()
            QMessageBox.information(
                self, "История", f"Добавлено в список: {added} URL"
            )

        bb = QHBoxLayout()
        bb.addStretch()
        b_add = QPushButton("Добавить выбранные в список")
        b_add.clicked.connect(add_selected)
        bb.addWidget(b_add)
        b_close = QPushButton("Закрыть")
        b_close.clicked.connect(dlg.close)
        bb.addWidget(b_close)
        v.addLayout(bb)
        dlg.exec()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_urls_state()
        self._control.request_stop()
        if self._worker and self._worker.isRunning():
            self._worker.wait(3000)
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Pinterest Downloader")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
