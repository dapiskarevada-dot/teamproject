#!/bin/bash
# Установка faster-whisper и зависимостей в основное окружение + загрузка модели (один раз, ~1.5 ГБ)
cd "$(dirname "$0")"
source .venv/bin/activate
pip install faster-whisper av numpy yt-dlp
python - <<'PY'
from faster_whisper import WhisperModel
print("Скачиваю модель large-v3-turbo (~1.6 ГБ)...")
WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
print("=== OK: Whisper готов")
PY
read -p "Нажмите Enter, чтобы закрыть"
