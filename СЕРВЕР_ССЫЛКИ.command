#!/bin/bash
# Шаг 1 для сервера: собрать server/links.csv (все видео с упоминанием школ без транскрипта) и показать папку.
cd "$(dirname "$0")"; source .venv/bin/activate
python server/export_links.py "$@"
cp cases/tiktok_media/cookies.txt server/cookies.txt 2>/dev/null && echo "cookies.txt тоже скопирован в server/ (загрузите на сервер вместе с links.csv)"
open server; echo; read -p "Нажмите Enter, чтобы закрыть"
