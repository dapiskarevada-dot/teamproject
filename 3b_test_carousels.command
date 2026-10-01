#!/bin/bash
# Проверка шага «карусели»: лента #umschool (150 постов), без комментариев.
# Фото-посты → слайды → расшифровка через API → колонки «Текст со слайдов» в таблице постов.
cd "$(dirname "$0")"
source .venv/bin/activate
rm -rf __pycache__
python -u collect_tiktok_search_threads.py --hashtag umschool --hashtag умскул --search-count 150 --search-only 2>&1 | tee test_carousels.log
echo
echo "=== Таблица постов: $(ls -t cases/tiktok_search_threads/search/search_posts_*.xlsx | head -1)"
read -p "Нажмите Enter, чтобы закрыть"
