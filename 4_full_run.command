#!/bin/bash
# Полная выгрузка: запросы из queries.txt + ленты хэштегов из hashtags.txt,
# до 200 результатов на источник, до 200 комментариев на пост + реплаи,
# слайды каруселей + расшифровка через API. Для другой школы — поменяйте queries.txt и hashtags.txt.
cd "$(dirname "$0")"
source .venv/bin/activate
rm -rf __pycache__
LOG="run_$(date +%Y%m%d_%H%M).log"
echo "=== Запросы:"; cat queries.txt; echo "=== Хэштеги:"; cat hashtags.txt; echo
python collect_tiktok_search_threads.py --queries-file queries.txt --hashtags-file hashtags.txt --search-count 200 --comments 200 --fetch-author 2>&1 | tee "$LOG"
echo
echo "=== Готово. Таблицы: cases/tiktok_search_threads/search/search_posts_*.xlsx и comments_*.xlsx"
read -p "Нажмите Enter, чтобы закрыть"
