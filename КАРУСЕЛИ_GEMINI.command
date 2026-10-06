#!/bin/bash
# Текст с каруселей через Gemini (OpenRouter) по server/new_posts_mac*.csv -> папка КАРУСЕЛИ_ТЕКСТ/
# Нужен openrouter_key.txt в папке проекта. Повторный запуск продолжает с места.
cd "$(dirname "$0")"; source .venv/bin/activate
python -c "import requests" 2>/dev/null || pip install -q requests
caffeinate -i python -u carousels_ocr.py "$@" 2>&1 | tee -a carousels_ocr.log
open КАРУСЕЛИ_ТЕКСТ 2>/dev/null; echo; [ -n "$NO_PAUSE" ] || read -p "Нажмите Enter, чтобы закрыть"
