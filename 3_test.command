#!/bin/bash
# Пробный прогон полного конвейера: поиск + лента хэштега, 5 результатов на источник,
# 10 комментариев + реплаи, слайды каруселей + расшифровка через API.
cd "$(dirname "$0")"
source .venv/bin/activate
rm -rf __pycache__
python -u collect_tiktok_search_threads.py --query "умскул" --hashtag umschool --search-count 5 --comments 10 2>&1 | tee test_run.log
echo
echo "=== Таблицы: cases/tiktok_search_threads/search/search_posts_*.xlsx (посты, с текстом слайдов) и comments_*.xlsx"
read -p "Нажмите Enter, чтобы закрыть"
