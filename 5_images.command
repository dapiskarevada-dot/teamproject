#!/bin/bash
# Скачивание слайдов фото-постов по ссылкам из photo_urls.txt (вручную; конвейер делает это сам)
cd "$(dirname "$0")"
source .venv/bin/activate
python -u collect_tiktok_images.py --urls-file photo_urls.txt 2>&1 | tee images_run.log
echo
echo "=== Готово. Лог: images_run.log"
read -p "Нажмите Enter, чтобы закрыть"
