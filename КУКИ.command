#!/bin/bash
# Экспорт куки из браузера pytok (нужен залогиненный аккаунт) -> cases/tiktok_media/cookies.txt.
# После этого ТРАНСКРИПТЫ.command качает видео через yt-dlp без браузера.
cd "$(dirname "$0")"
source .venv/bin/activate
python -u export_cookies.py
echo; read -p "Нажмите Enter, чтобы закрыть"
