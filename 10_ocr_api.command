#!/bin/bash
# Расшифровка слайдов каруселей нейросетью через API (OpenRouter).
# Ключ должен лежать в openrouter_key.txt (одна строка) рядом с этим файлом.
cd "$(dirname "$0")"
source .venv/bin/activate
if [ ! -s openrouter_key.txt ]; then echo "Нет файла openrouter_key.txt с ключом OpenRouter"; read -p "Enter"; exit 1; fi
python ocr_vlm.py --model "${1:-google/gemini-2.5-flash}" 2>&1 | tee ocr_api.log
echo
echo "=== Таблицы: cases/tiktok_media/images_text.xlsx (по слайдам) и posts_text.xlsx (по постам)"
read -p "Нажмите Enter, чтобы закрыть"
