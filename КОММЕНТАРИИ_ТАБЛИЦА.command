#!/bin/bash
# Все комментарии (сервер + Мак) в одну таблицу с данными поста -> КОММЕНТАРИИ_ВСЕ.csv / .xlsx
# Нужно: server/comments_and_labels.tgz (с сервера) — распакуется сам в server/comments_server/.
cd "$(dirname "$0")"; source .venv/bin/activate
if [ -f server/comments_and_labels.tgz ] && [ ! -f server/comments_server/КОММЕНТАРИИ_сервер.csv ]; then
  mkdir -p server/comments_server && tar xzf server/comments_and_labels.tgz -C server/comments_server && echo "Распакован comments_and_labels.tgz"
fi
python -c "import pandas, openpyxl, xlsxwriter" 2>/dev/null || pip install -q pandas openpyxl xlsxwriter
python server/merge_comments.py
echo; read -p "Готово. Нажмите Enter, чтобы закрыть"
