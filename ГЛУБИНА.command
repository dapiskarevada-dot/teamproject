#!/bin/bash
# СКАЧИВАНИЕ ВИДЕО ЧЕРЕЗ БРАУЗЕР (когда yt-dlp не работает): по собранным постам (планка SAMPLE в pipeline.py)
# скачать видео + субтитры TikTok. Расшифровку делает ТРАНСКРИПТЫ.command (запускать параллельно, с --wait).
# Комментарии и реплаи — отдельно: КОММЕНТАРИИ.command. Повторный запуск продолжает с места обрыва.
cd "$(dirname "$0")"
exec bash "./НА_НОЧЬ.command" --plan plan_census.txt --heavy --redo --search-only --whisper off --frames off "$@"
