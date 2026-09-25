"""
Общая логика скачивания/upscale без Tkinter — для GUI на Qt (macOS) и при необходимости других оболочек.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union
from urllib.parse import unquote

from PIL import Image

from pinterest_parser import PinterestParser


@dataclass
class DownloadSettings:
    download_folder: str
    max_images_default: int
    image_quality: str
    scroll_delay: float
    download_delay: float
    min_size_mb: float
    max_size_mb: float
    auto_subfolder: bool
    resume_download: bool
    auto_rename: bool
    filename_template: str
    export_metadata: bool
    enable_upscale: bool
    upscale_scale: int
    upscale_model: str
    upscale_tile: int
    upscale_gpu: int
    notify_on_complete: bool
    history_file: str = "download_history.json"
    timing_stats_file: str = "timing_stats.json"


def _resource_dirs() -> List[Path]:
    """Папка с кодом, а в собранном .app — ещё и ресурсы PyInstaller."""
    dirs = [Path(__file__).resolve().parent]
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled and Path(bundled) not in dirs:
        dirs.append(Path(bundled))
    return dirs


def find_upscale_binary() -> Optional[Path]:
    """Путь к realesrgan-ncnn-vulkan в upscale/ или None."""
    names = (
        ["realesrgan-ncnn-vulkan.exe"]
        if sys.platform == "win32"
        else ["realesrgan-ncnn-vulkan", "realesrgan-ncnn-vulkan.exe"]
    )
    for base_dir in _resource_dirs():
        tools_dir = base_dir / "upscale" / "tools"
        for name in names:
            for cand in (tools_dir / name, base_dir / "upscale" / name, base_dir / name):
                if cand.exists():
                    return cand
        if tools_dir.exists():
            for name in names:
                found = next((p for p in tools_dir.rglob(name) if p.is_file()), None)
                if found:
                    return found
    return None


class JobQueue:
    """
    Очередь досок для run_multi. Интерфейс может добавлять, убирать и переставлять
    ещё не начатые доски прямо во время скачивания.
    """

    def __init__(self, jobs: Optional[List[Dict]] = None) -> None:
        self._lock = threading.Lock()
        self._pending: List[Dict] = list(jobs or [])
        self._taken: set = set()

    @staticmethod
    def key(job: Dict) -> str:
        return str(job.get("key") or job["url"])

    def pop_next(self) -> Optional[Dict]:
        with self._lock:
            if not self._pending:
                return None
            job = self._pending.pop(0)
            self._taken.add(self.key(job))
            return job

    def set_pending(self, jobs: List[Dict]) -> None:
        """Заменяет список ожидающих досок; уже взятые в работу не добавляются повторно."""
        with self._lock:
            self._pending = [j for j in jobs if self.key(j) not in self._taken]

    def release(self, key: str) -> None:
        """Разрешает взять доску ещё раз (повтор после ошибки)."""
        with self._lock:
            self._taken.discard(key)

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)


class DownloadControl:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = False
        self._paused = False

    def reset(self) -> None:
        with self._lock:
            self._stop = False
            self._paused = False

    def request_stop(self) -> None:
        with self._lock:
            self._stop = True

    def should_stop(self) -> bool:
        with self._lock:
            return self._stop

    def toggle_pause(self) -> None:
        with self._lock:
            self._paused = not self._paused

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused


class PinterestDownloadEngine:
    """Снимок настроек + колбэки; методы вызываются из рабочего потока."""

    def __init__(
        self,
        settings: DownloadSettings,
        control: DownloadControl,
        log: Callable[[str], None],
        progress_status: Callable[[str], None],
        progress_bar: Callable[[int, int], None],
        stats_line: Callable[[str], None],
        upscale_progress: Callable[[str, int, int], None],
        download_timer: Callable[[str], None],
        upscale_timer: Callable[[str], None],
        notify: Callable[[str, str], None],
        urls_discovered: Optional[Callable[[List[str]], None]] = None,
        stats_changed: Optional[Callable[[Dict[str, int]], None]] = None,
        job_started: Optional[Callable[[str], None]] = None,
        job_finished: Optional[Callable[[str, bool], None]] = None,
        job_progress: Optional[Callable[[str, int, int, float], None]] = None,
    ) -> None:
        self.s = settings
        self.ctrl = control
        self._log = log
        self._progress_status = progress_status
        self._progress_bar = progress_bar
        self._stats_line = stats_line
        self._upscale_progress = upscale_progress
        self._download_timer = download_timer
        self._upscale_timer = upscale_timer
        self._notify = notify
        self._urls_discovered = urls_discovered
        self._stats_changed = stats_changed
        self._job_started = job_started
        self._job_finished = job_finished
        self._job_progress = job_progress
        self._current_job: Optional[str] = None

        self._parser: Optional[PinterestParser] = None
        self.stats = {"found": 0, "downloaded": 0, "failed": 0, "skipped": 0}
        self.total_images_to_download = 0
        self.current_downloaded_count = 0
        self.image_urls_list: List[str] = []
        self.download_start_time: Optional[float] = None
        self.estimated_download_time: Optional[float] = None
        self.upscale_start_time: Optional[float] = None
        self.estimated_upscale_time: Optional[float] = None
        self.history: List[dict] = []
        self.timing_stats: Dict = {"download_times": [], "upscale_times": []}

        self._load_history()
        self._load_timing_stats()

    def _load_history(self) -> None:
        p = self.s.history_file
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    self.history = json.load(f)
            except Exception:
                self.history = []

    def save_history(self) -> None:
        try:
            with open(self.s.history_file, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log(f"Ошибка сохранения истории: {e}")

    def _load_timing_stats(self) -> None:
        p = self.s.timing_stats_file
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    self.timing_stats = json.load(f)
            except Exception:
                pass
        if "download_times" not in self.timing_stats:
            self.timing_stats["download_times"] = []
        if "upscale_times" not in self.timing_stats:
            self.timing_stats["upscale_times"] = []

    def _save_timing_stats(self) -> None:
        try:
            with open(self.s.timing_stats_file, "w", encoding="utf-8") as f:
                json.dump(self.timing_stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения статистики времени: {e}")

    def add_download_timing(self, image_count: int, elapsed: float) -> None:
        self.timing_stats["download_times"].append(
            {
                "count": image_count,
                "time": elapsed,
                "timestamp": datetime.now().isoformat(),
            }
        )
        if len(self.timing_stats["download_times"]) > 50:
            self.timing_stats["download_times"] = self.timing_stats["download_times"][
                -50:
            ]
        self._save_timing_stats()

    def add_upscale_timing(self, image_count: int, elapsed: float) -> None:
        self.timing_stats["upscale_times"].append(
            {
                "count": image_count,
                "time": elapsed,
                "timestamp": datetime.now().isoformat(),
            }
        )
        if len(self.timing_stats["upscale_times"]) > 50:
            self.timing_stats["upscale_times"] = self.timing_stats["upscale_times"][
                -50:
            ]
        self._save_timing_stats()

    def estimate_download_time(self, image_count: int) -> Optional[float]:
        rec = self.timing_stats["download_times"][-10:]
        if not rec:
            return None
        total_time, total_count = 0.0, 0
        for r in rec:
            if r["count"] > 0:
                total_time += r["time"] / r["count"]
                total_count += 1
        if total_count == 0:
            return None
        return (total_time / total_count) * image_count

    def estimate_upscale_time(self, image_count: int) -> Optional[float]:
        rec = self.timing_stats["upscale_times"][-10:]
        if not rec:
            return None
        total_time, total_count = 0.0, 0
        for r in rec:
            if r["count"] > 0:
                total_time += r["time"] / r["count"]
                total_count += 1
        if total_count == 0:
            return None
        return (total_time / total_count) * image_count

    @staticmethod
    def format_time(seconds: Optional[float]) -> str:
        if seconds is None:
            return "---"
        if seconds < 60:
            return f"{int(seconds)} сек"
        if seconds < 3600:
            m, s = int(seconds // 60), int(seconds % 60)
            return f"{m} мин {s} сек"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h} ч {m} мин"

    def _emit_stats(self) -> None:
        s = self.stats
        self._stats_line(
            f"Найдено: {s['found']} | Скачано: {s['downloaded']} | "
            f"Ошибок: {s['failed']} | Пропущено: {s['skipped']}"
        )
        if self._stats_changed:
            self._stats_changed(dict(s))

    # --- upscale helpers (как в pinterest_gui.py) ---
    def _find_upscale_exe(self) -> Optional[Path]:
        return find_upscale_binary()

    def _find_models_dir(self, exe_path: Optional[Path]) -> Optional[Path]:
        if exe_path:
            near = exe_path.parent / "models"
            if near.exists() and any(near.glob("*.param")) and any(near.glob("*.bin")):
                return near
        for base_dir in _resource_dirs():
            models = base_dir / "upscale" / "tools" / "models"
            if models.exists() and any(models.glob("*.param")) and any(models.glob("*.bin")):
                return models
        return None

    def _list_model_names(self, models_dir: Path) -> List[str]:
        names = set()
        for p in models_dir.glob("*.param"):
            if (models_dir / f"{p.stem}.bin").exists():
                names.add(p.stem)
        return sorted(names, key=str.lower)

    @staticmethod
    def _parse_scale_from_name(name: str) -> Optional[int]:
        s = name.lower()
        m = re.search(r"[_\-]x([234])(?:[_\-]|$)", s)
        if m:
            return int(m.group(1))
        m2 = re.match(r"([234])x", s)
        if m2:
            return int(m2.group(1))
        return None

    def _pick_model(self, available: List[str], mode: str, want_scale: int) -> Optional[str]:
        def has_exact(scale: int, pool: List[str]) -> Optional[str]:
            for n in pool:
                if self._parse_scale_from_name(n) == scale:
                    return n
            return None

        if mode == "anime":
            anime_pool = [n for n in available if "anime" in n.lower()]
            c = has_exact(want_scale, anime_pool)
            if c:
                return c
            for pref in (
                "realesr-animevideov3-x4",
                "realesr-animevideov3-x3",
                "realesr-animevideov3-x2",
            ):
                for n in anime_pool:
                    if pref in n.lower():
                        return n

        general_pool = [n for n in available if "general" in n.lower()]
        c = has_exact(want_scale, general_pool)
        if c:
            return c
        for pref in ("realesrgan_general_wdn_x4_v3", "realesrgan_general_x4_v3"):
            for n in general_pool:
                if pref == n.lower():
                    return n

        other_pool = [n for n in available if n not in general_pool]
        c = has_exact(want_scale, other_pool)
        if c:
            return c
        return available[0] if available else None

    def _rescale_outputs(self, out_dir: Path, run_scale: int, want_scale: int) -> None:
        if run_scale == want_scale:
            return
        ratio = want_scale / run_scale
        outs = [
            p
            for p in sorted(out_dir.iterdir())
            if p.is_file() and p.suffix.lower() in {".jpg", ".png", ".webp"}
        ]
        for p in outs:
            try:
                im = Image.open(p).convert("RGB")
                w, h = im.size
                nw = max(1, int(round(w * ratio)))
                nh = max(1, int(round(h * ratio)))
                im.resize((nw, nh), Image.Resampling.LANCZOS).save(p, quality=95)
            except Exception as e:
                self._log(f"Ошибка ресэмплинга {p.name}: {e}")

    def run_upscale(self, input_folder: str) -> bool:
        try:
            input_path = Path(input_folder)
            image_files = [
                f
                for f in input_path.iterdir()
                if f.is_file()
                and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
            total = len(image_files)
            if total == 0:
                self._log(f"⚠️ Нет изображений для upscale в папке: {input_folder}")
                return False

            self._log(f"🔄 Upscale: {input_folder} ({total} изображений)")
            self._upscale_progress(f"Upscale: 0/{total}", 0, max(1, total))

            exe = self._find_upscale_exe()
            if not exe:
                hint = (
                    "realesrgan-ncnn-vulkan.exe"
                    if sys.platform == "win32"
                    else "realesrgan-ncnn-vulkan"
                )
                self._log(f"❌ Не найден {hint} в upscale/tools/")
                if sys.platform == "darwin":
                    self._log(
                        "ℹ️ Для macOS нужен именно macOS-бинарник Real-ESRGAN "
                        "(файл `realesrgan-ncnn-vulkan` без .exe)."
                    )
                return False
            if sys.platform == "darwin" and exe.suffix.lower() == ".exe":
                self._log(
                    f"❌ Найден Windows-бинарник: {exe.name}. "
                    "На macOS он не запустится."
                )
                self._log(
                    "ℹ️ Замените его на macOS-файл `realesrgan-ncnn-vulkan` "
                    "(без .exe) в папке upscale/tools/."
                )
                return False
            if sys.platform != "win32":
                try:
                    mode = exe.stat().st_mode
                    if not (mode & 0o111):
                        exe.chmod(mode | 0o755)
                        self._log(f"ℹ️ Выставил права на запуск: {exe.name}")
                except Exception:
                    pass

            models_dir = self._find_models_dir(exe)
            if not models_dir:
                self._log("❌ Не найдена папка models")
                return False

            param_files = sorted(models_dir.glob("*.param"))
            bin_files = sorted(models_dir.glob("*.bin"))
            if not param_files and not bin_files:
                self._log(f"❌ В {models_dir} нет файлов моделей (.param/.bin)")
                return False
            if param_files and not bin_files:
                self._log(
                    f"❌ В {models_dir} найдены только .param ({len(param_files)} шт), "
                    "но нет .bin файлов."
                )
                self._log(
                    "ℹ️ Для каждой модели нужны ОБЕ пары файлов: model.param + model.bin."
                )
                return False
            if bin_files and not param_files:
                self._log(
                    f"❌ В {models_dir} найдены только .bin ({len(bin_files)} шт), "
                    "но нет .param файлов."
                )
                self._log(
                    "ℹ️ Для каждой модели нужны ОБЕ пары файлов: model.param + model.bin."
                )
                return False

            available = self._list_model_names(models_dir)
            if not available:
                missing_bins = []
                for p in param_files:
                    if not (models_dir / f"{p.stem}.bin").exists():
                        missing_bins.append(p.stem)
                if missing_bins:
                    preview = ", ".join(missing_bins[:5])
                    extra = "" if len(missing_bins) <= 5 else " ..."
                    self._log(
                        "❌ Нет валидных пар моделей (.param + .bin). "
                        f"Без .bin для: {preview}{extra}"
                    )
                else:
                    self._log("❌ Нет доступных моделей")
                return False

            chosen = self._pick_model(
                available, self.s.upscale_model, self.s.upscale_scale
            )
            if not chosen:
                self._log("❌ Не удалось выбрать модель")
                return False

            model_scale = self._parse_scale_from_name(chosen)
            run_scale = model_scale if model_scale else self.s.upscale_scale

            output_path = input_path / "upscale"
            output_path.mkdir(exist_ok=True)

            self._log(f"📦 Модель: {chosen}, масштаб: x{run_scale}")
            self.upscale_start_time = time.time()
            self.estimated_upscale_time = self.estimate_upscale_time(total)
            if self.estimated_upscale_time:
                self._log(
                    f"⏱️ Оценка upscale: {self.format_time(self.estimated_upscale_time)}"
                )
            self._upscale_timer("Прошло: 0 сек")

            cmd = [
                str(exe),
                "-m",
                str(models_dir),
                "-n",
                chosen,
                "-i",
                str(input_path),
                "-o",
                str(output_path),
                "-s",
                str(run_scale),
                "-f",
                "jpg",
                "-t",
                str(self.s.upscale_tile),
                "-j",
                "4:4:4",
                "-g",
                str(self.s.upscale_gpu),
            ]
            self._log("🚀 Запуск upscale...")

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )
            except PermissionError as e:
                self._log(f"❌ Нет прав на запуск бинарника: {exe}")
                self._log("ℹ️ Выполните: chmod +x upscale/tools/realesrgan-ncnn-vulkan")
                self._log(f"Подробности: {e}")
                return False
            lines: List[str] = []

            def read_out() -> None:
                try:
                    assert proc.stdout
                    for line in proc.stdout:
                        line = line.rstrip()
                        if line:
                            lines.append(line)
                            print(f"[UPSCALE] {line}")
                except Exception as e:
                    print(f"[UPSCALE] read error: {e}")

            th = threading.Thread(target=read_out, daemon=True)
            th.start()

            last_count = 0
            stall = 0
            while proc.poll() is None:
                if self.ctrl.should_stop():
                    proc.terminate()
                    break
                try:
                    cur = len(
                        [
                            f
                            for f in output_path.iterdir()
                            if f.is_file()
                            and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                        ]
                    )
                    if cur != last_count:
                        last_count = cur
                        stall = 0
                        pct = min(100, int((cur / total) * 100)) if total else 0
                        self._upscale_progress(f"Upscale: {cur}/{total} ({pct}%)", cur, total)
                    else:
                        stall += 1
                except Exception:
                    pass
                time.sleep(0.5)

            th.join(timeout=2)
            proc.wait()

            if proc.returncode != 0:
                tail = "\n".join(lines[-20:]) if lines else ""
                self._log(f"❌ Ошибка upscale: {tail}")
                return False

            if run_scale != self.s.upscale_scale:
                self._upscale_progress("Ресэмплинг...", 0, 1)
                self._rescale_outputs(output_path, run_scale, self.s.upscale_scale)

            if self.upscale_start_time:
                elapsed = time.time() - self.upscale_start_time
                outs = [
                    f
                    for f in output_path.iterdir()
                    if f.is_file()
                    and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                ]
                if outs:
                    self.add_upscale_timing(len(outs), elapsed)
                    self._log(f"⏱️ Время upscale: {self.format_time(elapsed)}")
                self.upscale_start_time = None
                self.estimated_upscale_time = None
                self._upscale_timer("")

            self._upscale_progress(f"✓ Upscale: {output_path.name}", total, total)
            self._log(f"✅ Upscale завершен: {output_path}")
            return True
        except Exception as e:
            self._log(f"❌ Ошибка upscale: {e}\n{traceback.format_exc()}")
            return False

    def export_metadata_json(
        self,
        folder: str,
        image_urls: List[str],
        url: str,
        downloaded: int,
        failed: int,
        skipped: int,
    ) -> None:
        try:
            metadata: Dict = {
                "download_date": datetime.now().isoformat(),
                "source_url": url,
                "total_found": len(image_urls),
                "downloaded": downloaded,
                "failed": failed,
                "skipped": skipped,
                "download_folder": folder,
                "images": [],
            }
            parser = self._parser
            for index, img_url in enumerate(image_urls):
                try:
                    if self.s.auto_rename and self.s.filename_template:
                        assert parser is not None
                        fn = parser.get_filename_from_url(
                            img_url, index + 1, self.s.filename_template
                        )
                    else:
                        assert parser is not None
                        fn = parser.get_filename_from_url(img_url, index + 1)
                        if self.s.auto_rename:
                            fn = f"pin_{index+1:04d}_{fn}"
                    fp = os.path.join(folder, fn)
                    info = {
                        "index": index + 1,
                        "url": img_url,
                        "filename": fn,
                        "downloaded": os.path.exists(fp),
                    }
                    if os.path.exists(fp):
                        info["file_size"] = os.path.getsize(fp)
                        info["file_size_mb"] = round(
                            os.path.getsize(fp) / (1024 * 1024), 2
                        )
                    metadata["images"].append(info)
                except Exception:
                    pass
            jf = os.path.join(
                folder, f"metadata_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            )
            with open(jf, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            self._log(f"Метаданные: {jf}")
        except Exception as e:
            self._log(f"Ошибка экспорта метаданных: {e}")

    def download_worker(
        self,
        url: str,
        pre_board: Optional[str] = None,
        reuse_parser: bool = False,
        max_images: int = 0,
    ) -> Optional[str]:
        try:
            download_folder = self.s.download_folder
            board_name = pre_board

            if self.s.auto_subfolder and not board_name:
                try:
                    tp = PinterestParser(download_folder=download_folder)
                    expanded = tp.expand_short_url(url)
                    board_name = tp.get_board_name_from_url(expanded)
                    tp.close()
                except Exception:
                    pass

            if self.s.auto_subfolder and board_name:
                decoded = board_name
                if "%" in board_name:
                    try:
                        decoded = unquote(board_name)
                    except Exception:
                        decoded = board_name
                download_folder = os.path.join(self.s.download_folder, decoded)
                os.makedirs(download_folder, exist_ok=True)

            if reuse_parser and self._parser:
                parser = self._parser
                parser.download_folder = download_folder
                parser.setup_download_folder()
            else:
                parser = PinterestParser(download_folder=download_folder)
                parser.setup_download_folder()
                parser.scroll_delay = self.s.scroll_delay
                parser.download_delay = self.s.download_delay
                parser.image_quality = self.s.image_quality
                parser.max_workers = 5
                self._progress_status("Инициализация браузера...")
                parser.init_driver()
                self._parser = parser

            if board_name:
                parser.current_board_name = board_name

            expanded_url = url
            try:
                expanded_url = parser.expand_short_url(url)
            except Exception as e:
                self._log(f"⚠️ Не удалось расширить короткий URL: {e}")

            self._progress_status("Открытие страницы...")
            assert parser.driver
            parser.open_page(expanded_url)

            if not board_name and self.s.auto_subfolder:
                try:
                    board_name = parser.get_board_name_from_url(expanded_url)
                    if board_name:
                        parser.current_board_name = board_name
                        decoded = board_name
                        if "%" in board_name:
                            try:
                                decoded = unquote(board_name)
                            except Exception:
                                decoded = board_name
                        download_folder = os.path.join(self.s.download_folder, decoded)
                        os.makedirs(download_folder, exist_ok=True)
                        parser.download_folder = download_folder
                except Exception:
                    pass

            max_count = self.s.max_images_default if max_images == 0 else max_images
            if max_count > 0:
                self._progress_status(f"Поиск первых {max_count} изображений...")
            else:
                self._progress_status("Поиск изображений...")

            parser.scroll_and_load_images(
                max_images=max_count if max_count > 0 else None
            )
            image_urls = parser.extract_image_urls(
                max_images=max_count if max_count > 0 else None
            )

            if max_count > 0 and image_urls:
                self._log(f"Найдено {len(image_urls)} изображений для скачивания")

            self.image_urls_list = image_urls
            if self._urls_discovered:
                try:
                    self._urls_discovered(image_urls)
                except Exception:
                    pass
            self.stats["found"] += len(image_urls)
            self.total_images_to_download += len(image_urls)
            self._emit_stats()
            self._progress_bar(
                self.current_downloaded_count, max(1, self.total_images_to_download)
            )

            if not image_urls:
                self._log("⚠️ Изображения не найдены")
                if not reuse_parser:
                    parser.close()
                    self._parser = None
                return download_folder

            self._log(f"✓ Найдено {len(image_urls)} изображений")
            self.download_start_time = time.time()
            est = self.estimate_download_time(len(image_urls))
            self.estimated_download_time = est
            if est:
                self._log(f"⏱️ Оценка времени скачивания: {self.format_time(est)}")
            self._download_timer("Прошло: 0 сек")
            last_timer_emit = 0.0

            job_key = self._current_job
            job_bytes = 0
            job_t0 = time.time()

            def job_tick(done: int) -> None:
                # done/total по текущей доске и средняя скорость в байтах/с
                if self._job_progress and job_key is not None:
                    elapsed = max(0.001, time.time() - job_t0)
                    self._job_progress(job_key, done, len(image_urls), job_bytes / elapsed)

            job_tick(0)

            downloaded = failed = skipped = 0
            # Счётчики в self.stats — общие для всех досок запуска
            base = dict(self.stats)
            for index, img_url in enumerate(image_urls):
                if self.ctrl.should_stop():
                    break
                while self.ctrl.is_paused() and not self.ctrl.should_stop():
                    time.sleep(0.5)
                if self.ctrl.should_stop():
                    break

                now = time.time()
                if self.download_start_time and now - last_timer_emit >= 1.0:
                    last_timer_emit = now
                    el = now - self.download_start_time
                    extra = ""
                    if self.estimated_download_time is not None:
                        extra = " | Осталось: " + self.format_time(
                            max(0.0, self.estimated_download_time - el)
                        )
                    self._download_timer(f"Прошло: {self.format_time(el)}{extra}")

                full_url = None
                try:
                    full_url = parser.get_full_image_url(img_url, parser.image_quality)
                except Exception as e:
                    self._log(f"❌ Ошибка URL {img_url[:50]}...: {e}")
                    failed += 1
                    self.current_downloaded_count += 1
                    self._progress_bar(
                        self.current_downloaded_count,
                        max(1, self.total_images_to_download),
                    )
                    self._progress_status(
                        f"Скачивание: {index+1}/{len(image_urls)} "
                        f"(всего: {self.current_downloaded_count}/{self.total_images_to_download})"
                    )
                    self.stats["failed"] = base["failed"] + failed
                    self._emit_stats()
                    job_tick(index + 1)
                    continue

                if not full_url:
                    failed += 1
                    self.current_downloaded_count += 1
                    self._progress_bar(
                        self.current_downloaded_count,
                        max(1, self.total_images_to_download),
                    )
                    self.stats["failed"] = base["failed"] + failed
                    self._emit_stats()
                    job_tick(index + 1)
                    continue

                if self.s.auto_rename and self.s.filename_template:
                    filename = parser.get_filename_from_url(
                        full_url, index + 1, self.s.filename_template
                    )
                else:
                    filename = parser.get_filename_from_url(full_url, index + 1)
                    if self.s.auto_rename:
                        filename = f"pin_{index+1:04d}_{filename}"

                filepath = os.path.join(parser.download_folder, filename)

                if os.path.exists(filepath) and self.s.resume_download:
                    skipped += 1
                    self.current_downloaded_count += 1
                    self._progress_bar(
                        self.current_downloaded_count,
                        max(1, self.total_images_to_download),
                    )
                    self._log(f"⏭ Пропущено (уже есть): {filename}")
                    self.stats["skipped"] = base["skipped"] + skipped
                    self._emit_stats()
                    job_tick(index + 1)
                    continue
                if os.path.exists(filepath) and not self.s.resume_download:
                    try:
                        os.remove(filepath)
                    except Exception as e:
                        self._log(f"⚠️ Не удалось удалить файл: {e}")

                self._log(f"⬇ Скачиваю: {filename}")
                ok = False
                try:
                    ok = bool(parser.download_image(full_url, filename))
                except Exception as e:
                    self._log(f"❌ Исключение при скачивании {filename}: {e}")

                if ok:
                    try:
                        if os.path.exists(filepath):
                            job_bytes += os.path.getsize(filepath)
                            sz_mb = os.path.getsize(filepath) / (1024 * 1024)
                            if sz_mb < self.s.min_size_mb or sz_mb > self.s.max_size_mb:
                                try:
                                    os.remove(filepath)
                                except Exception:
                                    pass
                                skipped += 1
                                self.current_downloaded_count += 1
                                self._log(
                                    f"⏭ Пропущено (размер {sz_mb:.2f} МБ): {filename}"
                                )
                            else:
                                downloaded += 1
                                self.current_downloaded_count += 1
                                self._log(f"✓ Скачано ({sz_mb:.2f} МБ): {filename}")
                        else:
                            failed += 1
                            self._log(f"❌ Файл не создан: {filename}")
                    except Exception as e:
                        self._log(f"⚠️ Ошибка проверки размера {filename}: {e}")
                        if os.path.exists(filepath):
                            downloaded += 1
                            self.current_downloaded_count += 1
                else:
                    failed += 1
                    self.current_downloaded_count += 1
                    self._log(f"❌ Ошибка скачивания: {filename}")

                self._progress_bar(
                    self.current_downloaded_count, max(1, self.total_images_to_download)
                )
                self._progress_status(
                    f"Скачивание: {index+1}/{len(image_urls)} "
                    f"(всего: {self.current_downloaded_count}/{self.total_images_to_download})"
                )
                self.stats["downloaded"] = base["downloaded"] + downloaded
                self.stats["skipped"] = base["skipped"] + skipped
                self.stats["failed"] = base["failed"] + failed
                self._emit_stats()
                job_tick(index + 1)
                time.sleep(self.s.download_delay)

            if self.download_start_time:
                elapsed = time.time() - self.download_start_time
                if downloaded + skipped > 0:
                    self.add_download_timing(downloaded + skipped, elapsed)
                    self._log(f"⏱️ Время скачивания: {self.format_time(elapsed)}")
                self.download_start_time = None
                self.estimated_download_time = None
                self._download_timer("")

            self._log("\n✓ Скачивание завершено!")
            self._log(
                f"Успешно: {downloaded} | Ошибок: {failed} | Пропущено: {skipped}"
            )

            if self.s.export_metadata:
                self.export_metadata_json(
                    parser.download_folder, image_urls, url, downloaded, failed, skipped
                )

            if self.s.notify_on_complete:
                self._notify(
                    "Скачивание завершено",
                    f"Успешно: {downloaded} | Ошибок: {failed} | Пропущено: {skipped}",
                )

            saved_board = board_name
            if board_name and "%" in board_name:
                try:
                    saved_board = unquote(board_name)
                except Exception:
                    pass
            self.history.append(
                {
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "url": url,
                    "board_name": saved_board,
                    "count": downloaded,
                    "total": len(image_urls),
                }
            )
            self.save_history()

            if not reuse_parser:
                parser.close()
                self._parser = None

            return parser.download_folder

        except Exception as e:
            self._log(f"Ошибка: {e}")
            if self._parser:
                try:
                    self._parser.close()
                except Exception:
                    pass
                self._parser = None
            return None

    def run_multi(self, url_jobs: Union[List[Dict], JobQueue]) -> None:
        """
        url_jobs: [{"url": str, "board_name": str|None, "max_images": int}, ...]
        или JobQueue, которую интерфейс может менять во время работы.
        """
        queue = url_jobs if isinstance(url_jobs, JobQueue) else JobQueue(url_jobs)
        all_folders: List[str] = []
        if not queue.has_pending():
            self._log("\n=== Все задачи завершены ===")
            return

        # Один браузер на все доски (как в pinterest_gui.py)
        base = PinterestParser(download_folder=self.s.download_folder)
        base.scroll_delay = self.s.scroll_delay
        base.download_delay = self.s.download_delay
        base.image_quality = self.s.image_quality
        base.max_workers = 5
        self._progress_status("Инициализация браузера...")
        try:
            base.init_driver()
        except Exception as e:
            self._log(f"❌ Не удалось запустить браузер: {e}\n{traceback.format_exc()}")
            return
        self._parser = base

        try:
            idx = 0
            while not self.ctrl.should_stop():
                job = queue.pop_next()
                if job is None:
                    break
                key = JobQueue.key(job)
                url = job["url"]
                board = job.get("board_name")
                mi = int(job.get("max_images") or 0)
                disp = f"{board} - {url}" if board else url
                md = f" (макс. {mi})" if mi > 0 else " (все изображения)"
                self._log(f"\n=== Доска {idx+1}: {disp}{md} ===")
                self._current_job = key
                if self._job_started:
                    self._job_started(key)
                folder = None
                try:
                    folder = self.download_worker(
                        url, board, reuse_parser=True, max_images=mi
                    )
                    if folder:
                        all_folders.append(folder)
                except Exception as e:
                    self._log(f"❌ Ошибка URL: {e}\n{traceback.format_exc()}")
                self._current_job = None
                if self._job_finished:
                    self._job_finished(key, bool(folder))
                idx += 1
                if queue.has_pending() and not self.ctrl.should_stop():
                    time.sleep(2)
        finally:
            if self._parser:
                try:
                    self._log("Закрываю браузер...")
                    self._parser.close()
                except Exception as e:
                    self._log(f"Ошибка закрытия браузера: {e}")
                self._parser = None

        if self.s.enable_upscale and all_folders and not self.ctrl.should_stop():
            self._log("\n=== Запуск upscale ===")
            for folder in all_folders:
                if self.ctrl.should_stop():
                    break
                ok = self.run_upscale(folder)
                if not ok:
                    self._log("⛔ Upscale остановлен: сначала исправьте ошибку выше.")
                    break

        self._log("\n=== Все задачи завершены ===")
