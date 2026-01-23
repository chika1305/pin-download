"""
Pinterest Image Downloader - GUI приложение для Windows
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import json
import os
import time
import hashlib
import re
import subprocess
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageTk
import requests
from io import BytesIO
from urllib.parse import unquote
try:
    from win10toast import ToastNotifier
    HAS_TOAST = True
except ImportError:
    HAS_TOAST = False
from pinterest_parser import PinterestParser


class PinterestDownloaderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Pinterest Image Downloader")
        self.root.geometry("750x800")  # Компактная ширина окна
        self.root.resizable(True, True)

        # Настройка стиля macOS
        self.setup_macos_style()

        # Переменные состояния
        self.parser = None  # Переиспользуемый парсер для всех URL
        self.is_downloading = False
        self.is_paused = False
        self.download_thread = None
        self.total_images_to_download = 0  # Общее количество изображений для прогресс-бара
        self.current_downloaded_count = 0  # Текущее количество скачанных
        self.max_images = tk.IntVar(value=0)  # 0 = все изображения
        self.download_folder = tk.StringVar(value="pinterest_images")
        self.image_quality = tk.StringVar(value="full")  # full, medium, small
        self.min_size_mb = tk.DoubleVar(value=0.0)
        self.max_size_mb = tk.DoubleVar(value=1000.0)
        self.auto_rename = tk.BooleanVar(value=True)
        self.auto_subfolder = tk.BooleanVar(value=True)  # Автоподпапки
        self.resume_download = tk.BooleanVar(value=True)  # Продолжение скачивания
        self.windows_notifications = tk.BooleanVar(value=True)  # Уведомления Windows
        self.export_metadata = tk.BooleanVar(value=False)  # Экспорт метаданных
        self.filename_template = tk.StringVar(value="{index04}_{hash}.jpg")  # Шаблон имени файла
        self.scroll_delay = tk.DoubleVar(value=2.0)
        self.download_delay = tk.DoubleVar(value=0.5)
        self.history_file = "download_history.json"
        self.timing_stats_file = "timing_stats.json"  # Файл для статистики времени

        # Множественные URL - храним словари с URL, названием доски и количеством изображений
        self.url_list = []  # Список словарей: [{"url": "...", "board_name": "...", "max_images": 0}, ...]
        self.current_url_index = 0  # Текущий индекс URL

        # Статистика времени для оценки оставшегося времени
        self.timing_stats = self.load_timing_stats()
        self.download_start_time = None
        self.upscale_start_time = None
        self.estimated_download_time = None
        self.estimated_upscale_time = None

        # Upscale настройки
        self.enable_upscale = tk.BooleanVar(value=False)
        self.upscale_scale = tk.IntVar(value=3)  # 2, 3, 4
        self.upscale_model = tk.StringVar(value="auto")  # auto, photo, anime
        self.upscale_tile = tk.IntVar(value=200)
        self.upscale_gpu = tk.IntVar(value=0)

        # Загружаем историю
        self.history = self.load_history()

        self.create_widgets()

    def setup_macos_style(self):
        """Настройка стиля macOS для всех виджетов"""
        style = ttk.Style()

        # Определяем доступные шрифты (macOS или альтернативные)
        import tkinter.font as tkfont
        available_fonts = tkfont.families()

        # Пробуем найти macOS шрифты, иначе используем системные альтернативы
        if "SF Pro Display" in available_fonts:
            font_family = "SF Pro Display"
            font_mono = "SF Mono"
        elif "Helvetica Neue" in available_fonts:
            font_family = "Helvetica Neue"
            font_mono = "Menlo"
        elif "Segoe UI" in available_fonts:
            font_family = "Segoe UI"
            font_mono = "Consolas"
        else:
            font_family = "Arial"
            font_mono = "Courier New"

        # Цветовая схема macOS согласно спецификации
        bg_color = "#F5F5F7"  # bg.window
        frame_bg = "#FFFFFF"  # bg.content
        sidebar_bg = "#F2F2F4"  # bg.sidebar
        text_primary = "#111111"  # text.primary
        text_secondary = "#6B6B6F"  # text.secondary
        text_tertiary = "#8E8E93"  # text.tertiary
        separator_color = "#D9D9DE"  # separator
        control_fill = "#FFFFFF"  # control.fill
        control_hover = "#F2F2F5"  # control.hover
        control_active = "#EAEAEE"  # control.active
        accent_color = "#0A84FF"  # accent (macOS blue)
        success_color = "#34C759"  # success
        warning_color = "#FF9F0A"  # warning
        danger_color = "#FF3B30"  # danger

        # Используем для совместимости
        text_color = text_primary
        border_color = separator_color

        # Сохраняем шрифты для использования
        self.font_family = font_family
        self.font_mono = font_mono

        # Настройка фона окна
        self.root.configure(bg=bg_color)

        # Стиль для Frame
        style.configure("Mac.TFrame", background=frame_bg, relief="flat")
        style.configure("MacCard.TFrame", background=frame_bg, relief="flat")

        # Стиль для LabelFrame (карточки)
        style.configure("Mac.TLabelframe",
                       background=frame_bg,
                       foreground=text_color,
                       borderwidth=0,
                       relief="flat")
        style.configure("Mac.TLabelframe.Label",
                       background=frame_bg,
                       foreground=text_color,
                       font=(font_family, 13, "bold"))

        # Стиль для Label согласно спецификации
        style.configure("Mac.TLabel",
                       background=frame_bg,
                       foreground=text_primary,
                       font=(font_family, 13))  # Body: 13px
        style.configure("MacTitle.TLabel",
                       background=bg_color,
                       foreground=text_primary,
                       font=(font_family, 15, "semibold"))  # Title: 13-15px semibold
        style.configure("MacSubtitle.TLabel",
                       background=frame_bg,
                       foreground=text_secondary,
                       font=(font_family, 12))  # Secondary: 11-12px
        style.configure("MacHint.TLabel",
                       background=frame_bg,
                       foreground=text_tertiary,
                       font=(font_family, 11))  # Tertiary: 11-12px

        # Стиль для Entry согласно спецификации (TextField)
        style.configure("Mac.TEntry",
                       fieldbackground=control_fill,
                       foreground=text_primary,
                       borderwidth=1,
                       relief="solid",
                       padding=(12, 8),  # padding слева 12-14px
                       font=(font_family, 13))  # Body: 13px
        style.map("Mac.TEntry",
                 fieldbackground=[("focus", control_fill), ("hover", control_fill)],
                 bordercolor=[("focus", accent_color), ("!focus", separator_color)],
                 lightcolor=[("focus", accent_color)],
                 darkcolor=[("focus", accent_color)])

        # Стиль для Button согласно спецификации (Primary)
        style.configure("Mac.TButton",
                       background=accent_color,
                       foreground="#FFFFFF",
                       borderwidth=0,
                       relief="flat",
                       padding=(12, 8),  # 10-12px по горизонтали
                       font=(font_family, 13, "semibold"))  # 13px semibold
        style.map("Mac.TButton",
                 background=[("active", "#0051D5"), ("pressed", "#0040B3"), ("disabled", "#C7C7CC")],
                 foreground=[("disabled", text_secondary)])

        # Стиль для вторичной кнопки (Secondary)
        style.configure("MacSecondary.TButton",
                       background=control_fill,
                       foreground=text_primary,
                       borderwidth=1,
                       relief="solid",
                       padding=(12, 8),
                       font=(font_family, 13))
        style.map("MacSecondary.TButton",
                 background=[("active", control_hover), ("pressed", control_active)],
                 bordercolor=[("!focus", separator_color)],
                 foreground=[("disabled", text_secondary)])

        # Стиль для маленькой кнопки
        style.configure("MacSmall.TButton",
                       background="#E5E5EA",
                       foreground=text_color,
                       borderwidth=0,
                       relief="flat",
                       padding=(8, 4),
                       font=(font_family, 10))

        # Стиль для Checkbutton согласно спецификации (14x14px, радиус 3px)
        style.configure("Mac.TCheckbutton",
                       background=frame_bg,
                       foreground=text_primary,
                       font=(font_family, 13),
                       focuscolor="none",
                       indicatorsize=14)  # 14x14px
        style.map("Mac.TCheckbutton",
                 background=[("selected", accent_color), ("active", control_hover)],
                 indicatorcolor=[("selected", accent_color), ("!selected", control_fill)],
                 bordercolor=[("selected", accent_color), ("!selected", separator_color)])

        # Стиль для Radiobutton согласно спецификации (14x14px, внутренняя точка 6x6px)
        style.configure("Mac.TRadiobutton",
                       background=frame_bg,
                       foreground=text_primary,
                       font=(font_family, 13),
                       focuscolor="none",
                       indicatorsize=14)  # 14x14px
        style.map("Mac.TRadiobutton",
                 background=[("active", control_hover)],
                 indicatorcolor=[("selected", accent_color), ("!selected", control_fill)],
                 bordercolor=[("selected", accent_color), ("!selected", separator_color)])

        # Стиль для Spinbox согласно спецификации
        style.configure("Mac.TSpinbox",
                       fieldbackground=control_fill,
                       foreground=text_primary,
                       borderwidth=1,
                       relief="solid",
                       padding=(8, 6),
                       font=(font_family, 13))
        style.map("Mac.TSpinbox",
                 fieldbackground=[("focus", control_fill)],
                 bordercolor=[("focus", accent_color), ("!focus", separator_color)])

        # Стиль для Progressbar настраивается позже в create_widgets
        # Используем стандартный TProgressbar с кастомными цветами

        # Стиль для Notebook (вкладки)
        style.configure("Mac.TNotebook",
                       background=bg_color,
                       borderwidth=0)
        style.configure("Mac.TNotebook.Tab",
                       background="#E5E5EA",
                       foreground=text_color,
                       padding=(20, 10),
                       font=(font_family, 11))
        style.map("Mac.TNotebook.Tab",
                 background=[("selected", frame_bg)],
                 expand=[("selected", [1, 1, 1, 0])])

        # Стиль для Scrollbar
        style.configure("Mac.TScrollbar",
                       background="#E5E5EA",
                       troughcolor=bg_color,
                       borderwidth=0,
                       arrowcolor=text_color,
                       darkcolor="#E5E5EA",
                       lightcolor="#E5E5EA")

        # Сохраняем стили для использования
        self.style = style
        self.bg_color = bg_color
        self.frame_bg = frame_bg
        self.sidebar_bg = sidebar_bg
        self.text_color = text_primary
        self.text_primary = text_primary
        self.text_secondary = text_secondary
        self.text_tertiary = text_tertiary
        self.separator_color = separator_color
        self.control_fill = control_fill
        self.control_hover = control_hover
        self.control_active = control_active
        self.accent_color = accent_color
        self.success_color = success_color
        self.warning_color = warning_color
        self.danger_color = danger_color

    def safe_after(self, delay, func, *args, **kwargs):
        """
        Безопасный вызов root.after() с проверкой существования окна
        Гарантирует, что функция всегда возвращает int (для WNDPROC)
        """
        try:
            if self.root.winfo_exists():
                def wrapper():
                    try:
                        if self.root.winfo_exists():
                            result = func(*args, **kwargs)
                            # Гарантируем возврат int для WNDPROC
                            return result if isinstance(result, int) else 0
                        return 0
                    except Exception as e:
                        print(f"Ошибка в safe_after: {e}")
                        return 0
                self.root.after(delay, wrapper)
        except Exception as e:
            print(f"Ошибка при планировании обновления UI: {e}")

    def safe_update_ui(self, func, *args, **kwargs):
        """
        Безопасное обновление UI с гарантией возврата int
        Используется для методов, которые могут возвращать None
        """
        def wrapper():
            try:
                result = func(*args, **kwargs)
                return result if isinstance(result, int) else 0
            except Exception as e:
                print(f"Ошибка в safe_update_ui: {e}")
                return 0
        self.safe_after(0, wrapper)

    def create_rounded_frame(self, parent, bg_color, radius=12):
        """Создает фрейм со скругленными углами"""
        # Контейнер с отступами для визуального скругления
        container = tk.Frame(parent, bg=self.bg_color)
        # Canvas для рисования скругленного фона
        canvas = tk.Canvas(container, bg=self.bg_color, highlightthickness=0, borderwidth=0)
        canvas.pack(fill=tk.BOTH, expand=True)
        # Настраиваем контейнер для растягивания
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        # Внутренний фрейм для содержимого
        inner_frame = tk.Frame(canvas, bg=bg_color)
        inner_frame_window = None

        def draw_rounded_rect(event=None):
            nonlocal inner_frame_window
            canvas.delete("all")
            width = canvas.winfo_width()
            if width > 1:
                # Обновляем размеры внутреннего фрейма для получения требуемой высоты
                inner_frame.update_idletasks()
                content_height = inner_frame.winfo_reqheight()
                # Используем требуемую высоту содержимого + отступы
                actual_height = content_height + radius
                
                # Рисуем скругленный прямоугольник с закругленными углами
                # Верхняя левая дуга
                canvas.create_arc(0, 0, radius*2, radius*2, start=90, extent=90,
                                 fill=bg_color, outline=bg_color, style="pieslice")
                # Верхняя правая дуга
                canvas.create_arc(width-radius*2, 0, width, radius*2, start=0, extent=90,
                                 fill=bg_color, outline=bg_color, style="pieslice")
                # Нижняя правая дуга
                canvas.create_arc(width-radius*2, actual_height-radius*2, width, actual_height, start=270, extent=90,
                                 fill=bg_color, outline=bg_color, style="pieslice")
                # Нижняя левая дуга
                canvas.create_arc(0, actual_height-radius*2, radius*2, actual_height, start=180, extent=90,
                                 fill=bg_color, outline=bg_color, style="pieslice")
                # Центральный прямоугольник
                canvas.create_rectangle(radius, 0, width-radius, actual_height,
                                       fill=bg_color, outline=bg_color)
                # Вертикальные прямоугольники
                canvas.create_rectangle(0, radius, width, actual_height-radius,
                                       fill=bg_color, outline=bg_color)
                
                # Размещаем внутренний фрейм поверх Canvas с отступами
                if inner_frame_window:
                    canvas.delete(inner_frame_window)
                # Не указываем height, чтобы внутренний фрейм мог определять свою высоту автоматически
                inner_frame_window = canvas.create_window(radius//2, radius//2,
                                    window=inner_frame, anchor="nw",
                                    width=max(1, width-radius))
                
                # Обновляем область прокрутки canvas для отображения всего содержимого
                canvas.update_idletasks()
                # Получаем актуальную высоту после размещения фрейма
                bbox = canvas.bbox(inner_frame_window)
                if bbox:
                    frame_bottom = bbox[3]
                    final_height = max(actual_height, frame_bottom + radius//2)
                    canvas.configure(scrollregion=(0, 0, width, final_height))
                    # Обновляем размер canvas и контейнера, чтобы они соответствовали содержимому
                    canvas.configure(height=final_height)
                    container.configure(height=final_height)
                else:
                    canvas.configure(scrollregion=(0, 0, width, actual_height))
                    canvas.configure(height=actual_height)
                    container.configure(height=actual_height)

        def on_inner_frame_configure(event=None):
            """Вызывается при изменении размера внутреннего фрейма"""
            # Перерисовываем фон при изменении содержимого
            canvas.after_idle(draw_rounded_rect)

        # Привязываем обработчики событий
        canvas.bind('<Configure>', draw_rounded_rect)
        inner_frame.bind('<Configure>', on_inner_frame_configure)
        
        # Инициализируем отрисовку
        container.update_idletasks()
        # Запускаем обновление после создания всех виджетов
        container.after(100, draw_rounded_rect)

        return container, inner_frame

    def create_widgets(self):
        """Создание интерфейса в стиле macOS"""
        # Создаем Canvas с прокруткой для всего содержимого
        canvas_container = tk.Frame(self.root, bg=self.bg_color)
        canvas_container.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Настройка весов для растягивания
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        canvas_container.columnconfigure(0, weight=1)
        canvas_container.rowconfigure(0, weight=1)

        # Canvas для прокрутки
        self.main_canvas = tk.Canvas(canvas_container, bg=self.bg_color, highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_container, orient="vertical", command=self.main_canvas.yview)
        scrollable_frame = tk.Frame(self.main_canvas, bg=self.bg_color)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))
        )

        self.main_canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        self.main_canvas.configure(yscrollcommand=scrollbar.set)

        # Привязка прокрутки колесиком мыши
        def on_mousewheel(event):
            self.main_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def bind_mousewheel(event):
            self.main_canvas.bind_all("<MouseWheel>", on_mousewheel)

        def unbind_mousewheel(event):
            self.main_canvas.unbind_all("<MouseWheel>")

        self.main_canvas.bind('<Enter>', bind_mousewheel)
        self.main_canvas.bind('<Leave>', unbind_mousewheel)

        # Обновление области прокрутки при изменении размера окна
        def update_scroll_region(event=None):
            self.main_canvas.update_idletasks()
            self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))

        scrollable_frame.bind('<Configure>', update_scroll_region)

        def on_canvas_configure(event):
            # Обновляем ширину scrollable_frame при изменении размера Canvas
            canvas_width = event.width
            canvas_items = self.main_canvas.find_all()
            if canvas_items:
                self.main_canvas.itemconfig(canvas_items[0], width=canvas_width)
            update_scroll_region()

        self.main_canvas.bind('<Configure>', on_canvas_configure)

        self.main_canvas.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))

        # Сохраняем ссылку на scrollable_frame для обновления
        self.scrollable_frame = scrollable_frame

        # Главный контейнер с отступами внутри прокручиваемого фрейма
        main_frame = tk.Frame(scrollable_frame, bg=self.bg_color, padx=20, pady=20)
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.columnconfigure(0, weight=1)

        row = 0

        # Заголовок в стиле macOS
        title_frame = tk.Frame(main_frame, bg=self.bg_color)
        title_frame.grid(row=row, column=0, sticky=(tk.W, tk.E), pady=(0, 30))
        row += 1

        title_label = tk.Label(title_frame, text="Pinterest Image Downloader",
                               bg=self.bg_color, fg=self.text_color,
                               font=(self.font_family, 28, "bold"))
        title_label.pack(anchor="w")

        subtitle_label = tk.Label(title_frame, text="Скачивайте изображения с Pinterest быстро и удобно",
                                 bg=self.bg_color, fg="#6E6E73",
                                 font=(self.font_family, 13))
        subtitle_label.pack(anchor="w", pady=(5, 0))

        # === СЕКЦИЯ 1: URL и основные настройки ===
        # Создаем скругленный фрейм
        settings_container, settings_frame = self.create_rounded_frame(main_frame, self.frame_bg, radius=12)
        settings_container.grid(row=row, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 15))  # Добавлено вертикальное растягивание
        # Настраиваем main_frame для растягивания строки с settings_container
        main_frame.rowconfigure(row, weight=0, minsize=600)  # Устанавливаем минимальную высоту для строки с секцией
        settings_frame.columnconfigure(0, weight=1)  # Теперь все элементы в одной колонке
        settings_frame.rowconfigure(4, weight=1, minsize=200)  # Разрешаем растягивание строки с url_list_frame (строка 4, а не 3)
        # Добавляем отступы для содержимого (уменьшены для более компактного вида)
        settings_frame.configure(padx=20, pady=35)
        row += 1

        # Заголовок секции
        section_title = tk.Label(settings_frame, text="Основные настройки",
                                bg=self.frame_bg, fg=self.text_color,
                                font=(self.font_family, 15, "bold"))
        section_title.grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 15))

        # Количество изображений - глобальная настройка (по умолчанию для новых URL)
        ttk.Label(settings_frame, text="Количество изображений (по умолчанию):", style="Mac.TLabel").grid(row=1, column=0, sticky=tk.W, pady=(0, 5))
        images_frame = tk.Frame(settings_frame, bg=self.frame_bg)
        images_frame.grid(row=2, column=0, sticky=tk.W, pady=(0, 12))

        self.images_spinbox = ttk.Spinbox(images_frame, from_=0, to=10000,
                                          textvariable=self.max_images, width=15, style="Mac.TSpinbox")
        self.images_spinbox.grid(row=0, column=0, padx=(0, 10))
        ttk.Label(images_frame, text="(0 = все изображения, можно изменить для каждой ссылки)", style="MacSubtitle.TLabel").grid(row=0, column=1)

        # URL доски - список URL
        ttk.Label(settings_frame, text="URL досок/страниц:", style="Mac.TLabel").grid(row=3, column=0, sticky=tk.W, pady=(15, 5))

        url_list_frame = tk.Frame(settings_frame, bg=self.frame_bg)
        url_list_frame.grid(row=4, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 12))  # Уменьшены отступы
        url_list_frame.columnconfigure(0, weight=1, minsize=450)  # Уменьшена минимальная ширина для контейнера
        url_list_frame.rowconfigure(1, weight=0, minsize=100)  # Значительно уменьшена минимальная высота для строки с таблицей

        # Поле ввода URL
        url_input_frame = tk.Frame(url_list_frame, bg=self.frame_bg)
        url_input_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 5))
        url_input_frame.columnconfigure(0, weight=1)

        self.url_entry = ttk.Entry(url_input_frame, width=28, style="Mac.TEntry")
        self.url_entry.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=(0, 8))

        # Контекстное меню для вставки (стилизовано под macOS)
        context_menu = tk.Menu(self.root, tearoff=0, bg="#FFFFFF", fg=self.text_color,
                              activebackground="#E5E5EA", activeforeground=self.text_color,
                              font=(self.font_family, 11), relief="flat", borderwidth=0)
        context_menu.add_command(label="Вставить", command=lambda: self.paste_to_entry())
        context_menu.add_command(label="Вырезать", command=lambda: self.url_entry.event_generate("<<Cut>>"))
        context_menu.add_command(label="Копировать", command=lambda: self.url_entry.event_generate("<<Copy>>"))
        context_menu.add_separator()
        context_menu.add_command(label="Выделить все", command=lambda: self.url_entry.select_range(0, tk.END))

        def show_context_menu(event):
            try:
                context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                context_menu.grab_release()

        def paste_to_entry():
            try:
                clipboard_text = self.root.clipboard_get()
                if clipboard_text:
                    # Удаляем выделенный текст если есть
                    if self.url_entry.selection_present():
                        self.url_entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
                    # Вставляем из буфера обмена
                    self.url_entry.insert(tk.INSERT, clipboard_text)
            except:
                pass

        self.paste_to_entry = paste_to_entry

        # Выделение всего текста при фокусе для удобной замены
        def on_entry_focus(event):
            # Небольшая задержка для корректной работы
            self.root.after(10, lambda: self.url_entry.select_range(0, tk.END))

        # Поддержка вставки через Ctrl+V
        def on_paste(event):
            paste_to_entry()
            return "break"  # Предотвращаем стандартную обработку

        self.url_entry.bind('<FocusIn>', on_entry_focus)
        self.url_entry.bind('<Control-v>', on_paste)
        self.url_entry.bind('<Button-3>', show_context_menu)  # Правая кнопка мыши
        self.url_entry.bind('<Button-1>', lambda e: self.root.after(10, lambda: self.url_entry.select_range(0, tk.END)))

        # Устанавливаем фокус на поле ввода при запуске для удобства
        self.root.after(100, lambda: self.url_entry.focus_set())

        # Кнопка очистки
        clear_btn = ttk.Button(url_input_frame, text="✕", width=3, command=lambda: self.url_entry.delete(0, tk.END), style="MacSmall.TButton")
        clear_btn.grid(row=0, column=1, padx=(0, 8))

        # Кнопка добавления URL в список
        add_url_btn = ttk.Button(url_input_frame, text="Добавить", command=self.add_url_to_list, style="MacSecondary.TButton")
        add_url_btn.grid(row=0, column=2, padx=(0, 8))

        # История URL
        history_btn = ttk.Button(url_input_frame, text="История", command=self.show_history, style="MacSecondary.TButton")
        history_btn.grid(row=0, column=3)

        # Список URL с прокруткой
        url_list_container = tk.Frame(url_list_frame, bg=self.frame_bg)
        url_list_container.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 0))
        url_list_container.columnconfigure(0, weight=1)
        url_list_container.rowconfigure(0, weight=1)

        # Listbox для отображения URL
        url_listbox_frame = tk.Frame(url_list_container, bg=self.frame_bg)
        url_listbox_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        url_listbox_frame.columnconfigure(0, weight=1)
        url_listbox_frame.rowconfigure(0, weight=1)

        url_scrollbar = ttk.Scrollbar(url_listbox_frame)
        url_scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))

        # Используем Treeview вместо Listbox для отображения URL с количеством изображений
        url_tree_frame = tk.Frame(url_listbox_frame, bg=self.frame_bg)
        url_tree_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        url_tree_frame.columnconfigure(0, weight=1)
        url_tree_frame.rowconfigure(0, weight=1)

        # Treeview для отображения URL с колонками
        columns = ("board", "url", "max_images")
        self.url_treeview = ttk.Treeview(url_tree_frame, columns=columns, show="headings", height=3, selectmode="extended")  # Уменьшена высота до 3 строк
        self.url_treeview.heading("board", text="Название доски")
        self.url_treeview.heading("url", text="URL")
        self.url_treeview.heading("max_images", text="Макс.")  # Короткий заголовок для экономии места

        # Настройка ширины колонок (оптимизированы для полного отображения всех заголовков)
        # Используем stretch=True для колонок, чтобы они могли растягиваться
        self.url_treeview.column("board", width=70, anchor="w", stretch=False)  # Уменьшено для компактности
        self.url_treeview.column("url", width=220, anchor="w", stretch=True)  # Растягивается для заполнения пространства
        self.url_treeview.column("max_images", width=50, anchor="center", stretch=False)  # Уменьшено для компактности

        url_tree_scrollbar = ttk.Scrollbar(url_tree_frame, orient="vertical", command=self.url_treeview.yview)
        self.url_treeview.configure(yscrollcommand=url_tree_scrollbar.set)

        self.url_treeview.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        url_tree_scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))

        # Двойной клик для редактирования количества изображений
        self.url_treeview.bind("<Double-1>", self.edit_url_max_images)

        # Для обратной совместимости создаем url_listbox (скрытый)
        self.url_listbox = self.url_treeview

        # Кнопки управления списком URL
        url_buttons_frame = tk.Frame(url_list_frame, bg=self.frame_bg)
        url_buttons_frame.grid(row=2, column=0, sticky=tk.W, pady=(5, 0))  # Уменьшен отступ для кнопок

        ttk.Button(url_buttons_frame, text="Удалить выбранный", command=self.remove_selected_url,
                  style="MacSecondary.TButton").grid(row=0, column=0, padx=(0, 8))
        ttk.Button(url_buttons_frame, text="Очистить список", command=self.clear_url_list,
                  style="MacSecondary.TButton").grid(row=0, column=1, padx=(0, 8))
        ttk.Button(url_buttons_frame, text="Обновить названия", command=self.refresh_board_names,
                  style="MacSecondary.TButton").grid(row=0, column=2)

        # Папка для сохранения
        ttk.Label(settings_frame, text="Папка для сохранения:", style="Mac.TLabel").grid(row=5, column=0, sticky=tk.W, pady=(20, 5))
        folder_frame = tk.Frame(settings_frame, bg=self.frame_bg)
        folder_frame.grid(row=6, column=0, sticky=(tk.W, tk.E), pady=(0, 12))
        folder_frame.columnconfigure(0, weight=1)

        self.folder_entry = ttk.Entry(folder_frame, textvariable=self.download_folder, width=25, style="Mac.TEntry")
        self.folder_entry.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=(0, 8))
        ttk.Button(folder_frame, text="Выбрать", command=self.select_folder, style="MacSecondary.TButton").grid(row=0, column=1)

        # === СЕКЦИЯ 2: Дополнительные настройки ===
        # Создаем скругленный фрейм
        advanced_container, advanced_frame = self.create_rounded_frame(main_frame, self.frame_bg, radius=12)
        advanced_container.grid(row=row, column=0, sticky=(tk.W, tk.E), pady=(0, 15))
        # Сохраняем ссылку на контейнер для последующего обновления
        self.advanced_container = advanced_container
        advanced_frame.columnconfigure(0, weight=1)  # Теперь все элементы в одной колонке
        # Добавляем отступы для содержимого (уменьшены для компактности)
        advanced_frame.configure(padx=20, pady=20)
        # Обновляем область прокрутки после создания контейнера
        self.root.update_idletasks()
        row += 1

        # Заголовок секции
        section_title2 = tk.Label(advanced_frame, text="Дополнительные настройки",
                                 bg=self.frame_bg, fg=self.text_color,
                                 font=(self.font_family, 15, "bold"))
        section_title2.grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 12))

        # Качество изображений
        ttk.Label(advanced_frame, text="Качество:", style="Mac.TLabel").grid(row=1, column=0, sticky=tk.W, pady=(0, 5))
        quality_frame = tk.Frame(advanced_frame, bg=self.frame_bg)
        quality_frame.grid(row=2, column=0, sticky=tk.W, pady=(0, 6))

        ttk.Radiobutton(quality_frame, text="Полный размер", variable=self.image_quality,
                       value="full", style="Mac.TRadiobutton").grid(row=0, column=0, padx=(0, 15))
        ttk.Radiobutton(quality_frame, text="Средний", variable=self.image_quality,
                       value="medium", style="Mac.TRadiobutton").grid(row=0, column=1, padx=(0, 15))
        ttk.Radiobutton(quality_frame, text="Маленький", variable=self.image_quality,
                       value="small", style="Mac.TRadiobutton").grid(row=0, column=2)

        # Фильтр по размеру файла
        ttk.Label(advanced_frame, text="Размер файла (МБ):", style="Mac.TLabel").grid(row=3, column=0, sticky=tk.W, pady=(0, 5))
        size_frame = tk.Frame(advanced_frame, bg=self.frame_bg)
        size_frame.grid(row=4, column=0, sticky=tk.W, pady=(0, 6))

        ttk.Label(size_frame, text="От", style="Mac.TLabel").grid(row=0, column=0, padx=(0, 8))
        ttk.Spinbox(size_frame, from_=0.0, to=1000.0, increment=0.1,
                   textvariable=self.min_size_mb, width=10, style="Mac.TSpinbox").grid(row=0, column=1, padx=(0, 8))
        ttk.Label(size_frame, text="до", style="Mac.TLabel").grid(row=0, column=2, padx=(0, 8))
        ttk.Spinbox(size_frame, from_=0.0, to=1000.0, increment=0.1,
                   textvariable=self.max_size_mb, width=10, style="Mac.TSpinbox").grid(row=0, column=3, padx=(0, 8))
        ttk.Label(size_frame, text="МБ", style="Mac.TLabel").grid(row=0, column=4)

        # Upscale настройки - перемещаем сразу после качества для лучшей видимости
        ttk.Label(advanced_frame, text="Upscale:", style="Mac.TLabel",
                 font=(self.font_family, 12, "bold")).grid(row=5, column=0, sticky=tk.W, pady=(12, 5))
        upscale_frame = tk.Frame(advanced_frame, bg=self.frame_bg)
        upscale_frame.grid(row=6, column=0, sticky=(tk.W, tk.E, tk.N), pady=(0, 8))  # Уменьшен отступ снизу
        upscale_frame.columnconfigure(0, weight=1)  # Настраиваем колонку для растягивания
        upscale_frame.columnconfigure(1, weight=1, minsize=300)  # Увеличена минимальная ширина

        ttk.Checkbutton(upscale_frame, text="Включить upscale после скачивания",
                       variable=self.enable_upscale, style="Mac.TCheckbutton").grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 6))  # Уменьшен отступ

        # Масштаб - размещаем в одну строку с достаточным пространством
        ttk.Label(upscale_frame, text="Масштаб:", style="Mac.TLabel").grid(row=1, column=0, sticky=tk.W, pady=(0, 3))  # Уменьшен отступ
        upscale_scale_frame = tk.Frame(upscale_frame, bg=self.frame_bg)
        upscale_scale_frame.grid(row=2, column=0, sticky=tk.W, pady=(0, 5))  # Уменьшен отступ
        ttk.Radiobutton(upscale_scale_frame, text="x2", variable=self.upscale_scale,
                       value=2, style="Mac.TRadiobutton").grid(row=0, column=0, padx=(0, 15))
        ttk.Radiobutton(upscale_scale_frame, text="x3", variable=self.upscale_scale,
                       value=3, style="Mac.TRadiobutton").grid(row=0, column=1, padx=(0, 15))
        ttk.Radiobutton(upscale_scale_frame, text="x4", variable=self.upscale_scale,
                       value=4, style="Mac.TRadiobutton").grid(row=0, column=2)

        # Тип модели - размещаем в одну строку с достаточным пространством
        ttk.Label(upscale_frame, text="Тип модели:", style="Mac.TLabel").grid(row=3, column=0, sticky=tk.W, pady=(0, 3))  # Уменьшен отступ
        upscale_model_frame = tk.Frame(upscale_frame, bg=self.frame_bg)
        upscale_model_frame.grid(row=4, column=0, sticky=tk.W, pady=(0, 5))  # Уменьшен отступ
        ttk.Radiobutton(upscale_model_frame, text="Авто", variable=self.upscale_model,
                       value="auto", style="Mac.TRadiobutton").grid(row=0, column=0, padx=(0, 15))
        ttk.Radiobutton(upscale_model_frame, text="Фото", variable=self.upscale_model,
                       value="photo", style="Mac.TRadiobutton").grid(row=0, column=1, padx=(0, 15))
        ttk.Radiobutton(upscale_model_frame, text="Аниме", variable=self.upscale_model,
                       value="anime", style="Mac.TRadiobutton").grid(row=0, column=2)

        ttk.Label(upscale_frame, text="Размер тайла:", style="Mac.TLabel").grid(row=5, column=0, sticky=tk.W, pady=(0, 3))  # Уменьшен отступ
        ttk.Spinbox(upscale_frame, from_=50, to=500, increment=50,
                   textvariable=self.upscale_tile, width=10, style="Mac.TSpinbox").grid(row=6, column=0, sticky=tk.W, pady=(0, 5))  # Уменьшен отступ

        ttk.Label(upscale_frame, text="GPU:", style="Mac.TLabel").grid(row=7, column=0, sticky=tk.W, pady=(0, 3))  # Уменьшен отступ
        ttk.Spinbox(upscale_frame, from_=0, to=10, increment=1,
                   textvariable=self.upscale_gpu, width=10, style="Mac.TSpinbox").grid(row=8, column=0, sticky=tk.W, pady=(0, 0))  # Уменьшен отступ

        # Автоматическое переименование (после upscale_frame, который находится в row=6)
        ttk.Checkbutton(advanced_frame, text="Автоматическое переименование файлов",
                       variable=self.auto_rename, style="Mac.TCheckbutton").grid(row=7, column=0, sticky=tk.W, pady=(12, 8))

        # Автоматическое создание подпапок по названию доски
        ttk.Checkbutton(advanced_frame, text="Создавать подпапку по названию доски",
                       variable=self.auto_subfolder, style="Mac.TCheckbutton").grid(row=8, column=0, sticky=tk.W, pady=(0, 8))

        # Продолжение прерванного скачивания
        ttk.Checkbutton(advanced_frame, text="Продолжать прерванное скачивание (resume)",
                       variable=self.resume_download, style="Mac.TCheckbutton").grid(row=9, column=0, sticky=tk.W, pady=(0, 8))

        # Уведомления Windows
        ttk.Checkbutton(advanced_frame, text="Уведомления Windows о завершении",
                       variable=self.windows_notifications, style="Mac.TCheckbutton").grid(row=10, column=0, sticky=tk.W, pady=(0, 8))

        # Экспорт метаданных
        ttk.Checkbutton(advanced_frame, text="Экспорт метаданных в JSON",
                       variable=self.export_metadata, style="Mac.TCheckbutton").grid(row=11, column=0, sticky=tk.W, pady=(0, 8))

        # Шаблон имени файла
        ttk.Label(advanced_frame, text="Шаблон имени файла:", style="Mac.TLabel").grid(row=12, column=0, sticky=tk.W, pady=(12, 5))
        template_frame = tk.Frame(advanced_frame, bg=self.frame_bg)
        template_frame.grid(row=13, column=0, sticky=(tk.W, tk.E), pady=(0, 6))
        template_frame.columnconfigure(0, weight=1)

        template_entry = ttk.Entry(template_frame, textvariable=self.filename_template, width=20, style="Mac.TEntry")
        template_entry.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=(0, 0))

        template_help = tk.Label(template_frame,
                                 text="Доступно: {index}, {index04}, {date}, {time}, {datetime}, {board}, {hash}",
                                 bg=self.frame_bg, fg="#6E6E73",
                                 font=(self.font_family, 9))
        template_help.grid(row=1, column=0, sticky=tk.W, pady=(5, 0))

        # Задержки
        ttk.Label(advanced_frame, text="Задержка прокрутки (сек):", style="Mac.TLabel").grid(row=14, column=0, sticky=tk.W, pady=(0, 5))
        ttk.Spinbox(advanced_frame, from_=0.5, to=10.0, increment=0.5,
                   textvariable=self.scroll_delay, width=12, style="Mac.TSpinbox").grid(row=15, column=0, sticky=tk.W, pady=(0, 6))

        ttk.Label(advanced_frame, text="Задержка скачивания (сек):", style="Mac.TLabel").grid(row=16, column=0, sticky=tk.W, pady=(0, 5))
        ttk.Spinbox(advanced_frame, from_=0.1, to=5.0, increment=0.1,
                   textvariable=self.download_delay, width=12, style="Mac.TSpinbox").grid(row=17, column=0, sticky=tk.W, pady=(0, 6))

        # Обновляем размер контейнера после создания всех элементов
        def update_advanced_container_size():
            advanced_frame.update_idletasks()
            content_height = advanced_frame.winfo_reqheight()
            # Принудительно обновляем размер canvas через событие Configure
            if hasattr(self, 'advanced_container'):
                canvas = self.advanced_container.winfo_children()[0]  # Получаем canvas из контейнера
                if canvas:
                    # Обновляем размер canvas
                    canvas.update_idletasks()
                    canvas.event_generate('<Configure>')
                    # Также обновляем размер контейнера
                    self.advanced_container.update_idletasks()
        
        # Запускаем обновление после создания всех виджетов
        self.root.after(200, update_advanced_container_size)
        self.root.after(500, update_advanced_container_size)  # Дополнительное обновление для надежности

        # === СЕКЦИЯ 3: Управление ===
        control_frame = tk.Frame(main_frame, bg=self.bg_color)
        control_frame.grid(row=row, column=0, pady=(20, 15))
        row += 1

        self.start_btn = ttk.Button(control_frame, text="Запустить",
                                   command=self.start_download, style="MacSecondary.TButton",
                                   width=15)
        self.start_btn.grid(row=0, column=0, padx=(0, 10))

        self.pause_btn = ttk.Button(control_frame, text="Пауза",
                                    command=self.pause_download, style="MacSecondary.TButton",
                                    state=tk.DISABLED, width=15)
        self.pause_btn.grid(row=0, column=1, padx=(0, 10))

        self.stop_btn = ttk.Button(control_frame, text="Остановить",
                                  command=self.stop_download, style="MacSecondary.TButton",
                                  state=tk.DISABLED, width=15)
        self.stop_btn.grid(row=0, column=2)

        # === СЕКЦИЯ 4: Прогресс ===
        # Создаем скругленный фрейм
        progress_container, progress_frame = self.create_rounded_frame(main_frame, self.frame_bg, radius=12)
        progress_container.grid(row=row, column=0, sticky=(tk.W, tk.E), pady=(0, 15))
        progress_frame.columnconfigure(0, weight=1)
        # Добавляем отступы для содержимого
        progress_frame.configure(padx=20, pady=20)
        # Обновляем область прокрутки после создания контейнера
        self.root.update_idletasks()
        row += 1

        # Заголовок секции
        progress_title = tk.Label(progress_frame, text="Прогресс",
                                 bg=self.frame_bg, fg=self.text_color,
                                 font=(self.font_family, 15, "bold"))
        progress_title.grid(row=0, column=0, sticky=tk.W, pady=(0, 15))

        self.progress_var = tk.StringVar(value="Готов к работе")
        ttk.Label(progress_frame, textvariable=self.progress_var, style="Mac.TLabel").grid(row=1, column=0, sticky=tk.W, pady=(0, 5))

        # Таймер для отображения времени (под статусом, над прогресс-баром)
        self.time_var = tk.StringVar(value="")
        time_label = ttk.Label(progress_frame, textvariable=self.time_var, style="MacSubtitle.TLabel", foreground="#6B6B6F")
        time_label.grid(row=2, column=0, sticky=tk.W, pady=(0, 5))

        # Используем стандартный стиль Progressbar с настройками цвета
        self.progress_bar = ttk.Progressbar(progress_frame, mode='determinate', length=300)
        # Настраиваем цвета напрямую через стиль
        self.style.configure("TProgressbar",
                            background=self.accent_color,
                            troughcolor="#E5E5EA",
                            borderwidth=0,
                            thickness=8)
        self.progress_bar.grid(row=3, column=0, sticky=(tk.W, tk.E), pady=(0, 10))

        # Прогресс-бар для upscale
        self.upscale_progress_var = tk.StringVar(value="")
        self.upscale_progress_label = ttk.Label(progress_frame, textvariable=self.upscale_progress_var,
                                                style="MacSubtitle.TLabel")
        self.upscale_progress_label.grid(row=4, column=0, sticky=tk.W, pady=(0, 5))

        # Таймер для upscale
        self.upscale_time_var = tk.StringVar(value="")
        upscale_time_label = ttk.Label(progress_frame, textvariable=self.upscale_time_var, style="MacSubtitle.TLabel", foreground="#6B6B6F")
        upscale_time_label.grid(row=5, column=0, sticky=tk.W, pady=(0, 5))

        self.upscale_progress_bar = ttk.Progressbar(progress_frame, mode='determinate', length=300)
        self.upscale_progress_bar.grid(row=6, column=0, sticky=(tk.W, tk.E), pady=(0, 10))

        stats_frame = tk.Frame(progress_frame, bg=self.frame_bg)
        stats_frame.grid(row=7, column=0, sticky=tk.W)

        self.stats_label = ttk.Label(stats_frame, text="Найдено: 0 | Скачано: 0 | Ошибок: 0", style="Mac.TLabel")
        self.stats_label.grid(row=0, column=0)

        # === СЕКЦИЯ 5: Лог ===
        # Создаем скругленный фрейм
        log_container, log_frame = self.create_rounded_frame(main_frame, self.frame_bg, radius=12)
        log_container.grid(row=row, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        # Добавляем отступы для содержимого
        log_frame.configure(padx=20, pady=20)
        main_frame.rowconfigure(row, weight=1)

        # Заголовок секции
        log_title = tk.Label(log_frame, text="Лог действий",
                            bg=self.frame_bg, fg=self.text_color,
                            font=(self.font_family, 15, "bold"))
        log_title.grid(row=0, column=0, sticky=tk.W, pady=(0, 15))

        # Стилизация текстового поля лога (уменьшено для компактности)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=70, wrap=tk.WORD,
                                                  bg="#FFFFFF", fg=self.text_color,
                                                  font=(self.font_mono, 10),
                                                  relief="flat", borderwidth=1,
                                                  padx=12, pady=12)
        self.log_text.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 15))

        log_buttons_frame = tk.Frame(log_frame, bg=self.frame_bg)
        log_buttons_frame.grid(row=2, column=0, pady=(0, 0))

        ttk.Button(log_buttons_frame, text="Очистить лог",
                  command=self.clear_log, style="MacSecondary.TButton").grid(row=0, column=0, padx=(0, 8))
        ttk.Button(log_buttons_frame, text="Экспорт URL",
                  command=self.export_urls, style="MacSecondary.TButton").grid(row=0, column=1, padx=(0, 8))
        ttk.Button(log_buttons_frame, text="Предпросмотр",
                  command=self.show_preview, style="MacSecondary.TButton").grid(row=0, column=2, padx=(0, 8))
        ttk.Button(log_buttons_frame, text="Открыть папку",
                  command=self.open_folder, style="MacSecondary.TButton").grid(row=0, column=3)

        # Инициализация статистики
        self.stats = {"found": 0, "downloaded": 0, "failed": 0, "skipped": 0}
        self.image_urls_list = []

        # Обновляем область прокрутки после создания всех виджетов
        self.root.update_idletasks()
        self.main_canvas.update_idletasks()
        self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))

    def log(self, message):
        """Добавление сообщения в лог"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        # Обновляем область прокрутки основного окна
        if hasattr(self, 'main_canvas'):
            self.main_canvas.update_idletasks()
            self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))
        self.root.update_idletasks()

    def clear_log(self):
        """Очистка лога"""
        self.log_text.delete(1.0, tk.END)

    def select_folder(self):
        """Выбор папки для сохранения"""
        folder = filedialog.askdirectory(initialdir=self.download_folder.get())
        if folder:
            self.download_folder.set(folder)

    def load_history(self):
        """Загрузка истории скачиваний"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                    # Декодируем board_name для старых записей с URL-encoded названиями
                    for item in history:
                        board_name = item.get("board_name")
                        if board_name and '%' in board_name:
                            try:
                                item["board_name"] = unquote(board_name)
                            except:
                                pass  # Если не удалось декодировать, оставляем как есть
                    return history
            except:
                return []
        return []

    def save_history(self):
        """Сохранение истории скачиваний"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"Ошибка сохранения истории: {e}")

    def load_timing_stats(self):
        """Загрузка статистики времени"""
        if os.path.exists(self.timing_stats_file):
            try:
                with open(self.timing_stats_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {
                    "download_times": [],  # Список времени на скачивание N изображений
                    "upscale_times": []    # Список времени на upscale N изображений
                }
        return {
            "download_times": [],
            "upscale_times": []
        }

    def save_timing_stats(self):
        """Сохранение статистики времени"""
        try:
            with open(self.timing_stats_file, 'w', encoding='utf-8') as f:
                json.dump(self.timing_stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения статистики времени: {e}")

    def add_download_timing(self, image_count, elapsed_time):
        """Добавить запись о времени скачивания"""
        self.timing_stats["download_times"].append({
            "count": image_count,
            "time": elapsed_time,
            "timestamp": datetime.now().isoformat()
        })
        # Оставляем только последние 50 записей
        if len(self.timing_stats["download_times"]) > 50:
            self.timing_stats["download_times"] = self.timing_stats["download_times"][-50:]
        self.save_timing_stats()

    def add_upscale_timing(self, image_count, elapsed_time):
        """Добавить запись о времени upscale"""
        self.timing_stats["upscale_times"].append({
            "count": image_count,
            "time": elapsed_time,
            "timestamp": datetime.now().isoformat()
        })
        # Оставляем только последние 50 записей
        if len(self.timing_stats["upscale_times"]) > 50:
            self.timing_stats["upscale_times"] = self.timing_stats["upscale_times"][-50:]
        self.save_timing_stats()

    def estimate_download_time(self, image_count):
        """Оценить время скачивания на основе статистики"""
        if not self.timing_stats["download_times"]:
            return None
        
        # Берем последние 10 записей для более точной оценки
        recent_times = self.timing_stats["download_times"][-10:]
        
        # Вычисляем среднее время на одно изображение
        total_time = 0
        total_count = 0
        for record in recent_times:
            if record["count"] > 0:
                time_per_image = record["time"] / record["count"]
                total_time += time_per_image
                total_count += 1
        
        if total_count == 0:
            return None
        
        avg_time_per_image = total_time / total_count
        estimated_time = avg_time_per_image * image_count
        return estimated_time

    def estimate_upscale_time(self, image_count):
        """Оценить время upscale на основе статистики"""
        if not self.timing_stats["upscale_times"]:
            return None
        
        # Берем последние 10 записей для более точной оценки
        recent_times = self.timing_stats["upscale_times"][-10:]
        
        # Вычисляем среднее время на одно изображение
        total_time = 0
        total_count = 0
        for record in recent_times:
            if record["count"] > 0:
                time_per_image = record["time"] / record["count"]
                total_time += time_per_image
                total_count += 1
        
        if total_count == 0:
            return None
        
        avg_time_per_image = total_time / total_count
        estimated_time = avg_time_per_image * image_count
        return estimated_time

    def format_time(self, seconds):
        """Форматировать время в читаемый вид"""
        if seconds is None:
            return "---"
        if seconds < 60:
            return f"{int(seconds)} сек"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{minutes} мин {secs} сек"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours} ч {minutes} мин"

    def format_remaining_time(self, elapsed, estimated):
        """Форматировать оставшееся время"""
        if estimated is None:
            return "---"
        remaining = max(0, estimated - elapsed)
        return self.format_time(remaining)

    def add_url_to_list(self):
        """Добавить URL в список и получить название доски"""
        url = self.url_entry.get().strip()
        if not url:
            return
        if 'pinterest.com' not in url.lower() and 'pin.it' not in url.lower():
            messagebox.showerror("Ошибка", "Введите корректный URL Pinterest")
            return

        # Проверяем, не добавлен ли уже этот URL
        if any(item.get("url") == url for item in self.url_list):
            messagebox.showinfo("Информация", "Этот URL уже добавлен в список")
            return

        # Добавляем URL в список с временным названием и количеством изображений по умолчанию
        default_max_images = self.max_images.get()
        url_item = {"url": url, "board_name": None, "max_images": default_max_images}
        self.url_list.append(url_item)

        # Добавляем в Treeview
        item_id = self.url_treeview.insert("", tk.END, values=("(получение названия...)", url,
                                                                 default_max_images if default_max_images > 0 else "Все"))
        url_item["tree_item_id"] = item_id

        self.url_entry.delete(0, tk.END)

        # Асинхронно получаем название доски
        threading.Thread(target=self.get_board_name_async, args=(url, len(self.url_list) - 1), daemon=True).start()

    def get_board_name_async(self, url, index):
        """Асинхронное получение названия доски для URL"""
        temp_parser = None
        try:
            # Создаем временный парсер для получения названия доски
            temp_parser = PinterestParser(download_folder=self.download_folder.get())

            # Расширяем короткий URL
            expanded_url = url
            try:
                expanded_url = temp_parser.expand_short_url(url)
                if expanded_url != url:
                    self.safe_update_ui(lambda: self.log(f"✓ Расширен короткий URL: {expanded_url}") or 0)
            except Exception as e:
                import traceback
                error_details = traceback.format_exc()
                self.safe_update_ui(lambda e=e, d=error_details:
                                  self.log(f"⚠️ Ошибка расширения короткого URL для {url}: {e}\nДетали: {d}") or 0)

            # Получаем название доски (метод не требует браузера, просто парсит URL)
            board_name = None
            try:
                board_name = temp_parser.get_board_name_from_url(expanded_url)
                if board_name:
                    self.safe_update_ui(lambda b=board_name, u=url:
                                      self.log(f"✓ Название доски для {u}: {b}") or 0)
                else:
                    self.safe_update_ui(lambda u=expanded_url:
                                      self.log(f"⚠️ Не удалось извлечь название доски из URL: {u}") or 0)
            except Exception as e:
                import traceback
                error_details = traceback.format_exc()
                self.safe_update_ui(lambda e=e, u=url, d=error_details:
                                  self.log(f"❌ Ошибка получения названия доски для {u}: {e}\nДетали: {d}") or 0)

            # Обновляем элемент в списке
            if index < len(self.url_list) and self.url_list[index]["url"] == url:
                self.url_list[index]["board_name"] = board_name

                # Обновляем отображение в Treeview
                max_images = self.url_list[index].get("max_images", 0)
                max_images_display = max_images if max_images > 0 else "Все"
                board_display = board_name if board_name else "(название не найдено)"

                # Обновляем элемент в Treeview
                if "tree_item_id" in self.url_list[index]:
                    item_id = self.url_list[index]["tree_item_id"]
                    self.safe_update_ui(lambda iid=item_id, b=board_display, u=url, m=max_images_display:
                                      self.url_treeview.item(iid, values=(b, u, m)) or 0)

                # Создаем папку с названием доски (только если включена опция автоподпапок)
                if board_name and self.auto_subfolder.get():
                    # Декодируем board_name, если он в URL-encoded формате (для русских названий)
                    decoded_board_name = board_name
                    if '%' in board_name:
                        try:
                            decoded_board_name = unquote(board_name)
                        except:
                            decoded_board_name = board_name
                    board_folder = os.path.join(self.download_folder.get(), decoded_board_name)
                    try:
                        os.makedirs(board_folder, exist_ok=True)
                        self.safe_update_ui(lambda: self.log(f"✓ Создана папка: {board_folder}") or 0)
                    except Exception as e:
                        self.safe_update_ui(lambda e=e: self.log(f"❌ Ошибка создания папки {board_folder}: {e}") or 0)

        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            self.safe_update_ui(lambda e=e, d=error_details:
                              self.log(f"❌ Критическая ошибка при получении названия доски: {e}\nДетали: {d}") or 0)
            # Обновляем отображение даже при ошибке
            if index < len(self.url_list) and self.url_list[index]["url"] == url:
                max_images = self.url_list[index].get("max_images", 0)
                max_images_display = max_images if max_images > 0 else "Все"
                if "tree_item_id" in self.url_list[index]:
                    item_id = self.url_list[index]["tree_item_id"]
                    self.safe_update_ui(lambda iid=item_id, u=url, m=max_images_display:
                                      self.url_treeview.item(iid, values=("(ошибка получения названия)", u, m)) or 0)
        finally:
            # Закрываем парсер если он был создан (expand_short_url не создает браузер, но на всякий случай)
            if temp_parser:
                try:
                    temp_parser.close()
                except:
                    pass

    def edit_url_max_images(self, event):
        """Редактирование количества изображений для выбранного URL"""
        selection = self.url_treeview.selection()
        if not selection:
            return

        item_id = selection[0]
        item_values = self.url_treeview.item(item_id, "values")
        if not item_values or len(item_values) < 3:
            return

        url = item_values[1]
        current_max = item_values[2]

        # Находим индекс в списке
        index = None
        for idx, item in enumerate(self.url_list):
            if item["url"] == url:
                index = idx
                break

        if index is None:
            return

        # Создаем диалог для ввода количества
        dialog = tk.Toplevel(self.root)
        dialog.title("Количество изображений")
        dialog.geometry("300x150")
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text=f"URL: {url[:50]}...", wraplength=280).pack(pady=10)
        tk.Label(dialog, text="Количество изображений (0 = все):").pack()

        max_images_var = tk.IntVar(value=self.url_list[index].get("max_images", 0))
        spinbox = ttk.Spinbox(dialog, from_=0, to=10000, textvariable=max_images_var, width=15)
        spinbox.pack(pady=5)

        def save_and_close():
            max_images_val = max_images_var.get()
            self.url_list[index]["max_images"] = max_images_val
            max_images_display = max_images_val if max_images_val > 0 else "Все"
            board_name = self.url_list[index].get("board_name") or "(название не найдено)"
            self.url_treeview.item(item_id, values=(board_name, url, max_images_display))
            dialog.destroy()

        ttk.Button(dialog, text="Сохранить", command=save_and_close).pack(pady=10)
        dialog.bind("<Return>", lambda e: save_and_close())
        spinbox.focus_set()

    def remove_selected_url(self):
        """Удалить выбранный URL из списка"""
        selection = self.url_treeview.selection()
        if not selection:
            return

        # Удаляем в обратном порядке чтобы индексы не сдвигались
        for item_id in reversed(selection):
            item_values = self.url_treeview.item(item_id, "values")
            if item_values and len(item_values) >= 2:
                url = item_values[1]
                # Находим и удаляем из списка
                for idx, item in enumerate(self.url_list):
                    if item["url"] == url:
                        self.url_list.pop(idx)
                        break
            self.url_treeview.delete(item_id)

    def clear_url_list(self):
        """Очистить список URL"""
        for item_id in self.url_treeview.get_children():
            self.url_treeview.delete(item_id)
        self.url_list.clear()

    def refresh_board_names(self):
        """Обновить названия досок для всех URL в списке"""
        if not self.url_list:
            messagebox.showinfo("Обновление", "Список URL пуст")
            return
        
        self.log("🔄 Обновляю названия досок для всех URL...")
        
        # Обновляем названия для всех URL в списке
        for index, url_item in enumerate(self.url_list):
            url = url_item["url"]
            # Запускаем обновление в отдельном потоке
            threading.Thread(target=self.get_board_name_async, args=(url, index), daemon=True).start()
        
        self.log(f"✓ Запущено обновление названий для {len(self.url_list)} URL")

    def show_history(self):
        """Показать историю скачиваний с множественным выбором"""
        if not self.history:
            messagebox.showinfo("История", "История пуста")
            return

        history_window = tk.Toplevel(self.root)
        history_window.title("История скачиваний")
        history_window.geometry("900x500")

        # Treeview для истории с колонками
        columns = ("board", "url", "count", "date")
        history_tree = ttk.Treeview(history_window, columns=columns, show="headings", selectmode="extended")
        history_tree.heading("board", text="Название доски")
        history_tree.heading("url", text="URL")
        history_tree.heading("count", text="Скачано")
        history_tree.heading("date", text="Дата")

        # Настройка ширины колонок
        history_tree.column("board", width=200, anchor="w")
        history_tree.column("url", width=350, anchor="w")
        history_tree.column("count", width=100, anchor="center")
        history_tree.column("date", width=150, anchor="w")

        scrollbar_history = ttk.Scrollbar(history_window, orient="vertical", command=history_tree.yview)
        history_tree.configure(yscrollcommand=scrollbar_history.set)

        history_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=10)
        scrollbar_history.pack(side=tk.RIGHT, fill=tk.Y, pady=10)

        # Заполняем историю (последние 100 записей)
        # Сохраняем ссылки на элементы истории для обновления
        history_items_map = {}

        for item in reversed(self.history[-100:]):
            board_name = item.get("board_name")
            board_display = board_name if board_name else "(не указано)"
            url = item.get("url", "")
            count = item.get("count", 0)
            date = item.get("date", "")
            item_id = history_tree.insert("", tk.END, values=(board_display, url, count, date))
            history_items_map[item_id] = item

        # Асинхронно обновляем названия досок для записей без них
        def update_history_board_names():
            for item_id, item in history_items_map.items():
                if not item.get("board_name"):
                    try:
                        temp_parser = PinterestParser(download_folder=self.download_folder.get())
                        url_for_parsing = item.get("url", "")
                        expanded_url = temp_parser.expand_short_url(url_for_parsing)
                        board_name = temp_parser.get_board_name_from_url(expanded_url)
                        if board_name:
                            # Декодируем board_name перед сохранением, если он в URL-encoded формате
                            decoded_board_name = board_name
                            if '%' in board_name:
                                try:
                                    decoded_board_name = unquote(board_name)
                                except:
                                    decoded_board_name = board_name
                            # Обновляем историю с декодированным названием
                            item["board_name"] = decoded_board_name
                            board_name = decoded_board_name  # Используем декодированное название для отображения
                            # Обновляем отображение
                            item_values = list(history_tree.item(item_id, "values"))
                            item_values[0] = board_name
                            history_tree.item(item_id, values=item_values)
                            temp_parser.close()
                            # Сохраняем обновленную историю
                            self.save_history()
                    except:
                        pass

        # Запускаем обновление в фоне
        threading.Thread(target=update_history_board_names, daemon=True).start()

        def add_selected_to_list():
            """Добавить выбранные URL в список"""
            selection = history_tree.selection()
            if not selection:
                messagebox.showwarning("Внимание", "Выберите хотя бы одну запись из истории")
                return

            added_count = 0
            skipped_count = 0

            for item_id in selection:
                item_values = history_tree.item(item_id, "values")
                if len(item_values) >= 2:
                    url = item_values[1]
                    board_name = item_values[0] if item_values[0] != "(не указано)" else None
                    
                    # Декодируем board_name, если он в URL-encoded формате (для русских названий)
                    if board_name:
                        # Проверяем, является ли название URL-encoded строкой
                        if '%' in board_name:
                            try:
                                board_name = unquote(board_name)
                            except:
                                pass  # Если не удалось декодировать, оставляем как есть

                    # Проверяем, не добавлен ли уже этот URL
                    if any(item.get("url") == url for item in self.url_list):
                        skipped_count += 1
                        continue

                    # Получаем max_images из истории или используем значение по умолчанию
                    max_images = self.max_images.get()

                    # Добавляем URL в список
                    url_item = {"url": url, "board_name": board_name, "max_images": max_images}
                    self.url_list.append(url_item)

                    # Добавляем в Treeview
                    max_images_display = max_images if max_images > 0 else "Все"
                    board_display = board_name if board_name else "(получение названия...)"
                    item_id_new = self.url_treeview.insert("", tk.END, values=(board_display, url, max_images_display))
                    url_item["tree_item_id"] = item_id_new

                    # Если название доски не было найдено, пытаемся получить его асинхронно
                    if not board_name:
                        threading.Thread(target=self.get_board_name_async,
                                       args=(url, len(self.url_list) - 1), daemon=True).start()

                    added_count += 1

            if added_count > 0:
                messagebox.showinfo("Успех", f"Добавлено {added_count} URL в список" +
                                  (f"\nПропущено (уже в списке): {skipped_count}" if skipped_count > 0 else ""))
            else:
                messagebox.showinfo("Информация", "Все выбранные URL уже добавлены в список")

                history_window.destroy()

        buttons_frame = ttk.Frame(history_window)
        buttons_frame.pack(pady=10)

        ttk.Button(buttons_frame, text="Добавить выбранные в список", command=add_selected_to_list).pack(side=tk.LEFT, padx=5)
        ttk.Button(buttons_frame, text="Закрыть", command=history_window.destroy).pack(side=tk.LEFT, padx=5)

    def start_download(self):
        """Запуск скачивания"""
        # Собираем список URL для обработки
        urls_to_process = []

        # Собираем URL с их настройками (max_images)
        urls_with_settings = []

        # Добавляем URL из поля ввода, если он есть
        url_from_entry = self.url_entry.get().strip()
        if url_from_entry and ('pinterest.com' in url_from_entry.lower() or 'pin.it' in url_from_entry.lower()):
            urls_with_settings.append({
                "url": url_from_entry,
                "board_name": None,
                "max_images": self.max_images.get()
            })

        # Добавляем URL из списка с их настройками
        urls_with_settings.extend(self.url_list)

        # Извлекаем только URL для передачи в worker
        urls_to_process = [item["url"] for item in urls_with_settings]

        if not urls_to_process:
            messagebox.showerror("Ошибка", "Добавьте хотя бы один URL Pinterest\nПоддерживаются:\n- https://www.pinterest.com/...\n- https://pin.it/...")
            return

        if self.is_downloading:
            messagebox.showwarning("Внимание", "Скачивание уже выполняется")
            return

        # Обновление состояния кнопок
        self.start_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL)

        self.is_downloading = True
        self.is_paused = False
        self.stats = {"found": 0, "downloaded": 0, "failed": 0, "skipped": 0}
        self.image_urls_list = []
        self.current_url_index = 0
        self.total_images_to_download = 0
        self.current_downloaded_count = 0

        # Сброс прогресс-баров
        self.progress_bar.config(value=0, maximum=100)
        self.upscale_progress_bar.config(value=0, maximum=100)
        self.upscale_progress_var.set("")

        self.log(f"Начинаю скачивание {len(urls_to_process)} URL")
        self.log(f"Максимум изображений: {self.max_images.get() if self.max_images.get() > 0 else 'Все'}")
        self.log(f"Папка: {self.download_folder.get()}")

        # Запуск в отдельном потоке
        self.download_thread = threading.Thread(target=self.download_multiple_worker, args=(urls_to_process,), daemon=True)
        self.download_thread.start()

    def pause_download(self):
        """Пауза/возобновление скачивания"""
        if self.is_paused:
            self.is_paused = False
            self.pause_btn.config(text="Пауза")
            self.log("Скачивание возобновлено")
        else:
            self.is_paused = True
            self.pause_btn.config(text="Возобновить")
            self.log("Скачивание приостановлено")

    def stop_download(self):
        """Остановка скачивания"""
        self.is_downloading = False
        self.is_paused = False
        self.log("Остановка скачивания...")
        self.update_ui_after_stop()

    def find_upscale_exe(self):
        """Поиск realesrgan-ncnn-vulkan.exe"""
        base_dir = Path(__file__).resolve().parent
        tools_dir = base_dir / "upscale" / "tools"

        # Проверяем возможные пути
        for cand in [tools_dir / "realesrgan-ncnn-vulkan.exe",
                     base_dir / "upscale" / "realesrgan-ncnn-vulkan.exe",
                     base_dir / "realesrgan-ncnn-vulkan.exe"]:
            if cand.exists():
                return cand
        return None

    def find_models_dir(self, exe_path):
        """Поиск папки с моделями"""
        if exe_path:
            near = exe_path.parent / "models"
            if near.exists() and any(near.glob("*.param")) and any(near.glob("*.bin")):
                return near

        base_dir = Path(__file__).resolve().parent
        tools_dir = base_dir / "upscale" / "tools"
        models = tools_dir / "models"
        if models.exists() and any(models.glob("*.param")) and any(models.glob("*.bin")):
            return models
        return None

    def list_available_model_names(self, models_dir):
        """Список доступных моделей"""
        names = set()
        for p in models_dir.glob("*.param"):
            if (models_dir / f"{p.stem}.bin").exists():
                names.add(p.stem)
        return sorted(names, key=str.lower)

    def parse_scale_from_name(self, name):
        """Извлечение масштаба из имени модели"""
        s = name.lower()
        m = re.search(r'[_\-]x([234])(?:[_\-]|$)', s)
        if m:
            return int(m.group(1))
        m2 = re.match(r'([234])x', s)
        if m2:
            return int(m2.group(1))
        return None

    def pick_best_model_for_scale(self, available, mode, want_scale):
        """Выбор лучшей модели для масштаба"""
        def has_exact(scale, pool):
            for n in pool:
                if self.parse_scale_from_name(n) == scale:
                    return n
            return None

        if mode == "anime":
            anime_pool = [n for n in available if "anime" in n.lower()]
            chosen = has_exact(want_scale, anime_pool)
            if chosen:
                return chosen
            for pref in ["realesr-animevideov3-x4", "realesr-animevideov3-x3", "realesr-animevideov3-x2"]:
                for n in anime_pool:
                    if pref in n.lower():
                        return n

        general_pool = [n for n in available if "general" in n.lower()]
        chosen = has_exact(want_scale, general_pool)
        if chosen:
            return chosen
        for pref in ["realesrgan_general_wdn_x4_v3", "realesrgan_general_x4_v3"]:
            for n in general_pool:
                if pref == n.lower():
                    return n

        other_pool = [n for n in available if n not in general_pool]
        chosen = has_exact(want_scale, other_pool)
        if chosen:
            return chosen

        return available[0] if available else None

    def run_upscale(self, input_folder):
        """Запуск upscale для папки с отображением прогресса"""
        try:
            input_path = Path(input_folder)

            # Подсчитываем количество изображений для прогресс-бара
            image_files = [f for f in input_path.iterdir()
                          if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
            total_images = len(image_files)

            if total_images == 0:
                self.safe_update_ui(lambda: self.log(f"⚠️ Нет изображений для upscale в папке: {input_folder}") or 0)
                return False

            print(f"[UPSCALE] ===== Начало upscale =====")
            print(f"[UPSCALE] Папка: {input_folder}")
            print(f"[UPSCALE] Изображений: {total_images}")
            self.safe_update_ui(lambda: self.log(f"🔄 Начинаю upscale для папки: {input_folder} ({total_images} изображений)") or 0)
            self.safe_update_ui(lambda: self.upscale_progress_var.set(f"Upscale: 0/{total_images}") or 0)
            self.safe_update_ui(lambda: self.upscale_progress_bar.config(maximum=total_images, value=0) or 0)

            exe = self.find_upscale_exe()
            if not exe:
                print("[UPSCALE] ❌ Не найден realesrgan-ncnn-vulkan.exe")
                self.safe_update_ui(lambda: self.log("❌ Не найден realesrgan-ncnn-vulkan.exe") or 0)
                return False
            print(f"[UPSCALE] EXE: {exe}")

            models_dir = self.find_models_dir(exe)
            if not models_dir:
                print("[UPSCALE] ❌ Не найдена папка models")
                self.safe_update_ui(lambda: self.log("❌ Не найдена папка models") or 0)
                return False
            print(f"[UPSCALE] Models dir: {models_dir}")

            available = self.list_available_model_names(models_dir)
            if not available:
                print("[UPSCALE] ❌ Нет доступных моделей")
                self.safe_update_ui(lambda: self.log("❌ Нет доступных моделей") or 0)
                return False
            print(f"[UPSCALE] Доступные модели: {', '.join(available)}")

            # Выбор модели
            chosen = self.pick_best_model_for_scale(available, self.upscale_model.get(), self.upscale_scale.get())
            if not chosen:
                print("[UPSCALE] ❌ Не удалось выбрать модель")
                self.safe_update_ui(lambda: self.log("❌ Не удалось выбрать модель") or 0)
                return False

            model_scale = self.parse_scale_from_name(chosen)
            run_scale = model_scale if model_scale else self.upscale_scale.get()

            # Создаем папку upscale
            output_path = input_path / "upscale"
            output_path.mkdir(exist_ok=True)

            print(f"[UPSCALE] Выбрана модель: {chosen}")
            print(f"[UPSCALE] Масштаб модели: x{model_scale if model_scale else 'unknown'}")
            print(f"[UPSCALE] Запускаемый масштаб: x{run_scale}")
            print(f"[UPSCALE] Запрошенный масштаб: x{self.upscale_scale.get()}")
            print(f"[UPSCALE] Размер тайла: {self.upscale_tile.get()}")
            print(f"[UPSCALE] GPU: {self.upscale_gpu.get()}")
            print(f"[UPSCALE] Выходная папка: {output_path}")
            self.safe_update_ui(lambda: self.log(f"📦 Модель: {chosen}, масштаб: x{run_scale}") or 0)

            # Начинаем измерение времени upscale
            self.upscale_start_time = time.time()
            estimated_time = self.estimate_upscale_time(total_images)
            self.estimated_upscale_time = estimated_time
            if estimated_time:
                self.safe_update_ui(lambda: self.log(f"⏱️ Оценка времени upscale: {self.format_time(estimated_time)}") or 0)
            
            # Сразу показываем начальное значение таймера
            self.safe_update_ui(lambda: self.upscale_time_var.set("Прошло: 0 сек") or 0)
            
            # Запускаем обновление таймера через главный поток
            self.safe_after(1000, lambda: self.update_upscale_timer())

            # Запуск realesrgan с отслеживанием прогресса
            cmd = [str(exe), "-m", str(models_dir), "-n", chosen,
                   "-i", str(input_path), "-o", str(output_path), "-s", str(run_scale),
                   "-f", "jpg", "-t", str(self.upscale_tile.get()),
                   "-j", "4:4:4", "-g", str(self.upscale_gpu.get())]

            # Логируем команду в консоль
            cmd_str = " ".join(cmd)
            print(f"[UPSCALE] Команда: {cmd_str}")
            self.safe_update_ui(lambda: self.log(f"🚀 Запуск upscale...") or 0)

            # Запускаем процесс с отслеживанием прогресса в реальном времени
            # Используем универсальный newline для кроссплатформенности
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                   text=True, bufsize=1, universal_newlines=True)

            # Отслеживаем прогресс через периодическую проверку количества обработанных файлов
            processed_count = 0
            last_count = 0
            no_progress_count = 0
            stdout_lines = []

            # Читаем вывод в реальном времени
            import threading
            def read_output():
                nonlocal stdout_lines
                try:
                    for line in proc.stdout:
                        line = line.rstrip()
                        if line:
                            stdout_lines.append(line)
                            print(f"[UPSCALE] {line}")
                except Exception as e:
                    print(f"[UPSCALE] Ошибка чтения вывода: {e}")

            output_thread = threading.Thread(target=read_output, daemon=True)
            output_thread.start()

            while proc.poll() is None:
                # Проверяем количество обработанных файлов
                try:
                    current_files = [f for f in output_path.iterdir()
                                   if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
                    processed_count = len(current_files)

                    # Обновляем прогресс если есть изменения
                    if processed_count != last_count:
                        last_count = processed_count
                        no_progress_count = 0
                        progress_pct = min(100, int((processed_count / total_images) * 100))
                        print(f"[UPSCALE] Прогресс: {processed_count}/{total_images} ({progress_pct}%)")
                        self.safe_update_ui(lambda c=processed_count, t=total_images, p=progress_pct:
                                          self.upscale_progress_bar.config(value=c) or 0)
                        self.safe_update_ui(lambda c=processed_count, t=total_images:
                                          self.upscale_progress_var.set(f"Upscale: {c}/{t} ({progress_pct}%)") or 0)
                    else:
                        no_progress_count += 1
                        # Если долго нет прогресса, показываем что процесс идет
                        if no_progress_count % 10 == 0:
                            print(f"[UPSCALE] Ожидание прогресса... ({processed_count}/{total_images})")
                            self.safe_update_ui(lambda c=processed_count, t=total_images:
                                              self.upscale_progress_var.set(f"Upscale: {c}/{t} (обработка...)") or 0)
                except Exception as e:
                    print(f"[UPSCALE] Ошибка проверки прогресса: {e}")

                time.sleep(0.5)  # Проверяем каждые 0.5 секунды

            # Ждем завершения потока чтения
            output_thread.join(timeout=2)

            # Ждем завершения процесса
            proc.wait()

            if proc.returncode != 0:
                error_output = "\n".join(stdout_lines[-20:]) if stdout_lines else "Неизвестная ошибка"
                print(f"[UPSCALE] Ошибка (код {proc.returncode}): {error_output}")
                self.safe_update_ui(lambda e=error_output: self.log(f"❌ Ошибка upscale: {e}") or 0)
                return False
            else:
                print(f"[UPSCALE] Процесс завершен успешно (код {proc.returncode})")

            # Если нужен другой масштаб - ресэмплируем
            if run_scale != self.upscale_scale.get():
                print(f"[UPSCALE] Ресэмплинг с x{run_scale} на x{self.upscale_scale.get()}...")
                self.safe_update_ui(lambda: self.upscale_progress_var.set("Ресэмплинг результатов...") or 0)
                self.rescale_outputs_to_requested(output_path, run_scale, self.upscale_scale.get())
                print(f"[UPSCALE] Ресэмплинг завершен")

            # Подсчитываем результат
            result_files = [f for f in output_path.iterdir()
                           if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]

            # Сохраняем время upscale
            if self.upscale_start_time:
                elapsed_time = time.time() - self.upscale_start_time
                if len(result_files) > 0:
                    self.add_upscale_timing(len(result_files), elapsed_time)
                    self.safe_update_ui(lambda: self.log(f"⏱️ Время upscale: {self.format_time(elapsed_time)}") or 0)
                self.upscale_start_time = None
                self.estimated_upscale_time = None
                self.safe_update_ui(lambda: self.upscale_time_var.set("") or 0)

            print(f"[UPSCALE] ===== Upscale завершен =====")
            print(f"[UPSCALE] Обработано изображений: {len(result_files)}/{total_images}")
            print(f"[UPSCALE] Выходная папка: {output_path}")
            self.safe_update_ui(lambda: self.upscale_progress_bar.config(value=total_images) or 0)
            self.safe_update_ui(lambda: self.upscale_progress_var.set(f"✓ Upscale завершен: {len(result_files)} изображений") or 0)
            self.safe_update_ui(lambda: self.log(f"✅ Upscale завершен: {output_path} ({len(result_files)} изображений)") or 0)
            return True

        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"[UPSCALE] ❌ Исключение: {e}")
            print(f"[UPSCALE] Детали:\n{error_details}")
            self.safe_update_ui(lambda e=e, d=error_details:
                              self.log(f"❌ Ошибка upscale: {e}\nДетали: {d}") or 0)
            return False

    def rescale_outputs_to_requested(self, out_dir, run_scale, want_scale):
        """Ресэмплинг результатов если масштаб не совпадает"""
        if run_scale == want_scale:
            return
        ratio = want_scale / run_scale
        outs = [p for p in sorted(out_dir.iterdir()) if p.is_file() and p.suffix.lower() in {".jpg", ".png", ".webp"}]
        if not outs:
            print(f"[UPSCALE] Ресэмплинг: нет файлов для обработки в {out_dir}")
            return

        print(f"[UPSCALE] Ресэмплинг {len(outs)} файлов: {run_scale}x → {want_scale}x (коэффициент: {ratio:.3f})")
        self.safe_update_ui(lambda: self.log(f"Ресэмплинг {run_scale}x→{want_scale}x...") or 0)
        for i, p in enumerate(outs, 1):
            try:
                im = Image.open(p).convert("RGB")
                w, h = im.size
                new_w = max(1, int(round(w * ratio)))
                new_h = max(1, int(round(h * ratio)))
                print(f"[UPSCALE] Ресэмплинг [{i}/{len(outs)}] {p.name}: {w}x{h} → {new_w}x{new_h}")
                im2 = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
                im2.save(p, quality=95)
            except Exception as e:
                print(f"[UPSCALE] ❌ Ошибка ресэмплинга {p.name}: {e}")
                self.safe_update_ui(lambda: self.log(f"Ошибка ресэмплинга {p.name}: {e}") or 0)
        print(f"[UPSCALE] Ресэмплинг завершен: {len(outs)} файлов")

    def download_multiple_worker(self, urls):
        """Обработка нескольких URL последовательно с переиспользованием браузера"""
        all_downloaded_folders = []

        # Инициализируем парсер один раз для всех URL
        try:
            # Создаем парсер с базовыми настройками
            base_parser = PinterestParser(download_folder=self.download_folder.get())
            base_parser.scroll_delay = self.scroll_delay.get()
            base_parser.download_delay = self.download_delay.get()
            base_parser.image_quality = self.image_quality.get()
            base_parser.max_workers = 5

            # Инициализируем браузер один раз
            self.safe_update_ui(lambda: self.progress_var.set("Инициализация браузера...") or 0)
            base_parser.init_driver()
            self.parser = base_parser

            # Передаем список словарей с настройками вместо простых URL
            urls_with_settings = []

            # Собираем настройки для каждого URL
            for url in urls:
                # Ищем в url_list
                url_settings = None
                for item in self.url_list:
                    if item["url"] == url:
                        url_settings = item
                        break

                # Если не нашли в списке, создаем с настройками по умолчанию
                if not url_settings:
                    url_settings = {
                        "url": url,
                        "board_name": None,
                        "max_images": self.max_images.get()
                    }

                urls_with_settings.append(url_settings)

            for idx, url_settings in enumerate(urls_with_settings):
                if not self.is_downloading:
                    break

                self.current_url_index = idx + 1
                url = url_settings["url"]
                board_name = url_settings.get("board_name")
                max_images_for_url = url_settings.get("max_images", 0)

                display_url = f"{board_name} - {url}" if board_name else url
                max_display = f" (макс. {max_images_for_url})" if max_images_for_url > 0 else " (все изображения)"
                self.safe_update_ui(lambda u=display_url, i=idx+1, t=len(urls_with_settings), m=max_display:
                                  self.log(f"\n=== Обработка URL {i}/{t}: {u}{m} ===") or 0)

                try:
                    download_folder = self.download_single_url(url, board_name, reuse_parser=True, max_images=max_images_for_url)
                    if download_folder:
                        all_downloaded_folders.append(download_folder)
                except Exception as e:
                    import traceback
                    error_details = traceback.format_exc()
                    self.safe_update_ui(lambda e=e, d=error_details:
                                      self.log(f"❌ Ошибка при обработке URL: {e}\nДетали: {d}") or 0)

                # Небольшая пауза между URL
                if idx < len(urls_with_settings) - 1 and self.is_downloading:
                    time.sleep(2)

        finally:
            # Закрываем браузер только после обработки всех URL
            if self.parser:
                try:
                    self.safe_update_ui(lambda: self.log("Закрываю браузер...") or 0)
                    self.parser.close()
                    self.parser = None
                except Exception as e:
                    self.safe_update_ui(lambda e=e: self.log(f"Ошибка при закрытии браузера: {e}") or 0)

        # После завершения всех скачиваний - запускаем upscale если включен
        if self.enable_upscale.get() and all_downloaded_folders:
            self.safe_update_ui(lambda: self.log(f"\n=== Запуск upscale ===") or 0)
            for folder in all_downloaded_folders:
                if self.is_downloading:  # Проверяем не остановили ли процесс
                    self.run_upscale(folder)

        self.safe_update_ui(lambda: self.log(f"\n=== Все задачи завершены ===") or 0)
        self.root.after(0, self.update_ui_after_stop)

    def download_single_url(self, url, board_name=None, reuse_parser=False, max_images=0):
        """Скачивание одного URL (вынесено из download_worker)"""
        return self.download_worker(url, board_name, reuse_parser, max_images)

    def download_worker(self, url, pre_fetched_board_name=None, reuse_parser=False, max_images=0):
        """Рабочий поток для скачивания"""
        try:
            # Определяем папку для скачивания (с учетом автоподпапок)
            download_folder = self.download_folder.get()
            board_name = pre_fetched_board_name

            # Если название доски не было получено заранее, получаем его сейчас
            if self.auto_subfolder.get() and not board_name:
                try:
                    temp_parser = PinterestParser(download_folder=download_folder)
                    expanded_url = temp_parser.expand_short_url(url)
                    board_name = temp_parser.get_board_name_from_url(expanded_url)
                except:
                    pass

            # Используем название доски для создания подпапки
            if self.auto_subfolder.get() and board_name:
                # Декодируем board_name, если он в URL-encoded формате (для русских названий)
                decoded_board_name = board_name
                if '%' in board_name:
                    try:
                        decoded_board_name = unquote(board_name)
                    except:
                        decoded_board_name = board_name
                download_folder = os.path.join(self.download_folder.get(), decoded_board_name)
                # Создаем папку если её еще нет
                os.makedirs(download_folder, exist_ok=True)

            # Используем переиспользуемый парсер или создаем новый
            if reuse_parser and self.parser:
                parser = self.parser
                parser.download_folder = download_folder
                parser.setup_download_folder()
            else:
                # Создаем новый парсер с настройками
                parser = PinterestParser(download_folder=download_folder)
                parser.setup_download_folder()
                parser.scroll_delay = self.scroll_delay.get()
                parser.download_delay = self.download_delay.get()
                parser.image_quality = self.image_quality.get()
                parser.max_workers = 5

                # Инициализация браузера только если не переиспользуем
                self.safe_update_ui(lambda: self.progress_var.set("Инициализация браузера...") or 0)
                parser.init_driver()
                self.parser = parser

            # Сохраняем название доски для шаблона
            if board_name:
                parser.current_board_name = board_name

            # Расширяем короткий URL если нужно
            expanded_url = url
            try:
                expanded_url = parser.expand_short_url(url)
            except Exception as e:
                self.safe_update_ui(lambda e=e: self.log(f"⚠️ Не удалось расширить короткий URL: {e}") or 0)

            # Открытие страницы (браузер уже открыт если переиспользуем)
            self.safe_update_ui(lambda: self.progress_var.set("Открытие страницы...") or 0)
            parser.driver.get(expanded_url)
            time.sleep(5)

            # Получаем название доски из страницы если еще не получили
            if not board_name and self.auto_subfolder.get():
                try:
                    board_name = parser.get_board_name_from_url(expanded_url)
                    if board_name:
                        parser.current_board_name = board_name
                        # Обновляем папку если название доски было получено только сейчас
                        # Декодируем board_name, если он в URL-encoded формате (для русских названий)
                        decoded_board_name = board_name
                        if '%' in board_name:
                            try:
                                decoded_board_name = unquote(board_name)
                            except:
                                decoded_board_name = board_name
                        download_folder = os.path.join(self.download_folder.get(), decoded_board_name)
                        os.makedirs(download_folder, exist_ok=True)
                        parser.download_folder = download_folder
                except:
                    pass

            # Прокрутка и извлечение URL
            # Используем max_images переданный в функцию, если не передан - используем глобальную настройку
            if max_images == 0:
                max_count = self.max_images.get()
            else:
                max_count = max_images

            if max_count > 0:
                self.safe_update_ui(lambda: self.progress_var.set(f"Поиск первых {max_count} изображений...") or 0)
            else:
                self.safe_update_ui(lambda: self.progress_var.set("Поиск изображений...") or 0)

            # Прокручиваем и собираем изображения с ограничением
            # Если указано ограничение, собираем только первые N (самые новые)
            parser.scroll_and_load_images(max_images=max_count if max_count > 0 else None)
            image_urls = parser.extract_image_urls(max_images=max_count if max_count > 0 else None)

            # Логирование уже выполняется в extract_image_urls(), но можно добавить дополнительное сообщение
            if max_count > 0 and len(image_urls) > 0:
                self.safe_update_ui(lambda: self.log(f"Найдено {len(image_urls)} изображений для скачивания") or 0)

            self.image_urls_list = image_urls
            self.stats["found"] += len(image_urls)
            self.total_images_to_download += len(image_urls)

            self.safe_update_ui(lambda: self.update_stats() or 0)
            # Устанавливаем максимум прогресс-бара на общее количество изображений (обновляем каждый раз)
            self.safe_update_ui(lambda t=self.total_images_to_download: self.progress_bar.config(maximum=max(1, t)) or 0)

            if not image_urls:
                self.safe_update_ui(lambda: self.log("⚠️ Изображения не найдены") or 0)
                if not reuse_parser:
                    parser.close()
                return download_folder  # Возвращаем папку даже если изображений нет

            self.safe_update_ui(lambda: self.log(f"✓ Найдено {len(image_urls)} изображений") or 0)

            # Начинаем измерение времени скачивания
            self.download_start_time = time.time()
            estimated_time = self.estimate_download_time(len(image_urls))
            self.estimated_download_time = estimated_time
            if estimated_time:
                self.safe_update_ui(lambda: self.log(f"⏱️ Оценка времени скачивания: {self.format_time(estimated_time)}") or 0)
            
            # Сразу показываем начальное значение таймера
            self.safe_update_ui(lambda: self.time_var.set("Прошло: 0 сек") or 0)
            
            # Запускаем обновление таймера через главный поток
            self.safe_after(1000, lambda: self.update_download_timer())

            # Скачивание изображений
            downloaded = 0
            failed = 0
            skipped = 0

            for index, img_url in enumerate(image_urls):
                if not self.is_downloading:
                    break

                # Ожидание при паузе
                while self.is_paused and self.is_downloading:
                    time.sleep(0.5)

                if not self.is_downloading:
                    break

                # Получение URL с нужным качеством
                full_url = None
                try:
                    full_url = parser.get_full_image_url(img_url, parser.image_quality)
                except Exception as e:
                    self.safe_update_ui(lambda e=e, u=img_url:
                                      self.log(f"❌ Ошибка получения полного URL для {u[:50]}...: {e}") or 0)
                    failed += 1
                    self.current_downloaded_count += 1
                    # Обновляем прогресс даже при ошибке
                    self.safe_update_ui(lambda c=self.current_downloaded_count, t=self.total_images_to_download:
                                      self.progress_bar.config(value=c) or 0)
                    self.safe_update_ui(lambda i=index+1, t=len(image_urls), c=self.current_downloaded_count, tot=self.total_images_to_download:
                                      self.progress_var.set(f"Скачивание: {i}/{t} (всего: {c}/{tot})") or 0)
                    self.stats["failed"] = failed
                    self.safe_after(0, lambda: self.update_stats() or 0)
                    continue

                if not full_url:
                    failed += 1
                    self.current_downloaded_count += 1
                    self.safe_update_ui(lambda u=img_url:
                                      self.log(f"❌ Не удалось получить полный URL для изображения: {u[:50]}...") or 0)
                    # Обновляем прогресс даже при ошибке
                    self.safe_update_ui(lambda c=self.current_downloaded_count, t=self.total_images_to_download:
                                      self.progress_bar.config(value=c) or 0)
                    self.safe_update_ui(lambda i=index+1, t=len(image_urls), c=self.current_downloaded_count, tot=self.total_images_to_download:
                                      self.progress_var.set(f"Скачивание: {i}/{t} (всего: {c}/{tot})") or 0)
                    self.stats["failed"] = failed
                    self.safe_after(0, lambda: self.update_stats() or 0)
                    continue

                # Генерация имени файла с шаблоном
                if self.auto_rename.get() and self.filename_template.get():
                    filename = parser.get_filename_from_url(full_url, index + 1,
                                                          self.filename_template.get())
                else:
                    filename = parser.get_filename_from_url(full_url, index + 1)
                    if self.auto_rename.get():
                        filename = f"pin_{index+1:04d}_{filename}"

                filepath = os.path.join(parser.download_folder, filename)

                # Пропуск существующих (resume функционал)
                if os.path.exists(filepath) and self.resume_download.get():
                    skipped += 1
                    self.current_downloaded_count += 1
                    # Обновляем прогресс даже для пропущенных файлов
                    self.safe_update_ui(lambda c=self.current_downloaded_count, t=self.total_images_to_download:
                                      self.progress_bar.config(value=c) or 0)
                    self.safe_update_ui(lambda i=index+1, t=len(image_urls), c=self.current_downloaded_count, tot=self.total_images_to_download:
                                      self.progress_var.set(f"Скачивание: {i}/{t} (всего: {c}/{tot})") or 0)
                    self.safe_update_ui(lambda f=filename: self.log(f"⏭ Пропущено (уже существует): {f}") or 0)
                    continue
                elif os.path.exists(filepath) and not self.resume_download.get():
                    # Если resume отключен, перезаписываем
                    try:
                        os.remove(filepath)
                    except Exception as e:
                        self.safe_update_ui(lambda e=e: self.log(f"⚠️ Не удалось удалить существующий файл: {e}") or 0)

                # Скачивание
                self.safe_update_ui(lambda f=filename: self.log(f"⬇ Скачиваю: {f}") or 0)

                download_success = False
                try:
                    download_success = parser.download_image(full_url, filename)
                except Exception as e:
                    import traceback
                    error_details = traceback.format_exc()
                    self.safe_update_ui(lambda e=e, f=filename, d=error_details:
                                      self.log(f"❌ Исключение при скачивании {f}: {e}\nДетали: {d}") or 0)
                    download_success = False

                if download_success:
                    # Проверка размера файла
                    try:
                        if os.path.exists(filepath):
                            file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
                            if file_size_mb < self.min_size_mb.get() or file_size_mb > self.max_size_mb.get():
                                try:
                                    os.remove(filepath)
                                except:
                                    pass
                                skipped += 1
                                self.current_downloaded_count += 1
                                self.safe_update_ui(lambda f=filename, s=file_size_mb:
                                                  self.log(f"⏭ Пропущено (размер {s:.2f} МБ не подходит): {f}") or 0)
                                # Обновляем прогресс
                                self.safe_update_ui(lambda c=self.current_downloaded_count, t=self.total_images_to_download:
                                                  self.progress_bar.config(value=c) or 0)
                                self.safe_update_ui(lambda i=index+1, t=len(image_urls), c=self.current_downloaded_count, tot=self.total_images_to_download:
                                                  self.progress_var.set(f"Скачивание: {i}/{t} (всего: {c}/{tot})") or 0)
                                self.stats["skipped"] = skipped
                                self.stats["downloaded"] = downloaded
                                self.stats["failed"] = failed
                                self.safe_after(0, lambda: self.update_stats() or 0)
                                continue
                            else:
                                downloaded += 1
                                self.current_downloaded_count += 1
                                self.safe_update_ui(lambda f=filename, s=file_size_mb:
                                                  self.log(f"✓ Скачано ({s:.2f} МБ): {f}") or 0)
                        else:
                            # Файл не был создан
                            failed += 1
                            self.safe_update_ui(lambda f=filename:
                                              self.log(f"❌ Файл не был создан: {f}") or 0)
                    except Exception as e:
                        self.safe_update_ui(lambda e=e, f=filename:
                                          self.log(f"⚠️ Ошибка проверки размера файла {f}: {e}") or 0)
                        # Считаем успешным если файл существует
                        if os.path.exists(filepath):
                            downloaded += 1
                            self.current_downloaded_count += 1
                else:
                    failed += 1
                    self.current_downloaded_count += 1
                    self.safe_update_ui(lambda f=filename, u=full_url[:50]:
                                      self.log(f"❌ Ошибка скачивания: {f} (URL: {u}...)") or 0)

                # Обновляем прогресс после каждой попытки скачивания (включая ошибки)
                self.safe_update_ui(lambda c=self.current_downloaded_count, t=self.total_images_to_download:
                                  self.progress_bar.config(value=c) or 0)
                self.safe_update_ui(lambda i=index+1, t=len(image_urls), c=self.current_downloaded_count, tot=self.total_images_to_download:
                                  self.progress_var.set(f"Скачивание: {i}/{t} (всего: {c}/{tot})") or 0)
                # Таймер обновляется автоматически каждую секунду

                self.stats["downloaded"] = downloaded
                self.stats["skipped"] = skipped
                self.stats["failed"] = failed
                self.safe_after(0, lambda: self.update_stats() or 0)

                time.sleep(self.download_delay.get())

            # Завершение - сохраняем время скачивания
            if self.download_start_time:
                elapsed_time = time.time() - self.download_start_time
                total_downloaded = downloaded + skipped  # Учитываем и пропущенные
                if total_downloaded > 0:
                    self.add_download_timing(total_downloaded, elapsed_time)
                    self.safe_update_ui(lambda: self.log(f"⏱️ Время скачивания: {self.format_time(elapsed_time)}") or 0)
                self.download_start_time = None
                self.estimated_download_time = None
                self.safe_update_ui(lambda: self.time_var.set("") or 0)

            # Завершение
            self.safe_update_ui(lambda: self.log(f"\n✓ Скачивание завершено!") or 0)
            self.safe_update_ui(lambda: self.log(f"Успешно: {downloaded} | Ошибок: {failed} | Пропущено: {skipped}") or 0)

            # Экспорт метаданных в JSON
            if self.export_metadata.get():
                self.export_metadata_json(parser.download_folder, image_urls, url, downloaded, failed, skipped)

            # Уведомление Windows
            if self.windows_notifications.get():
                self.show_notification(f"Скачивание завершено!",
                                     f"Успешно: {downloaded} | Ошибок: {failed} | Пропущено: {skipped}")

            # Сохранение в историю (с названием доски)
            # Декодируем board_name перед сохранением, если он в URL-encoded формате
            saved_board_name = board_name
            if board_name and '%' in board_name:
                try:
                    saved_board_name = unquote(board_name)
                except:
                    saved_board_name = board_name
            
            history_item = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "url": url,
                "board_name": saved_board_name,  # Сохраняем декодированное название доски
                "count": downloaded,
                "total": len(image_urls)
            }
            self.history.append(history_item)
            self.save_history()

            # НЕ закрываем браузер если переиспользуем парсер
            if not reuse_parser:
                parser.close()

            # Возвращаем папку для возможного upscale
            return parser.download_folder

        except Exception as e:
            self.safe_update_ui(lambda: self.log(f"Ошибка: {e}") or 0)
            if self.parser:
                try:
                    self.parser.close()
                except:
                    pass
            return None

    def update_stats(self):
        """Обновление статистики"""
        stats_text = f"Найдено: {self.stats['found']} | Скачано: {self.stats['downloaded']} | Ошибок: {self.stats['failed']} | Пропущено: {self.stats['skipped']}"
        self.stats_label.config(text=stats_text)

    def update_download_timer(self):
        """Обновление таймера скачивания"""
        if not self.download_start_time:
            return
        
        elapsed = time.time() - self.download_start_time
        if self.estimated_download_time:
            remaining = self.format_remaining_time(elapsed, self.estimated_download_time)
            elapsed_str = self.format_time(elapsed)
            timer_text = f"Прошло: {elapsed_str} | Осталось: {remaining}"
        else:
            elapsed_str = self.format_time(elapsed)
            timer_text = f"Прошло: {elapsed_str}"
        
        self.safe_update_ui(lambda t=timer_text: self.time_var.set(t) or 0)
        
        # Планируем следующее обновление через 1 секунду
        if self.is_downloading and self.download_start_time:
            self.safe_after(1000, lambda: self.update_download_timer())

    def update_upscale_timer(self):
        """Обновление таймера upscale"""
        if not self.upscale_start_time:
            return
        
        elapsed = time.time() - self.upscale_start_time
        if self.estimated_upscale_time:
            remaining = self.format_remaining_time(elapsed, self.estimated_upscale_time)
            elapsed_str = self.format_time(elapsed)
            timer_text = f"Прошло: {elapsed_str} | Осталось: {remaining}"
        else:
            elapsed_str = self.format_time(elapsed)
            timer_text = f"Прошло: {elapsed_str}"
        
        self.safe_update_ui(lambda t=timer_text: self.upscale_time_var.set(t) or 0)
        
        # Планируем следующее обновление через 1 секунду
        if self.is_downloading and self.upscale_start_time:
            self.safe_after(1000, lambda: self.update_upscale_timer())

    def update_ui_after_stop(self):
        """Обновление UI после остановки"""
        self.is_downloading = False
        self.is_paused = False
        self.start_btn.config(state=tk.NORMAL, text="Запустить")  # Восстанавливаем текст кнопки
        self.pause_btn.config(state=tk.DISABLED, text="Пауза")
        self.stop_btn.config(state=tk.DISABLED)
        self.progress_var.set("Готов к работе")
        self.progress_bar.config(value=0, maximum=100)
        self.time_var.set("")
        self.upscale_progress_var.set("")
        self.upscale_progress_bar.config(value=0, maximum=100)
        self.upscale_time_var.set("")
        self.total_images_to_download = 0
        self.current_downloaded_count = 0

    def export_urls(self):
        """Экспорт списка URL"""
        if not self.image_urls_list:
            messagebox.showwarning("Внимание", "Список URL пуст")
            return

        filename = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )

        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    for url in self.image_urls_list:
                        f.write(f"{url}\n")
                messagebox.showinfo("Успех", f"Экспортировано {len(self.image_urls_list)} URL")
            except Exception as e:
                messagebox.showerror("Ошибка", f"Не удалось экспортировать: {e}")

    def show_preview(self):
        """Показать предпросмотр найденных изображений"""
        if not self.image_urls_list:
            messagebox.showwarning("Внимание", "Список изображений пуст. Сначала выполните поиск.")
            return

        preview_window = tk.Toplevel(self.root)
        preview_window.title(f"Предпросмотр изображений ({len(self.image_urls_list)})")
        preview_window.geometry("800x600")

        # Canvas с прокруткой
        canvas_frame = ttk.Frame(preview_window)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        canvas = tk.Canvas(canvas_frame)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Загрузка миниатюр
        def load_thumbnails():
            max_preview = min(50, len(self.image_urls_list))  # Максимум 50 превью

            # Показываем индикатор загрузки
            loading_label = ttk.Label(scrollable_frame, text="Загрузка миниатюр...",
                                    font=("Arial", 12))
            loading_label.grid(row=0, column=0, columnspan=3, pady=20)
            preview_window.update()

            for i, url in enumerate(self.image_urls_list[:max_preview]):
                try:
                    # Создаем фрейм для каждого изображения
                    img_frame = ttk.Frame(scrollable_frame)
                    img_frame.grid(row=(i//3)+1, column=i%3, padx=5, pady=5, sticky="nsew")

                    # Загружаем миниатюру
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Referer': 'https://www.pinterest.com/'
                    }
                    response = requests.get(url, timeout=10, stream=True, headers=headers)
                    if response.status_code == 200:
                        try:
                            img_data = response.content
                            img = Image.open(BytesIO(img_data))
                            img.thumbnail((150, 150), Image.Resampling.LANCZOS)
                            photo = ImageTk.PhotoImage(img)

                            label = ttk.Label(img_frame, image=photo)
                            label.image = photo  # Сохраняем ссылку
                            label.pack()

                            # Информация об изображении
                            info_label = ttk.Label(img_frame, text=f"#{i+1}", font=("Arial", 8))
                            info_label.pack()
                        except Exception as img_error:
                            # Если не удалось обработать изображение
                            placeholder = ttk.Label(img_frame, text=f"#{i+1}\nОшибка\nизображения",
                                                 width=20, height=10, foreground="red")
                            placeholder.pack()
                    else:
                        # Если не удалось загрузить, показываем заглушку
                        placeholder = ttk.Label(img_frame, text=f"#{i+1}\nНе загружено",
                                             width=20, height=10)
                        placeholder.pack()
                except Exception as e:
                    # В случае ошибки показываем заглушку
                    placeholder = ttk.Label(img_frame, text=f"#{i+1}\nОшибка",
                                         width=20, height=10, foreground="red")
                    placeholder.pack()

                # Обновляем прогресс
                if (i + 1) % 10 == 0:
                    loading_label.config(text=f"Загружено {i+1}/{max_preview}...")
                    preview_window.update()

            # Удаляем индикатор загрузки
            loading_label.destroy()

            if len(self.image_urls_list) > max_preview:
                info_label = ttk.Label(scrollable_frame,
                                     text=f"... и еще {len(self.image_urls_list) - max_preview} изображений",
                                     font=("Arial", 10))
                info_label.grid(row=(max_preview//3)+2, column=0, columnspan=3, pady=10)

        # Запускаем загрузку в отдельном потоке
        threading.Thread(target=load_thumbnails, daemon=True).start()

        # Кнопка закрытия
        ttk.Button(preview_window, text="Закрыть",
                  command=preview_window.destroy).pack(pady=10)

    def show_notification(self, title, message):
        """Показать уведомление Windows"""
        if HAS_TOAST:
            try:
                toaster = ToastNotifier()
                toaster.show_toast(title, message, duration=5, threaded=True)
            except:
                pass
        else:
            # Fallback на messagebox если win10toast не установлен
            try:
                messagebox.showinfo(title, message)
            except:
                pass

    def export_metadata_json(self, folder, image_urls, url, downloaded, failed, skipped):
        """Экспорт метаданных скачивания в JSON"""
        try:
            metadata = {
                "download_date": datetime.now().isoformat(),
                "source_url": url,
                "total_found": len(image_urls),
                "downloaded": downloaded,
                "failed": failed,
                "skipped": skipped,
                "download_folder": folder,
                "images": []
            }

            # Собираем информацию о скачанных файлах
            for index, img_url in enumerate(image_urls):
                try:
                    if self.auto_rename.get() and self.filename_template.get():
                        filename = self.parser.get_filename_from_url(img_url, index + 1,
                                                                     self.filename_template.get())
                    else:
                        filename = self.parser.get_filename_from_url(img_url, index + 1)
                        if self.auto_rename.get():
                            filename = f"pin_{index+1:04d}_{filename}"

                    filepath = os.path.join(folder, filename)

                    image_info = {
                        "index": index + 1,
                        "url": img_url,
                        "filename": filename,
                        "downloaded": os.path.exists(filepath)
                    }

                    if os.path.exists(filepath):
                        image_info["file_size"] = os.path.getsize(filepath)
                        image_info["file_size_mb"] = round(os.path.getsize(filepath) / (1024 * 1024), 2)
                        image_info["modified_date"] = datetime.fromtimestamp(
                            os.path.getmtime(filepath)).isoformat()

                    metadata["images"].append(image_info)
                except:
                    pass

            # Сохраняем JSON
            json_filename = os.path.join(folder, f"metadata_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(json_filename, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

            self.safe_update_ui(lambda: self.log(f"Метаданные экспортированы: {json_filename}") or 0)
        except Exception as e:
            self.safe_update_ui(lambda: self.log(f"Ошибка экспорта метаданных: {e}") or 0)

    def open_folder(self):
        """Открыть папку с изображениями"""
        folder = self.download_folder.get()
        if os.path.exists(folder):
            os.startfile(folder)
        else:
            messagebox.showwarning("Внимание", "Папка не существует")


def main():
    root = tk.Tk()
    app = PinterestDownloaderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
