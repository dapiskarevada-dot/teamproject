#!/bin/bash
# Два датасета в CSV: ШКОЛЫ (перепись + новые школы с Мака) и ОБЩИЕ_ЕГЭ — посты и комментарии отдельно -> папка ДАТАСЕТЫ_CSV/
# Если в server/ лежит all_comments_result.tgz (комментарии ко всем постам с сервера) — распакуется сам.
cd "$(dirname "$0")"; source .venv/bin/activate
if [ -f server/all_comments_result.tgz ] && [ ! -f server/out_all/КОММЕНТАРИИ.csv ]; then
  echo "Распаковываю all_comments_result.tgz…"; tar xzf server/all_comments_result.tgz -C server
fi
[ -f server/out_all/КОММЕНТАРИИ.csv ] || echo "!!! Нет server/all_comments_result.tgz — комментарии досбора (20 797 постов) не войдут. Скачайте его с сервера."
python -c "import pandas, openpyxl" 2>/dev/null || pip install -q pandas openpyxl
python -u server/build_csv_datasets.py
open ДАТАСЕТЫ_CSV 2>/dev/null; echo; read -p "Нажмите Enter, чтобы закрыть"
