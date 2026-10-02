#!/bin/bash
# Где сейчас сбор (по школам). Можно запускать в любой момент, сбору не мешает.
cd "$(dirname "$0")"
source .venv/bin/activate
python status.py
[ -f plan_general_ege.txt ] && [ -d cases/ЕГЭ_общее ] && { echo; python status.py --plan plan_general_ege.txt; }
echo
read -p "Нажмите Enter, чтобы закрыть"
