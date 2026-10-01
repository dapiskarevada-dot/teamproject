#!/bin/bash
# Обновить код из GitHub (dapiskarevada-dot/teamproject, ветка main).
# Перезаписывает скрипты (*.py, *.command, README, requirements, schools.txt).
# НЕ трогает: .venv, cases/ (данные), openrouter_key.txt, github_token.txt, pipeline_state.json.
# schools_plan.txt перезаписывается (свои правки плана держите в копии).
cd "$(dirname "$0")"
TMP="$(mktemp -d)"
URL="https://github.com/dapiskarevada-dot/teamproject/archive/refs/heads/main.zip"
echo "=== Скачиваю $URL"
if ! curl -sSL -m 120 -o "$TMP/main.zip" "$URL"; then echo "Не удалось скачать (нет сети / VPN?)"; read -p "Enter"; exit 1; fi
unzip -q -o "$TMP/main.zip" -d "$TMP" || { echo "Не удалось распаковать"; read -p "Enter"; exit 1; }
SRC="$TMP/teamproject-main"
cp "$SRC"/*.py . && cp "$SRC"/*.command . && cp "$SRC"/README.md "$SRC"/requirements.txt "$SRC"/schools.txt .
# списки запросов/хэштегов копируем только если их ещё нет (свои не перезаписываем)
[ -f schools_plan.txt ] && cp -n "$SRC/schools_plan.txt" schools_plan.txt.new 2>/dev/null; true
chmod +x *.command; rm -rf __pycache__ "$TMP"
echo "=== Обновлено:"; ls -la *.py | awk '{print $6, $7, $8, $9}'
read -p "Нажмите Enter, чтобы закрыть"
