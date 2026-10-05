#!/bin/bash
# Общая (discovery) выборка + транскрипты с сервера -> ОБЩИЕ_ЕГЭ_с_транскриптами.xlsx
# Нужны в папке server/: all_posts_dedup.csv и transcripts_general.jsonl (или .gz / out.tgz).
cd "$(dirname "$0")"; source .venv/bin/activate
CSV=server/all_posts_dedup.csv
[ -f "$CSV" ] || { echo "Нет $CSV"; read -p Enter; exit 1; }
# свежий .gz с сервера всегда распаковываем поверх старого .jsonl
G=server/transcripts_general.jsonl
if [ -f "$G.gz" ] && { [ ! -f "$G" ] || [ "$G.gz" -nt "$G" ]; }; then gunzip -c "$G.gz" > "$G"; echo "Распакован свежий $G.gz"; fi
TR=""
for f in "$G" server/transcripts_now.tgz server/out.tgz; do [ -f "$f" ] && { TR="$f"; break; }; done
[ -n "$TR" ] || { echo "Нет файла транскриптов в server/ (transcripts_general.jsonl.gz / out.tgz)"; read -p Enter; exit 1; }
[ -f server/screen_text.jsonl ] && echo "Текст с картинок: server/screen_text.jsonl"
python -c "import pandas, openpyxl" 2>/dev/null || pip install -q pandas openpyxl
python server/merge_general.py "$CSV" "$TR" "ОБЩИЕ_ЕГЭ_с_транскриптами.xlsx"
echo; read -p "Нажмите Enter, чтобы закрыть"
