#!/bin/bash
# Обновить код из GitHub (dapiskarevada-dot/teamproject, ветка main).
# Перезаписывает скрипты (*.py, *.command, README, requirements, schools.txt).
# НЕ трогает: .venv, cases/ (данные), openrouter_key.txt, github_token.txt, pipeline_state.json.
# schools_plan.txt не перезаписывается: новая версия из GitHub кладётся как schools_plan.txt.new.
cd "$(dirname "$0")"
TMP="$(mktemp -d)"
URL="https://github.com/dapiskarevada-dot/teamproject/archive/refs/heads/main.zip"
echo "=== Скачиваю $URL"
if ! curl -sSL -m 120 -o "$TMP/main.zip" "$URL"; then echo "Не удалось скачать (нет сети / VPN?)"; read -p "Enter"; exit 1; fi
# ditto (родной архиватор macOS) понимает русские имена файлов; unzip на них иногда пишет «disk full?»
if command -v ditto >/dev/null; then ditto -x -k "$TMP/main.zip" "$TMP" 2>/dev/null; else unzip -q -o "$TMP/main.zip" -d "$TMP" 2>/dev/null; fi
SRC="$TMP/teamproject-main"
[ -f "$SRC/pipeline.py" ] || { echo "Не удалось распаковать архив"; read -p "Enter"; exit 1; }
cp "$SRC"/*.py . && cp "$SRC"/*.command . && cp "$SRC"/README.md "$SRC"/requirements.txt "$SRC"/schools.txt "$SRC"/school_aliases.tsv . && mkdir -p server && cp "$SRC"/server/*.py "$SRC"/server/*.sh "$SRC"/server/*.md "$SRC"/server/*.csv server/
# списки запросов/хэштегов копируем только если их ещё нет (свои не перезаписываем)
# план школ: если своего ещё нет — берём из GitHub; если есть — свежая версия кладётся рядом как schools_plan.txt.new
if [ -f schools_plan.txt ]; then cp "$SRC/schools_plan.txt" schools_plan.txt.new; else cp "$SRC/schools_plan.txt" .; fi
for f in "$SRC"/plan_*.txt; do [ -f "$(basename "$f")" ] || cp "$f" .; done   # новые планы (например plan_new_schools.txt)
chmod +x *.command; rm -rf __pycache__ "$TMP"
echo "=== Обновлено:"; ls -la *.py | awk '{print $6, $7, $8, $9}'
read -p "Нажмите Enter, чтобы закрыть"
