#!/bin/bash
# Текст на экране роликов БЕЗ РЕЧИ из постов с Маков (new/mac_posts.csv) через Gemini -> new/out/screen_text.jsonl,
# потом пересобирает таблицу new/МАКИ_посты.csv и архив new_schools_result.tgz.
# Ждёт, пока закончится текущий new_schools.py (если идёт). Повторный запуск продолжает с места.
# Порядок: сначала самые просматриваемые ролики. Доп. параметры передаются в server_screen_ocr.py:
#   bash run_screen_ocr.sh --dry-run --model google/gemini-2.5-flash-lite --max-frames 2 --resize 768
#   bash run_screen_ocr.sh --limit 30 --model google/gemini-2.5-flash-lite --max-frames 2 --resize 768   # проба
#   bash run_screen_ocr.sh --model google/gemini-2.5-flash-lite --max-frames 2 --resize 768              # всё, в фоне
cd "$(dirname "$0")"
pip install -q requests pillow 2>/dev/null
BASE=(--out new/out --ids new/mac_posts.csv --carousels "")
if [[ " $* " == *" --dry-run "* || " $* " == *" --limit "* ]]; then
  python -u server_screen_ocr.py "${BASE[@]}" "$@"; exit
fi
ARGS=$(printf '%q ' "$@")
nohup bash -c "
  while pgrep -f '[n]ew_schools.py' >/dev/null; do sleep 60; done
  echo \"старт текста с экрана: \$(date)\"
  python -u server_screen_ocr.py --out new/out --ids new/mac_posts.csv --carousels '' --workers 8 $ARGS
  python -u new_schools.py --mac-only --steps table,pack
" > screen_ocr.log 2>&1 &
echo "Запущено. Смотреть: tail -3 /teamproject/server/screen_ocr.log"
