#!/bin/bash
# Общие таблицы после параллельного сбора: посты (из последнего поиска) + комментарии/реплаи по всем постам
cd "$(dirname "$0")"
source .venv/bin/activate
python export_comments_table.py --all cases/tiktok_search_threads
echo
echo "=== Таблица постов: $(ls -t cases/tiktok_search_threads/search/search_posts_*.xlsx | head -1)"
echo "=== Таблица комментариев: cases/tiktok_search_threads/search/comments_all.xlsx"
python - <<'PY'
import json,glob
ok=err=0
for s in glob.glob('cases/tiktok_search_threads/raw/posts/*/thread_*_summary.json'):
    d=json.load(open(s)); ok+= d.get('status')=='success'; err+= d.get('status')!='success'
print(f"=== Постов собрано успешно: {ok}, с ошибкой: {err}")
PY
read -p "Нажмите Enter, чтобы закрыть"
