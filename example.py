"""
Пример использования Pinterest парсера
"""

from pinterest_parser import PinterestParser

# Пример 1: Скачивание с доски
def example_board():
    parser = PinterestParser(download_folder="my_pinterest_images")

    url = "https://www.pinterest.com/username/board-name/"
    parser.parse_pinterest_url(url)
    parser.close()

# Пример 2: Скачивание со страницы пользователя
def example_user_page():
    parser = PinterestParser(download_folder="user_images")

    url = "https://www.pinterest.com/username/"
    parser.parse_pinterest_url(url)
    parser.close()

if __name__ == "__main__":
    # Раскомментируйте нужный пример:
    # example_board()
    # example_user_page()

    print("Используйте pinterest_parser.py для интерактивного режима")
    print("Или импортируйте PinterestParser в свой скрипт")
