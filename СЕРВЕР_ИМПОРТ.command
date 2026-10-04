#!/bin/bash
# Шаг 3 для сервера: положить скачанный out.tgz в папку server/ и запустить этот файл.
cd "$(dirname "$0")"; source .venv/bin/activate
python server/import_transcripts.py server/out.tgz && python -u pipeline.py --final-only | tail -20
echo; read -p "Нажмите Enter, чтобы закрыть"
