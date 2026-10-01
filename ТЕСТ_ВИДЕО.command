#!/bin/bash
# Проверка скачивания видео через браузер (3 ролика из уже собранного). Можно запускать параллельно с ночным сбором.
cd "$(dirname "$0")"
source .venv/bin/activate
rm -rf __pycache__
pip install -q -U yt-dlp 2>/dev/null
python -u test_video_download.py --n 3 2>&1 | tee "test_video_$(date +%Y%m%d_%H%M).log"
echo
read -p "Нажмите Enter, чтобы закрыть"
