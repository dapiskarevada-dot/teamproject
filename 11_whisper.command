#!/bin/bash
# Расшифровка речи (Whisper) для всех скачанных видео в cases/tiktok_media — отдельно от сбора.
# Конвейер делает это сам (--whisper missing), этот файл — чтобы дорасшифровать/пересчитать.
# Другая модель: ./11_whisper.command small
cd "$(dirname "$0")"
source .venv/bin/activate
python -u transcribe_whisper.py --model "${1:-large-v3-turbo}" 2>&1 | tee whisper.log
echo
echo "=== Таблица: cases/tiktok_media/transcripts.xlsx"
read -p "Нажмите Enter, чтобы закрыть"
