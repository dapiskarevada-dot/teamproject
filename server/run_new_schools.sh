#!/bin/bash
# НА НОВОМ GPU-ПОДЕ: досбор по новым и пустым школам — посты, расшифровка, кадры, комментарии и ответы.
#   cd /teamproject/server && bash run_new_schools.sh
# Итог: server/new_schools_result.tgz (скачать через Jupyter). Лог: server/new_schools.log
cd "$(dirname "$0")"
apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null 2>&1 || true
pip install -q -U faster-whisper yt-dlp av numpy pillow curl_cffi pandas openpyxl
nvidia-smi | head -12
nohup python -u new_schools.py "$@" > new_schools.log 2>&1 &
echo "Запущено. Смотреть: tail -5 /teamproject/server/new_schools.log"
