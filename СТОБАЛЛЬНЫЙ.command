#!/bin/bash
# ДОСБОР «100балльный репетитор» на Маке через залогиненные аккаунты TikTok: запросы, хэштеги (в т.ч. «100бальный»,
# «стобальный»), аккаунты школы и ленты авторов. Только посты — без видео, Whisper, OCR и комментариев (это сделает сервер).
# Окно 01.10.2025–01.10.2026. Уже собранное в переписи не теряется: новые посты добавляются в ту же папку cases/.
# Итог: server/new_posts_mac_100b.csv -> загрузить на под в /teamproject/server/ и запустить там bash run_new_schools.sh --mac-only
cd "$(dirname "$0")"
[ -f plan_100b.txt ] || { echo "Нет plan_100b.txt — сначала 0_update_from_github.command"; read -p Enter; exit 1; }
NO_PAUSE=1 bash "./НА_НОЧЬ.command" --plan plan_100b.txt --search-only --no-videos --no-ocr --whisper off --redo "$@"
echo; echo "=================== ФАЙЛ ДЛЯ СЕРВЕРА ==================="
source .venv/bin/activate && python server/export_new_schools.py --plan plan_100b.txt --out new_posts_mac_100b.csv
open server; [ -n "$NO_PAUSE" ] || read -p "Нажмите Enter, чтобы закрыть"
