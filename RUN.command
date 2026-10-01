#!/bin/bash
# ОДНА КНОПКА: весь конвейер (поиск + хэштеги -> посты -> слайды/OCR -> видео/Whisper -> комментарии -> ИТОГ_<школа>.xlsx)
# Настройки — в шапке pipeline.py или аргументами: ./RUN.command --school Фоксфорд --queries q.txt --hashtags h.txt
# Пробный маленький прогон: ./RUN.command --test
cd "$(dirname "$0")"
source .venv/bin/activate
rm -rf __pycache__
python -u pipeline.py "$@" 2>&1 | tee "pipeline_$(date +%Y%m%d_%H%M).log"
echo
read -p "Нажмите Enter, чтобы закрыть"
