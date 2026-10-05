#!/bin/bash
# После ПЕРЕПИСЬ_НОВЫЕ.command: все найденные посты новых школ -> server/new_posts_mac.csv (загрузить на под в /teamproject/server/).
cd "$(dirname "$0")"; source .venv/bin/activate
python server/export_new_schools.py
open server; echo; read -p "Нажмите Enter, чтобы закрыть"
