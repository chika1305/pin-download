"""
Парсер для скачивания изображений с Pinterest
Поддерживает скачивание с досок и страниц пользователей в полном размере
"""

import os
import time
import requests
import subprocess
import shutil
import hashlib
from urllib.parse import urlparse, parse_qs, unquote
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import re


class PinterestParser:
    def __init__(self, download_folder="pinterest_images"):
        """
        Инициализация парсера

        Args:
            download_folder: Папка для сохранения изображений
        """
        self.download_folder = download_folder
        self.driver = None
        self.scroll_delay = 2.0  # Задержка при прокрутке
        self.download_delay = 0.5  # Задержка между скачиваниями
        self.image_quality = "full"  # Качество изображений: full, medium, small
        self.max_workers = 5  # Количество потоков для параллельного скачивания
        self.session = None  # Переиспользуемая сессия requests
        self.setup_download_folder()

    def setup_download_folder(self):
        """Создает папку для скачивания, если её нет"""
        if not os.path.exists(self.download_folder):
            os.makedirs(self.download_folder)
            print(f"Создана папка: {self.download_folder}")

    def get_browser_cookies(self):
        """Получает cookies из открытого браузера Selenium"""
        cookies_dict = {}
        if self.driver:
            try:
                cookies = self.driver.get_cookies()
                for cookie in cookies:
                    cookies_dict[cookie['name']] = cookie['value']
            except:
                pass
        return cookies_dict

    def init_session(self):
        """Инициализирует переиспользуемую сессию requests"""
        if self.session is None:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.pinterest.com/',
                'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Sec-Fetch-Dest': 'image',
                'Sec-Fetch-Mode': 'no-cors',
                'Sec-Fetch-Site': 'cross-site',
                'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"Windows"',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'DNT': '1'
            }
            self.session = requests.Session()
            self.session.headers.update(headers)

            # Пробуем добавить cookies из браузера, если он открыт
            try:
                browser_cookies = self.get_browser_cookies()
                if browser_cookies:
                    self.session.cookies.update(browser_cookies)
            except:
                pass
        return self.session

    def check_chrome_installed(self):
        """Проверяет, установлен ли Chrome"""
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")
        ]

        for path in chrome_paths:
            if os.path.exists(path):
                return True

        # Проверяем через команду where (Windows)
        try:
            result = subprocess.run(['where', 'chrome'],
                                  capture_output=True,
                                  text=True,
                                  timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return True
        except:
            pass

        return False

    def init_driver(self):
        """Инициализация браузера Chrome"""
        try:
            print("Инициализация браузера Chrome...")

            # Проверяем наличие Chrome
            if not self.check_chrome_installed():
                raise Exception(
                    "Google Chrome не найден!\n"
                    "Пожалуйста, установите Google Chrome с официального сайта:\n"
                    "https://www.google.com/chrome/"
                )

            chrome_options = Options()
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

            # Настройки для ускорения работы
            prefs = {
                "profile.default_content_setting_values.notifications": 2
            }
            chrome_options.add_experimental_option("prefs", prefs)

            # Пытаемся установить ChromeDriver
            print("Установка ChromeDriver...")
            driver_path = None

            try:
                # Очищаем кэш webdriver-manager для свежей установки
                driver_path = ChromeDriverManager().install()
                print(f"ChromeDriver найден: {driver_path}")

                # Проверяем, что файл существует
                if not os.path.exists(driver_path):
                    raise FileNotFoundError(f"ChromeDriver не найден по пути: {driver_path}")

                # Проверяем, что это исполняемый файл (для Windows это .exe)
                if os.name == 'nt' and not driver_path.endswith('.exe'):
                    # Ищем .exe файл в той же директории
                    driver_dir = os.path.dirname(driver_path)
                    exe_files = [f for f in os.listdir(driver_dir) if f.endswith('.exe')]
                    if exe_files:
                        driver_path = os.path.join(driver_dir, exe_files[0])
                        print(f"Найден исполняемый файл: {driver_path}")

                service = Service(driver_path)
                self.driver = webdriver.Chrome(service=service, options=chrome_options)

            except Exception as e:
                print(f"Ошибка при установке ChromeDriver через webdriver-manager: {e}")
                print("Пытаюсь использовать системный ChromeDriver...")

                # Пробуем использовать ChromeDriver из PATH или без указания пути
                try:
                    # Пробуем без указания пути (Selenium найдет сам)
                    self.driver = webdriver.Chrome(options=chrome_options)
                except Exception as e2:
                    # Последняя попытка - ищем chromedriver.exe в текущей директории
                    local_driver = os.path.join(os.getcwd(), 'chromedriver.exe')
                    if os.path.exists(local_driver):
                        try:
                            service = Service(local_driver)
                            self.driver = webdriver.Chrome(service=service, options=chrome_options)
                        except Exception as e3:
                            raise Exception(
                                f"Не удалось инициализировать ChromeDriver.\n\n"
                                f"Попробуйте:\n"
                                f"1. Обновить Chrome до последней версии\n"
                                f"2. Перезапустить скрипт (ChromeDriver установится автоматически)\n"
                                f"3. Убедитесь, что у вас есть доступ в интернет\n\n"
                                f"Ошибки:\n"
                                f"- webdriver-manager: {e}\n"
                                f"- системный: {e2}\n"
                                f"- локальный: {e3}"
                            )
                    else:
                        raise Exception(
                            f"Не удалось инициализировать ChromeDriver.\n\n"
                            f"Попробуйте:\n"
                            f"1. Обновить Chrome до последней версии\n"
                            f"2. Перезапустить скрипт (ChromeDriver установится автоматически)\n"
                            f"3. Убедитесь, что у вас есть доступ в интернет\n\n"
                            f"Ошибки:\n"
                            f"- webdriver-manager: {e}\n"
                            f"- системный: {e2}"
                        )

            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            print("✓ Браузер успешно инициализирован")

        except Exception as e:
            print(f"\n✗ Ошибка инициализации браузера: {e}")
            raise

    def get_full_image_url(self, image_url, quality="full"):
        """
        Преобразует URL изображения в URL нужного размера

        Args:
            image_url: Исходный URL изображения
            quality: Качество изображения - "full", "medium", "small"

        Pinterest использует разные форматы URL:
        - Обычно нужно заменить размеры в URL на нужный размер
        - Или использовать специальный формат для получения оригинала
        """
        if not image_url or 'pinimg.com' not in image_url:
            return None

        # Определяем целевой размер в зависимости от качества
        if quality == "full":
            target_size = "originals"
        elif quality == "medium":
            target_size = "736x"  # Средний размер
        elif quality == "small":
            target_size = "564x"  # Маленький размер
        else:
            target_size = "originals"

        # Pinterest использует формат: .../564x/... или .../originals/...
        # Заменяем все варианты размеров на нужный размер

        # Список всех возможных размеров Pinterest
        sizes = ['236x', '474x', '564x', '736x', '750x', '1200x', '1400x']

        for size in sizes:
            if f'/{size}/' in image_url:
                if target_size == "originals":
                    image_url = image_url.replace(f'/{size}/', '/originals/')
                else:
                    image_url = image_url.replace(f'/{size}/', f'/{target_size}/')
                break

        # Если originals есть, но нужен другой размер
        if '/originals/' in image_url and target_size != "originals":
            image_url = image_url.replace('/originals/', f'/{target_size}/')

        # Убираем параметры размера из query string
        parsed_url = urlparse(image_url)
        if parsed_url.query:
            params = parse_qs(parsed_url.query)
            # Удаляем параметры размера
            params.pop('w', None)
            params.pop('h', None)
            params.pop('fit', None)
            params.pop('auto', None)

            # Пересобираем URL без параметров размера
            new_query = '&'.join([f"{k}={v[0]}" for k, v in params.items()])
            image_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
            if new_query:
                image_url += f"?{new_query}"

        return image_url

    def check_similar_pins_section(self):
        """
        Проверяет, появился ли раздел "Похожие пины" на странице

        Returns:
            True если раздел найден, False в противном случае
        """
        try:
            # Тексты, которые указывают на раздел похожих пинов (на разных языках)
            similar_texts = [
                "Показать похожие",
                "Похожие пины",
                "Similar ideas",
                "Show more like this",
                "More like this",
                "Similar pins",
                "Похожие идеи",
                "Más ideas como esta",
                "Ideas similares"
            ]

            # Получаем весь текст страницы
            page_text = self.driver.page_source.lower()

            # Проверяем наличие текстов похожих пинов
            for text in similar_texts:
                if text.lower() in page_text:
                    # Проверяем, виден ли этот текст на экране (в видимой области)
                    try:
                        elements = self.driver.find_elements(By.XPATH, f"//*[contains(text(), '{text}')]")
                        for element in elements:
                            if element.is_displayed():
                                print(f"Обнаружен раздел '{text}' - останавливаю прокрутку")
                                return True
                    except:
                        pass

            return False
        except Exception as e:
            # В случае ошибки продолжаем работу
            return False

    def scroll_and_load_images(self, max_scrolls=50, max_images=None):
        """
        Прокручивает страницу для загрузки изображений
        Останавливается при обнаружении раздела "Похожие пины" или при достижении нужного количества

        Args:
            max_scrolls: Максимальное количество прокруток
            max_images: Максимальное количество изображений для сбора (None = все)
        """
        last_height = self.driver.execute_script("return document.body.scrollHeight")
        scroll_count = 0
        no_new_content_count = 0  # Счетчик отсутствия нового контента
        similar_section_detected_count = 0  # Счетчик обнаружения раздела похожих пинов
        collected_image_data = []  # Список кортежей (y, x, url) для сохранения порядка
        no_images_loaded_count = 0  # Счетчик попыток, когда изображения не загружаются
        end_of_board_retry_count = 0  # Счетчик попыток при достижении конца доски
        ignore_similar_section = False  # Флаг для игнорирования фильтрации похожих пинов при недостатке изображений

        if max_images and max_images > 0:
            print(f"Начинаю прокрутку для загрузки первых {max_images} изображений...")
        else:
            print("Начинаю прокрутку страницы для загрузки всех изображений...")

        while scroll_count < max_scrolls:
            # Если указано ограничение по количеству, проверяем сколько изображений уже собрано
            # Адаптивная частота сбора: чаще собираем, когда близки к нужному количеству
            if max_images and max_images > 0:
                # Определяем частоту сбора в зависимости от того, сколько уже собрано
                current_count = len(collected_image_data)
                # Если обнаружен раздел похожих пинов и собрано мало, собираем каждую прокрутку
                if ignore_similar_section and current_count < max_images * 0.7:
                    collect_frequency = 1  # Собираем каждую прокрутку при недостатке изображений
                elif current_count < max_images * 0.3:  # Меньше 30% - собираем каждую прокрутку
                    collect_frequency = 1
                elif current_count < max_images * 0.7:  # Меньше 70% - собираем каждые 2 прокрутки
                    collect_frequency = 2
                elif current_count < max_images:  # Близко к нужному - собираем каждую прокрутку
                    collect_frequency = 1
                else:  # Больше нужного - собираем реже
                    collect_frequency = 3

                # Собираем изображения с адаптивной частотой
                if scroll_count % collect_frequency == 0:
                    # Добавляем задержку для загрузки lazy-loaded изображений
                    time.sleep(1.0)  # Увеличена задержка для лучшей загрузки
                    # Временно устанавливаем флаг игнорирования для метода extract_image_urls_with_positions
                    self._ignore_similar_section = ignore_similar_section
                    current_images_data = self.extract_image_urls_with_positions()
                    self._ignore_similar_section = False  # Сбрасываем флаг
                    # Добавляем только новые URL (проверяем по URL)
                    seen_urls = {url for _, _, url in collected_image_data}
                    for y, x, url in current_images_data:
                        if url not in seen_urls:
                            collected_image_data.append((y, x, url))
                            seen_urls.add(url)

                    # Если собрано 0 изображений, продолжаем прокрутку дальше, но с ограничением
                    if len(collected_image_data) == 0:
                        no_images_loaded_count += 1
                        if no_images_loaded_count <= 15:  # Максимум 15 попыток
                            print(f"Изображения еще не загрузились (попытка {no_images_loaded_count}/15), продолжаю прокрутку...")
                            continue
                        else:
                            print("Изображения не загружаются после 15 попыток, останавливаю прокрутку")
                            break
                    else:
                        no_images_loaded_count = 0  # Сбрасываем счетчик, если изображения найдены

                    required_count = int(max_images * 1.5)  # Запас 50%
                    if len(collected_image_data) >= required_count:
                        print(f"Собрано достаточно изображений: {len(collected_image_data)} >= {required_count} (нужно {max_images})")
                        break

            # Проверяем наличие раздела "Похожие пины" перед прокруткой
            has_similar = self.check_similar_pins_section()
            if has_similar:
                similar_section_detected_count += 1

                if max_images and max_images > 0:
                    # Проверяем, собрано ли достаточно изображений
                    if len(collected_image_data) >= max_images:
                        print(f"Обнаружен раздел похожих пинов, собрано {len(collected_image_data)}/{max_images} - останавливаю прокрутку")
                        ignore_similar_section = False  # Отключаем игнорирование
                        break
                    else:
                        # Если собрано недостаточно, ИГНОРИРУЕМ раздел похожих пинов и продолжаем агрессивно
                        if len(collected_image_data) < max_images * 0.5:  # Если собрано меньше половины
                            ignore_similar_section = True  # Включаем игнорирование фильтрации
                            print(f"Обнаружен раздел похожих пинов, но собрано только {len(collected_image_data)}/{max_images} - ИГНОРИРУЮ раздел и продолжаю агрессивную прокрутку")
                        elif similar_section_detected_count >= 10:  # Увеличено до 10 для большей настойчивости
                            ignore_similar_section = True
                            print(f"Раздел похожих пинов обнаружен {similar_section_detected_count} раз подряд, но собрано только {len(collected_image_data)}/{max_images} - ИГНОРИРУЮ раздел и продолжаю прокрутку")
                            similar_section_detected_count = 5  # Сбрасываем счетчик, но не полностью
                        else:
                            ignore_similar_section = True  # Включаем игнорирование при недостатке изображений
                            print(f"Обнаружен раздел похожих пинов ({similar_section_detected_count}/10), собрано только {len(collected_image_data)}/{max_images} - продолжаю прокрутку (игнорирую фильтрацию)")
                else:
                    # Если не указано ограничение, останавливаемся при обнаружении похожих пинов
                    if similar_section_detected_count >= 3:
                        print(f"Раздел похожих пинов обнаружен {similar_section_detected_count} раз подряд - останавливаю прокрутку доски")
                        ignore_similar_section = False
                        break
                    else:
                        print("Обнаружен раздел похожих пинов - продолжаю прокрутку")
            else:
                similar_section_detected_count = 0  # Сбрасываем счетчик если раздел не обнаружен
                # Если собрано достаточно изображений, отключаем игнорирование
                if max_images and max_images > 0 and len(collected_image_data) >= max_images:
                    ignore_similar_section = False

            # Оптимизированная прокрутка
            # Если нужно больше изображений и обнаружен раздел похожих пинов, прокручиваем более агрессивно
            if ignore_similar_section and max_images and max_images > 0 and len(collected_image_data) < max_images:
                # Агрессивная прокрутка: несколько небольших прокруток для лучшей загрузки
                for _ in range(2):
                    self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(0.5)
                time.sleep(max(1.5, self.scroll_delay * 0.8))  # Увеличиваем задержку для загрузки
            else:
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                # Увеличиваем задержку для лучшей загрузки lazy-loaded изображений
                time.sleep(max(1.0, self.scroll_delay * 0.8))  # Минимум 1.0 сек для загрузки изображений

            # Проверяем, загрузился ли новый контент
            new_height = self.driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                no_new_content_count += 1
                # Увеличиваем количество попыток перед остановкой, чтобы дать больше времени на загрузку
                if no_new_content_count >= 3:  # Увеличено с 2 до 3
                    # Если есть ограничение по количеству и собрано недостаточно, продолжаем еще немного
                    if max_images and max_images > 0 and len(collected_image_data) < max_images:
                        end_of_board_retry_count += 1
                        # Ограничиваем количество попыток при достижении конца доски
                        if end_of_board_retry_count <= 5:  # Максимум 5 дополнительных попыток
                            print(f"Достигнут конец доски, но собрано только {len(collected_image_data)}/{max_images}, попытка {end_of_board_retry_count}/5...")
                            no_new_content_count = 1  # Сбрасываем счетчик для дополнительных попыток
                            # Делаем дополнительную прокрутку для загрузки изображений
                            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                            time.sleep(2.0)  # Дополнительная задержка
                            continue
                        else:
                            print(f"Достигнут конец доски, собрано {len(collected_image_data)}/{max_images} изображений. Больше изображений не найдено.")
                            break
                    print("Достигнут конец доски (нет нового контента)")
                    break
            else:
                no_new_content_count = 0  # Сбрасываем счетчик при появлении нового контента
                end_of_board_retry_count = 0  # Сбрасываем счетчик попыток при появлении нового контента

            last_height = new_height
            scroll_count += 1
            if max_images and max_images > 0:
                # Показываем количество собранных URL
                print(f"Прокрутка {scroll_count}/{max_scrolls} | Найдено: {len(collected_image_data)}/{max_images}")
            else:
                print(f"Прокрутка {scroll_count}/{max_scrolls}")

        # Прокручиваем в начало, чтобы собрать самые новые изображения
        if max_images and max_images > 0:
            print("Прокручиваю в начало доски для сбора самых новых изображений...")
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(3)  # Увеличена задержка для загрузки изображений

            # Прокручиваем постепенно вниз для загрузки lazy-loaded изображений
            for i in range(5):  # Увеличено количество итераций для лучшей загрузки
                scroll_pos = 800 * (i + 1)
                self.driver.execute_script(f"window.scrollTo(0, {scroll_pos});")
                time.sleep(1.0)  # Увеличена задержка для загрузки изображений

            # Возвращаемся в начало
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(3)  # Увеличена задержка для финальной загрузки

            # Финальный сбор изображений с позициями
            # Временно отключаем фильтрацию похожих пинов для финального сбора
            self._ignore_similar_section = True
            final_images_data = self.extract_image_urls_with_positions()
            self._ignore_similar_section = False
            seen_urls = {url for _, _, url in collected_image_data}
            for y, x, url in final_images_data:
                if url not in seen_urls:
                    collected_image_data.append((y, x, url))
                    seen_urls.add(url)

            # Сортируем по позиции (сверху вниз, слева направо)
            collected_image_data.sort(key=lambda item: (item[0], item[1]))

            print(f"Всего собрано уникальных URL во время прокрутки: {len(collected_image_data)}")

            # Если собрано недостаточно изображений, делаем дополнительную попытку извлечения
            if max_images and max_images > 0 and len(collected_image_data) < max_images:
                print(f"Собрано только {len(collected_image_data)}/{max_images}, делаю дополнительную попытку извлечения...")
                # Дополнительная прокрутка для загрузки изображений
                for i in range(3):
                    scroll_pos = 1000 * (i + 1)
                    self.driver.execute_script(f"window.scrollTo(0, {scroll_pos});")
                    time.sleep(1.5)
                self.driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(2)
                # Повторный сбор (с отключенной фильтрацией похожих пинов)
                self._ignore_similar_section = True
                additional_images_data = self.extract_image_urls_with_positions()
                self._ignore_similar_section = False
                seen_urls = {url for _, _, url in collected_image_data}
                for y, x, url in additional_images_data:
                    if url not in seen_urls:
                        collected_image_data.append((y, x, url))
                        seen_urls.add(url)
                # Пересортировка
                collected_image_data.sort(key=lambda item: (item[0], item[1]))
                print(f"После дополнительной попытки собрано: {len(collected_image_data)} изображений")

            # Сохраняем собранные данные для использования в extract_image_urls
            self._collected_image_data_during_scroll = collected_image_data

        print("Прокрутка завершена")

    def extract_image_urls_from_current_view(self):
        """
        Быстрое извлечение URL изображений с текущей видимой области страницы
        Используется для проверки количества во время прокрутки

        Returns:
            Список URL изображений
        """
        image_urls = set()

        try:
            # Используем поиск через пины для более точного результата
            selectors = [
                "[data-test-id='pin']",
                "[data-test-id='pinrep']",
                "div[data-test-id='pinWrapper']",
                "div[role='listitem']"
            ]

            for selector in selectors:
                try:
                    pin_elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for pin in pin_elements:
                        if self.is_in_similar_section(pin):
                            continue

                        img_elements = pin.find_elements(By.TAG_NAME, "img")
                        for img in img_elements:
                            if self.is_in_similar_section(img):
                                continue

                            src = (img.get_attribute('src') or
                                  img.get_attribute('data-src') or
                                  img.get_attribute('data-lazy-src') or
                                  img.get_attribute('data-pin-media'))

                            if src and 'pinimg.com' in src:
                                # Используем улучшенную проверку
                                if not self.is_valid_pin_image(img, src):
                                    continue

                                full_url = self.get_full_image_url(src, self.image_quality)
                                if full_url:
                                    image_urls.add(full_url)
                except:
                    continue

            # Также проверяем все изображения напрямую (на случай если пины не найдены)
            try:
                all_images = self.driver.find_elements(By.TAG_NAME, "img")
                for img in all_images:
                    if self.is_in_similar_section(img):
                        continue

                    src = (img.get_attribute('src') or
                          img.get_attribute('data-src') or
                          img.get_attribute('data-lazy-src') or
                          img.get_attribute('data-pin-media'))

                    if src and 'pinimg.com' in src:
                        if not self.is_valid_pin_image(img, src):
                            continue

                        full_url = self.get_full_image_url(src, self.image_quality)
                        if full_url:
                            image_urls.add(full_url)
            except:
                pass
        except:
            pass

        return list(image_urls)

    def extract_image_urls_with_positions(self):
        """
        Извлекает URL изображений с их позициями (y, x) для правильной сортировки
        Используется во время прокрутки для сохранения порядка

        Returns:
            Список кортежей (y, x, url)
        """
        image_data = []

        try:
            # Используем поиск через пины для более точного результата
            selectors = [
                "[data-test-id='pin']",
                "[data-test-id='pinrep']",
                "div[data-test-id='pinWrapper']",
                "div[role='listitem']"
            ]

            for selector in selectors:
                try:
                    pin_elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for pin in pin_elements:
                        if self.is_in_similar_section(pin):
                            continue

                        # Получаем позицию пина
                        try:
                            pin_location = pin.location
                            pin_y = pin_location.get('y', 0)
                            pin_x = pin_location.get('x', 0)
                        except:
                            pin_y = 999999
                            pin_x = 999999

                        img_elements = pin.find_elements(By.TAG_NAME, "img")
                        for img in img_elements:
                            if self.is_in_similar_section(img):
                                continue

                            src = (img.get_attribute('src') or
                                  img.get_attribute('data-src') or
                                  img.get_attribute('data-lazy-src') or
                                  img.get_attribute('data-pin-media'))

                            if src and 'pinimg.com' in src:
                                # Используем улучшенную проверку
                                if not self.is_valid_pin_image(img, src):
                                    continue

                                full_url = self.get_full_image_url(src, self.image_quality)
                                if full_url:
                                    image_data.append((pin_y, pin_x, full_url))
                except:
                    continue

            # Также проверяем все изображения напрямую (на случай если пины не найдены)
            try:
                all_images = self.driver.find_elements(By.TAG_NAME, "img")
                for img in all_images:
                    if self.is_in_similar_section(img):
                        continue

                    src = (img.get_attribute('src') or
                          img.get_attribute('data-src') or
                          img.get_attribute('data-lazy-src') or
                          img.get_attribute('data-pin-media'))

                    if src and 'pinimg.com' in src:
                        if not self.is_valid_pin_image(img, src):
                            continue

                        full_url = self.get_full_image_url(src, self.image_quality)
                        if full_url:
                            try:
                                location = img.location
                                y_pos = location.get('y', 0)
                                x_pos = location.get('x', 0)
                                image_data.append((y_pos, x_pos, full_url))
                            except:
                                image_data.append((999999, 999999, full_url))
            except:
                pass
        except:
            pass

        return image_data

    def is_in_similar_section(self, element):
        """
        Проверяет, находится ли элемент в разделе "Похожие пины"

        Args:
            element: WebElement для проверки

        Returns:
            True если элемент в разделе похожих пинов, False в противном случае
        """
        # Если установлен флаг игнорирования, не фильтруем элементы
        if hasattr(self, '_ignore_similar_section') and self._ignore_similar_section:
            return False

        try:
            # Ищем элемент с текстом "Показать похожие" или "Похожие пины"
            similar_texts = [
                "Показать похожие",
                "Похожие пины",
                "Similar ideas",
                "Show more like this",
                "More like this",
                "Similar pins"
            ]

            # Получаем позицию элемента
            try:
                element_y = element.location['y']
            except:
                # Если не можем получить позицию, не фильтруем элемент
                return False

            # Ищем разделитель похожих пинов
            for text in similar_texts:
                try:
                    # Ищем элементы с этим текстом
                    separators = self.driver.find_elements(By.XPATH, f"//*[contains(text(), '{text}')]")
                    for separator in separators:
                        if separator.is_displayed():
                            try:
                                separator_y = separator.location['y']
                                # Увеличиваем порог с 500px до 1000px для менее агрессивной фильтрации
                                # Это позволяет собирать больше изображений, которые могут быть валидными
                                if element_y > separator_y + 1000:
                                    # Дополнительная проверка: проверяем, действительно ли элемент в контейнере похожих пинов
                                    try:
                                        # Ищем родительский контейнер с признаками раздела похожих пинов
                                        parent = element.find_element(By.XPATH, "./ancestor::*[contains(@class, 'similar') or contains(@class, 'related')]")
                                        if parent:
                                            return True
                                    except:
                                        # Если не нашли контейнер, используем только позиционную проверку
                                        pass
                                    return True
                            except:
                                continue
                except:
                    continue

            return False
        except:
            # В случае любой ошибки не фильтруем элемент
            return False

    def is_valid_pin_image(self, img_element, src):
        """
        Проверяет, является ли изображение валидным пином (не аватарка, не иконка и т.д.)

        Args:
            img_element: WebElement изображения
            src: URL изображения

        Returns:
            True если это валидное изображение пина, False в противном случае
        """
        if not src or 'pinimg.com' not in src:
            return False

        # Проверка по URL - исключаем аватарки, иконки, логотипы
        src_lower = src.lower()
        skip_patterns = ['avatar', 'logo', 'icon', 'profile', 'user', 'account',
                        'favicon', 'button', 'badge', 'emoji', 'reaction']

        if any(pattern in src_lower for pattern in skip_patterns):
            return False

        # Проверка размера изображения на странице
        try:
            size = img_element.size
            width = size.get('width', 0)
            height = size.get('height', 0)

            # Аватарки обычно маленькие (менее 50x50) - ослабляем проверку
            if width < 50 or height < 50:
                return False

            # Проверка позиции - аватарки обычно вверху страницы
            # Убираем эту проверку, так как она может отфильтровывать валидные изображения в начале доски
            # location = img_element.location
            # y_pos = location.get('y', 0)
            # if y_pos < 200 and (width < 150 or height < 150):
            #     return False

        except:
            pass

        # Проверка по родительским элементам - ищем признаки пина
        has_pin_parent = False
        try:
            parent = img_element.find_element(By.XPATH, "./ancestor::*[contains(@class, 'pin') or contains(@data-test-id, 'pin')]")
            # Если нашли родителя с признаками пина, это валидно
            has_pin_parent = True
        except:
            pass

        # Если нашли родителя-пина, это точно валидно
        if has_pin_parent:
            return True

        # Если не нашли родителя-пина, проверяем другие признаки
        # Проверяем размеры в URL - пины обычно имеют размеры 564x, 736x, originals
        pin_sizes = ['/564x/', '/736x/', '/originals/', '/474x/', '/750x/', '/1200x/', '/1400x/', '/236x/']
        if any(size in src for size in pin_sizes):
            # Если URL содержит правильный размер, считаем валидным (ослабляем проверку размера)
            try:
                size = img_element.size
                width = size.get('width', 0)
                height = size.get('height', 0)
                # Ослабляем проверку - если размер больше 50px или не можем проверить, считаем валидным
                if width >= 50 or height >= 50:
                    return True
                # Если размер меньше 50, но URL правильный, все равно считаем валидным
                return True
            except:
                return True  # Если не можем проверить размер, но URL правильный, считаем валидным

        # Если URL содержит pinimg.com и не содержит паттернов для пропуска, считаем валидным
        # Это более мягкая проверка для случаев, когда другие проверки не сработали
        return True

    def extract_image_urls(self, max_images=None):
        """
        Извлекает URL всех изображений со страницы в правильном порядке (сверху вниз, слева направо)
        Исключает изображения из раздела "Похожие пины" и аватарки

        Args:
            max_images: Максимальное количество изображений для возврата (None = все)

        Returns:
            Список URL изображений в правильном порядке
        """
        # Если есть данные, собранные во время прокрутки, используем их как основной источник
        if hasattr(self, '_collected_image_data_during_scroll') and self._collected_image_data_during_scroll:
            print("Использую изображения, собранные во время прокрутки...")
            # Извлекаем только URL из собранных данных (они уже отсортированы)
            image_urls = [url for _, _, url in self._collected_image_data_during_scroll]
            original_count = len(image_urls)

            # Ограничиваем до max_images, если указано
            if max_images and max_images > 0:
                if original_count > max_images:
                    image_urls = image_urls[:max_images]
                    print(f"Найдено {original_count} изображений, выбрано {len(image_urls)} самых новых (по запросу)")
                else:
                    print(f"Найдено {len(image_urls)} уникальных изображений в правильном порядке")
            else:
                print(f"Найдено {len(image_urls)} уникальных изображений в правильном порядке")

            # Очищаем временную переменную
            delattr(self, '_collected_image_data_during_scroll')
            return image_urls

        # Если данных нет, собираем изображения стандартным способом
        image_data = []  # Список кортежей (y, x, url) для сортировки

        # Ждем загрузки контента
        time.sleep(3)

        print("Извлекаю URL изображений...")

        # Дополнительная прокрутка для загрузки всех изображений
        # Прокручиваем понемногу вниз и обратно для загрузки lazy-loaded изображений
        print("Загружаю все изображения на странице...")

        # Прокручиваем постепенно вниз с небольшими возвратами для триггера загрузки
        for i in range(8):
            scroll_amount = 600 * (i + 1)
            self.driver.execute_script(f"window.scrollTo(0, {scroll_amount});")
            time.sleep(0.8)
            # Небольшой возврат для триггера lazy loading
            if i > 0:
                self.driver.execute_script(f"window.scrollTo(0, {scroll_amount - 300});")
                time.sleep(0.5)
                self.driver.execute_script(f"window.scrollTo(0, {scroll_amount});")
                time.sleep(0.8)

        # Возвращаемся в начало для правильного порядка
        self.driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(3)  # Увеличиваем задержку для финальной загрузки

        # Финальная прокрутка для гарантии загрузки всех изображений в начале
        for i in range(3):
            self.driver.execute_script(f"window.scrollTo(0, {400 * (i + 1)});")
            time.sleep(1)

        self.driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(2)

        # Метод 1: Поиск через Selenium для динамически загруженных элементов
        # Это более надежный метод для Pinterest
        try:
            # Ждем появления пинов на странице
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "img"))
            )

            # Получаем все изображения
            images = self.driver.find_elements(By.TAG_NAME, "img")
            print(f"Найдено {len(images)} элементов img")

            for img in images:
                # Проверяем, не находится ли изображение в разделе похожих пинов
                if self.is_in_similar_section(img):
                    continue

                # Пробуем разные атрибуты
                src = (img.get_attribute('src') or
                      img.get_attribute('data-src') or
                      img.get_attribute('data-lazy-src') or
                      img.get_attribute('data-pin-media'))

                if src and 'pinimg.com' in src:
                    # Проверяем, является ли это валидным пином
                    if not self.is_valid_pin_image(img, src):
                        continue

                    full_url = self.get_full_image_url(src, self.image_quality)
                    if full_url:
                        # Получаем позицию для сортировки
                        try:
                            location = img.location
                            y_pos = location.get('y', 0)
                            x_pos = location.get('x', 0)
                            image_data.append((y_pos, x_pos, full_url))
                        except:
                            # Если не удалось получить позицию, добавляем в конец
                            image_data.append((999999, 999999, full_url))
                elif src:
                    # Логируем изображения без pinimg.com для диагностики (только первые несколько)
                    if len([d for d in image_data if d]) < 5:  # Логируем только если найдено мало изображений
                        pass  # Можно добавить логирование если нужно
        except Exception as e:
            print(f"Ошибка при поиске изображений через Selenium: {e}")

        # Метод 2: Поиск через пины Pinterest (более надежный метод)
        try:
            # Различные селекторы для пинов Pinterest
            selectors = [
                "[data-test-id='pin']",
                "[data-test-id='pinrep']",
                "div[data-test-id='pinWrapper']",
                "div[role='listitem']"
            ]

            for selector in selectors:
                try:
                    pin_elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    print(f"Найдено {len(pin_elements)} элементов с селектором {selector}")

                    for pin in pin_elements:
                        # Проверяем, не находится ли пин в разделе похожих
                        if self.is_in_similar_section(pin):
                            continue

                        # Получаем позицию пина для сортировки
                        try:
                            pin_location = pin.location
                            pin_y = pin_location.get('y', 0)
                            pin_x = pin_location.get('x', 0)
                        except:
                            pin_y = 999999
                            pin_x = 999999

                        img_elements = pin.find_elements(By.TAG_NAME, "img")
                        for img in img_elements:
                            # Дополнительная проверка для изображения
                            if self.is_in_similar_section(img):
                                continue

                            src = (img.get_attribute('src') or
                                  img.get_attribute('data-src') or
                                  img.get_attribute('data-lazy-src') or
                                  img.get_attribute('data-pin-media'))

                            if src and 'pinimg.com' in src:
                                # Используем улучшенную проверку
                                if not self.is_valid_pin_image(img, src):
                                    continue

                                full_url = self.get_full_image_url(src, self.image_quality)
                                if full_url:
                                    # Используем позицию пина для сортировки
                                    image_data.append((pin_y, pin_x, full_url))
                except Exception as e:
                    print(f"Ошибка с селектором {selector}: {e}")
                    continue
        except Exception as e:
            print(f"Ошибка при поиске через data-атрибуты: {e}")

        # Удаляем дубликаты, сохраняя порядок
        seen_urls = set()
        unique_image_data = []
        for y, x, url in image_data:
            if url not in seen_urls:
                seen_urls.add(url)
                unique_image_data.append((y, x, url))

        # Сортируем по Y (сверху вниз), затем по X (слева направо)
        unique_image_data.sort(key=lambda item: (item[0], item[1]))

        # Извлекаем только URL в правильном порядке
        image_urls = [url for _, _, url in unique_image_data]

        print(f"Найдено {len(image_urls)} уникальных изображений в правильном порядке")
        return image_urls

    def download_image(self, url, filename, use_session=True):
        """
        Скачивает изображение по URL

        Args:
            url: URL изображения
            filename: Имя файла для сохранения
            use_session: Использовать переиспользуемую сессию (по умолчанию True)

        Returns:
            True если успешно, False в противном случае
        """
        filepath = os.path.join(self.download_folder, filename)

        # Список методов для попытки скачивания
        methods = [
            # Метод 1: Полные заголовки с правильным Referer
            {
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Referer': 'https://www.pinterest.com/',
                    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Sec-Fetch-Dest': 'image',
                    'Sec-Fetch-Mode': 'no-cors',
                    'Sec-Fetch-Site': 'cross-site',
                    'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                    'Sec-Ch-Ua-Mobile': '?0',
                    'Sec-Ch-Ua-Platform': '"Windows"',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'DNT': '1'
                },
                'url': url
            },
            # Метод 2: Попробуем изменить URL - заменить /originals/ на /736x/ или /564x/
            {
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Referer': 'https://www.pinterest.com/',
                    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Sec-Fetch-Dest': 'image',
                    'Sec-Fetch-Mode': 'no-cors',
                    'Sec-Fetch-Site': 'cross-site'
                },
                'url': url.replace('/originals/', '/736x/') if '/originals/' in url else url
            },
            # Метод 3: Попробуем другой размер
            {
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Referer': 'https://www.pinterest.com/',
                    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Sec-Fetch-Dest': 'image',
                    'Sec-Fetch-Mode': 'no-cors',
                    'Sec-Fetch-Site': 'cross-site'
                },
                'url': url.replace('/originals/', '/564x/') if '/originals/' in url else url
            },
            # Метод 4: Простые заголовки
            {
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Referer': 'https://www.pinterest.com/'
                },
                'url': url
            },
            # Метод 5: Без Referer, но с полным User-Agent
            {
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9'
                },
                'url': url
            },
            # Метод 6: Убрать параметры из URL
            {
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Referer': 'https://www.pinterest.com/',
                    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
                },
                'url': url.split('?')[0] if '?' in url else url
            },
            # Метод 7: Использовать urllib вместо requests (обход некоторых блокировок)
            {
                'method': 'urllib',
                'url': url
            },
            # Метод 8: Использовать Selenium для прямого скачивания (последний метод)
            {
                'method': 'selenium',
                'url': url
            }
        ]

        # Пробуем каждый метод по очереди
        for i, method in enumerate(methods, 1):
            try:
                # Небольшая задержка между попытками (кроме первой)
                if i > 1:
                    time.sleep(0.2)

                if method.get('method') == 'urllib':
                    # Используем urllib
                    import urllib.request
                    import urllib.error

                    req = urllib.request.Request(method['url'])
                    req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
                    req.add_header('Referer', 'https://www.pinterest.com/')
                    req.add_header('Accept', 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8')
                    req.add_header('Accept-Language', 'en-US,en;q=0.9')

                    try:
                        with urllib.request.urlopen(req, timeout=30) as response:
                            if response.status == 200:
                                with open(filepath, 'wb') as f:
                                    f.write(response.read())
                                print(f"✓ Успешно скачано методом {i} (urllib): {filename}")
                                return True
                    except urllib.error.HTTPError as e:
                        if e.code == 403:
                            continue
                        raise
                elif method.get('method') == 'selenium':
                    # Используем Selenium для прямого скачивания через JavaScript (последний метод)
                    if not self.driver:
                        continue

                    try:
                        # Используем JavaScript для получения изображения как base64
                        js_code = f"""
                        var img = new Image();
                        img.crossOrigin = 'anonymous';
                        img.src = '{method['url']}';
                        return new Promise((resolve, reject) => {{
                            img.onload = function() {{
                                var canvas = document.createElement('canvas');
                                canvas.width = img.width;
                                canvas.height = img.height;
                                var ctx = canvas.getContext('2d');
                                ctx.drawImage(img, 0, 0);
                                try {{
                                    var dataURL = canvas.toDataURL('image/jpeg');
                                    resolve(dataURL);
                                }} catch(e) {{
                                    reject(e);
                                }}
                            }};
                            img.onerror = reject;
                            setTimeout(() => reject(new Error('Timeout')), 30000);
                        }});
                        """

                        # Пробуем получить изображение через JavaScript
                        # Но это может не сработать из-за CORS, поэтому используем альтернативный метод
                        # Скачиваем через requests с cookies из браузера
                        browser_cookies = self.get_browser_cookies()
                        if browser_cookies:
                            session = requests.Session()
                            session.headers.update({
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                                'Referer': 'https://www.pinterest.com/',
                                'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
                            })
                            session.cookies.update(browser_cookies)
                            response = session.get(method['url'], timeout=30, stream=True, allow_redirects=True)
                            response.raise_for_status()

                            with open(filepath, 'wb') as f:
                                for chunk in response.iter_content(chunk_size=8192):
                                    if chunk:
                                        f.write(chunk)
                            print(f"✓ Успешно скачано методом {i} (selenium cookies): {filename}")
                            return True
                    except:
                        continue
                else:
                    # Используем requests
                    if use_session and i == 1:
                        # Для первого метода используем переиспользуемую сессию
                        session = self.init_session()
                        response = session.get(method['url'], timeout=30, stream=True, allow_redirects=True)
                    else:
                        # Для остальных методов создаем новую сессию с нужными заголовками
                        session = requests.Session()
                        session.headers.update(method['headers'])
                        # Пробуем добавить cookies из браузера, если он открыт
                        try:
                            browser_cookies = self.get_browser_cookies()
                            if browser_cookies:
                                session.cookies.update(browser_cookies)
                        except:
                            pass
                        response = session.get(method['url'], timeout=30, stream=True, allow_redirects=True)

                    response.raise_for_status()

                    with open(filepath, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

                    if i > 1:  # Логируем только если использован не первый метод
                        print(f"✓ Успешно скачано методом {i}: {filename}")
                    return True
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 403:
                    # Продолжаем пробовать следующий метод
                    continue
                else:
                    # Другая HTTP ошибка - пробуем следующий метод
                    continue
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                # Ошибка urllib - пробуем следующий метод
                continue
            except Exception as e:
                # Любая другая ошибка - пробуем следующий метод
                continue

        # Если все методы не сработали
        short_url = url[:80] + "..." if len(url) > 80 else url
        print(f"✗ Ошибка при скачивании {short_url} (все {len(methods)} методов не сработали)")
        return False

    def get_filename_from_url(self, url, index, filename_template=None):
        """
        Генерирует имя файла из URL

        Args:
            url: URL изображения
            index: Индекс изображения
            filename_template: Шаблон имени файла (опционально)
                Поддерживаемые переменные:
                - {index} - номер изображения
                - {index04} - номер с ведущими нулями (0001, 0002, ...)
                - {hash} - хэш URL (первые 8 символов)
                - {url_hash} - полный хэш URL

        Returns:
            Имя файла
        """
        # Если передан шаблон, используем его
        if filename_template:
            # Вычисляем хэш URL
            url_hash = hashlib.md5(url.encode()).hexdigest()
            url_hash_short = url_hash[:8]

            # Заменяем переменные в шаблоне
            filename = filename_template
            filename = filename.replace('{index}', str(index))
            filename = filename.replace('{index04}', f"{index:04d}")
            filename = filename.replace('{hash}', url_hash_short)
            filename = filename.replace('{url_hash}', url_hash)

            # Очищаем имя файла от недопустимых символов для Windows
            filename = re.sub(r'[<>:"/\\|?*]', '_', filename)

            # Если нет расширения, добавляем .jpg
            if '.' not in os.path.splitext(filename)[1]:
                filename += '.jpg'

            return filename

        # Стандартная логика без шаблона
        parsed = urlparse(url)
        path = parsed.path

        # Пытаемся извлечь имя файла из URL
        filename = os.path.basename(path)

        # Если имя файла не найдено или слишком короткое, генерируем
        if not filename or len(filename) < 5:
            # Используем хэш URL или индекс
            filename = f"pin_{index:04d}.jpg"
        else:
            # Убираем параметры из имени файла
            filename = filename.split('?')[0]
            # Если нет расширения, добавляем .jpg
            if '.' not in filename:
                filename += '.jpg'

        # Очищаем имя файла от недопустимых символов для Windows
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)

        return filename

    def expand_short_url(self, url):
        """
        Преобразует короткую ссылку pin.it в полную ссылку Pinterest

        Args:
            url: Короткая или полная ссылка Pinterest

        Returns:
            Полная ссылка Pinterest
        """
        # Проверяем, является ли это короткой ссылкой pin.it
        if 'pin.it' in url.lower() and 'pinterest.com' not in url.lower():
            try:
                print(f"Обнаружена короткая ссылка: {url}")
                print("Преобразую в полную ссылку...")

                # Следуем редиректу для получения полного URL
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                }

                response = requests.get(url, headers=headers, allow_redirects=True, timeout=10)
                final_url = response.url

                print(f"Полная ссылка: {final_url}")
                return final_url
            except Exception as e:
                print(f"Ошибка при преобразовании короткой ссылки: {e}")
                print("Пытаюсь использовать исходную ссылку...")
                return url

        return url

    def get_board_name_from_url(self, url):
        """
        Извлекает название доски из URL

        Args:
            url: URL доски Pinterest

        Returns:
            Название доски или None
        """
        try:
            # Декодируем URL-encoded строки в URL (для русских и других не-ASCII символов)
            # Декодируем только путь, сохраняя структуру URL
            from urllib.parse import urlparse as parse_url
            parsed = parse_url(url)
            # Декодируем путь
            decoded_path = unquote(parsed.path)
            # Собираем URL обратно с декодированным путем
            url = f"{parsed.scheme}://{parsed.netloc}{decoded_path}"

            # Убираем параметры запроса и якоря
            url = url.split('?')[0].split('#')[0].rstrip('/')

            # Проверяем различные форматы URL Pinterest
            # Формат 1: https://www.pinterest.com/username/board-name/
            # Формат 2: https://www.pinterest.com/pin/... (это пин, не доска)
            # Формат 3: https://www.pinterest.com/username/board-name/pin/... (пин внутри доски)

            parts = url.split('/')

            # Ищем индекс pinterest.com
            pinterest_idx = -1
            for i, part in enumerate(parts):
                if 'pinterest.com' in part.lower():
                    pinterest_idx = i
                    break

            if pinterest_idx == -1:
                return None

            # После pinterest.com должен быть username, затем board-name
            if len(parts) > pinterest_idx + 2:
                username = parts[pinterest_idx + 1]
                board_name = parts[pinterest_idx + 2]

                # Если следующий сегмент - "pin", значит это пин внутри доски, название доски уже есть
                # Если board_name пустой или это "pin", пропускаем
                if board_name and board_name.lower() != 'pin' and board_name.lower() != 'board':
                    # Декодируем URL-encoded строку (для русских и других не-ASCII символов)
                    board_name = unquote(board_name)
                    # Убираем параметры если есть
                    board_name = board_name.split('?')[0].split('#')[0]
                    # Очищаем от недопустимых символов для имени папки
                    board_name = re.sub(r'[<>:"/\\|?*]', '_', board_name)
                    # Убираем лишние подчеркивания
                    board_name = re.sub(r'_+', '_', board_name).strip('_')
                    if board_name and len(board_name) > 0:
                        return board_name

            # Альтернативный вариант: если URL содержит /board/ или /boards/
            for i, part in enumerate(parts):
                if part.lower() in ['board', 'boards'] and i + 1 < len(parts):
                    board_name = parts[i + 1]
                    # Декодируем URL-encoded строку (для русских и других не-ASCII символов)
                    board_name = unquote(board_name)
                    board_name = board_name.split('?')[0].split('#')[0]
                    board_name = re.sub(r'[<>:"/\\|?*]', '_', board_name)
                    board_name = re.sub(r'_+', '_', board_name).strip('_')
                    if board_name and len(board_name) > 0:
                        return board_name

            # Если ничего не найдено, пробуем взять последний значимый сегмент
            if len(parts) >= 2:
                last_part = parts[-1]
                if last_part and last_part.lower() not in ['pin', 'board', 'boards', '']:
                    board_name = last_part
                    # Декодируем URL-encoded строку (для русских и других не-ASCII символов)
                    board_name = unquote(board_name)
                    board_name = board_name.split('?')[0].split('#')[0]
                    board_name = re.sub(r'[<>:"/\\|?*]', '_', board_name)
                    board_name = re.sub(r'_+', '_', board_name).strip('_')
                    if board_name and len(board_name) > 0:
                        return board_name

        except Exception as e:
            print(f"Ошибка при извлечении названия доски: {e}")
            pass
        return None

    def parse_pinterest_url(self, url, max_images=None, auto_subfolder=True):
        """
        Основной метод для парсинга Pinterest URL

        Args:
            url: URL доски или страницы Pinterest (поддерживает короткие ссылки pin.it)
            max_images: Максимальное количество изображений для скачивания (None = все)
            auto_subfolder: Автоматически создавать подпапку по названию доски
        """
        # Преобразуем короткую ссылку в полную, если необходимо
        url = self.expand_short_url(url)

        # Автоматическое создание подпапки по названию доски
        original_folder = self.download_folder
        if auto_subfolder:
            board_name = self.get_board_name_from_url(url)
            if board_name:
                self.download_folder = os.path.join(original_folder, board_name)
                self.setup_download_folder()
                print(f"Создана подпапка: {self.download_folder}")

        try:
            if not self.driver:
                self.init_driver()
        except Exception as e:
            print(f"\nКритическая ошибка: {e}")
            return

        print(f"Открываю страницу: {url}")
        try:
            self.driver.get(url)
        except Exception as e:
            print(f"Ошибка при открытии страницы: {e}")
            return

        # Ждем загрузки страницы
        time.sleep(5)

        # Прокручиваем страницу для загрузки изображений
        # Если указано ограничение, прокручиваем только до нужного количества
        self.scroll_and_load_images(max_images=max_images)

        # Извлекаем URL изображений (с ограничением max_images, если указано)
        image_urls = self.extract_image_urls(max_images=max_images)

        if not image_urls:
            print("Не найдено изображений на странице")
            return

        # Если указано ограничение и собрано меньше, чем нужно, пробуем еще раз
        if max_images and max_images > 0:
            if len(image_urls) < max_images:
                print(f"Внимание: найдено только {len(image_urls)} изображений из запрошенных {max_images}")
                print("Попытка собрать больше изображений...")
                # Пробуем еще раз прокрутить и собрать
                self.driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(3)

                # Оптимизированная прокрутка для загрузки всех изображений
                for i in range(5):  # Уменьшено с 8 до 5 итераций
                    scroll_pos = 800 * (i + 1)  # Увеличено расстояние для меньшего количества итераций
                    self.driver.execute_script(f"window.scrollTo(0, {scroll_pos});")
                    time.sleep(0.8)  # Уменьшена задержка с 1.2 до 0.8

                # Возвращаемся в начало
                self.driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(3)

                # Повторное извлечение (без ограничения для сбора всех доступных)
                additional_urls = self.extract_image_urls(max_images=None)
                # Объединяем и удаляем дубликаты, сохраняя порядок
                seen = set(image_urls)
                all_urls = list(image_urls)  # Начинаем с уже собранных
                for url in additional_urls:
                    if url not in seen:
                        seen.add(url)
                        all_urls.append(url)

                # Ограничиваем до max_images
                image_urls = all_urls[:max_images] if len(all_urls) >= max_images else all_urls
                print(f"После повторного сбора: найдено {len(image_urls)} изображений")

        print(f"\nНачинаю скачивание {len(image_urls)} изображений...")

        # Инициализируем сессию для переиспользования
        self.init_session()

        downloaded = 0
        failed = 0
        skipped = 0

        # Подготавливаем список задач для скачивания
        download_tasks = []
        for index, img_url in enumerate(image_urls, 1):
            filename = self.get_filename_from_url(img_url, index)
            filepath = os.path.join(self.download_folder, filename)

            # Проверяем, не скачано ли уже это изображение
            if os.path.exists(filepath):
                skipped += 1
                print(f"[{index}/{len(image_urls)}] Пропущено (уже существует): {filename}")
                continue

            download_tasks.append((index, img_url, filename))

        # Параллельное скачивание с использованием ThreadPoolExecutor
        if download_tasks:
            print(f"Скачиваю {len(download_tasks)} изображений параллельно (до {self.max_workers} потоков)...")

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # Запускаем задачи
                future_to_task = {
                    executor.submit(self.download_image, img_url, filename): (index, filename, img_url)
                    for index, img_url, filename in download_tasks
                }

                # Обрабатываем результаты по мере завершения
                for future in as_completed(future_to_task):
                    index, filename, img_url = future_to_task[future]
                    try:
                        success = future.result()
                        if success:
                            downloaded += 1
                            print(f"[{index}/{len(image_urls)}] ✓ Успешно скачано: {filename}")
                        else:
                            failed += 1
                            print(f"[{index}/{len(image_urls)}] ✗ Ошибка скачивания: {filename}")
                    except Exception as e:
                        failed += 1
                        print(f"[{index}/{len(image_urls)}] ✗ Исключение при скачивании {filename}: {e}")

        print(f"\n{'='*50}")
        print(f"Скачивание завершено!")
        print(f"Успешно: {downloaded}")
        print(f"Ошибок: {failed}")
        print(f"Пропущено: {skipped}")
        print(f"Всего: {len(image_urls)}")
        print(f"Папка: {os.path.abspath(self.download_folder)}")
        print(f"{'='*50}")

    def close(self):
        """Закрывает браузер и очищает ресурсы"""
        if self.driver:
            self.driver.quit()
            print("Браузер закрыт")

        # Закрываем сессию requests
        if self.session:
            self.session.close()
            self.session = None


def main():
    """Основная функция для запуска парсера"""
    print("="*50)
    print("Парсер Pinterest для скачивания изображений")
    print("="*50)

    # Получаем URL от пользователя
    url = input("\nВведите URL доски или страницы Pinterest: ").strip()

    if not url:
        print("URL не указан!")
        return

    # Проверяем, что это Pinterest URL
    if 'pinterest.com' not in url.lower():
        print("Предупреждение: URL не похож на Pinterest ссылку")
        response = input("Продолжить? (y/n): ")
        if response.lower() != 'y':
            return

    # Получаем имя папки для скачивания
    folder_name = input("Введите имя папки для сохранения (Enter для 'pinterest_images'): ").strip()
    if not folder_name:
        folder_name = "pinterest_images"

    # Создаем парсер
    parser = PinterestParser(download_folder=folder_name)

    try:
        # Парсим страницу
        parser.parse_pinterest_url(url)
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем")
    except Exception as e:
        print(f"\nОшибка: {e}")
    finally:
        parser.close()


if __name__ == "__main__":
    main()
