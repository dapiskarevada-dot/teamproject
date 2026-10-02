#!/bin/bash
# Собрать итоговые таблицы из того, что уже есть (сбор можно не останавливать — только читает данные).
cd "$(dirname "$0")"
source .venv/bin/activate
python -u pipeline.py --final-only
[ -f plan_general_ege.txt ] && [ -d cases/ЕГЭ_общее ] && python -u pipeline.py --final-only --plan plan_general_ege.txt
echo
echo "Файлы: ИТОГ_<школа>.xlsx и ИТОГ_ВСЕ_ШКОЛЫ.xlsx в этой папке."
read -p "Нажмите Enter, чтобы закрыть"
