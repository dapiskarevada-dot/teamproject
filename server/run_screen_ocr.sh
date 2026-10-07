#!/bin/bash
# Текст на экране роликов БЕЗ РЕЧИ из постов с Маков (new/mac_posts.csv) через Gemini -> new/out/screen_text.jsonl,
# потом пересобирает таблицу new/МАКИ_посты.csv и архив new_schools_result.tgz.
# Ждёт, пока закончится текущий new_schools.py (если идёт). Повторный запуск продолжает с места.
#   bash run_screen_ocr.sh --dry-run     # только посчитать ролики и стоимость
#   bash run_screen_ocr.sh               # в фоне; лог: screen_ocr.log
cd "$(dirname "$0")"
pip install -q requests pillow 2>/dev/null
if [ "$1" = "--dry-run" ]; then
  python -u server_screen_ocr.py --out new/out --ids new/mac_posts.csv --carousels "" --dry-run; exit
fi
nohup bash -c '
  while pgrep -f "[n]ew_schools.py" >/dev/null; do sleep 60; done
  echo "старт текста с экрана: $(date)"
  python -u server_screen_ocr.py --out new/out --ids new/mac_posts.csv --carousels "" --workers 8
  python -u new_schools.py --mac-only --steps table,pack
' > screen_ocr.log 2>&1 &
echo "Запущено. Смотреть: tail -3 /teamproject/server/screen_ocr.log"
