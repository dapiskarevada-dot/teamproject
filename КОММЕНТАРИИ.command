#!/bin/bash
# КОММЕНТАРИИ И РЕПЛАИ по той же выборке постов (COMMENTS в pipeline.py = 200 на пост), без видео/Whisper/кадров.
# Параллельно по аккаунтам; уже собранные посты пропускаются. Повторный запуск продолжает с места обрыва.
cd "$(dirname "$0")"
exec bash "./НА_НОЧЬ.command" --plan plan_census.txt --heavy --redo --no-videos --no-images --whisper off --frames off "$@"
