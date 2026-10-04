#!/bin/bash
# Шаг 3 для сервера: положить скачанный out.tgz в папку server/ и запустить этот файл.
cd "$(dirname "$0")"; source .venv/bin/activate
[ -f server/import_transcripts.py ] || { echo "Нет server/import_transcripts.py — запустите 0_update_from_github.command"; read -p Enter; exit 1; }
[ -f server/out.tgz ] || { echo "Нет server/out.tgz — скачайте архив с сервера в папку server/ (имя файла: out.tgz)"; read -p Enter; exit 1; }
python server/import_transcripts.py server/out.tgz && python -u pipeline.py --final-only | tail -20
echo; read -p "Нажмите Enter, чтобы закрыть"
