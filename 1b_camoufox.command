#!/bin/bash
# Загрузка браузера camoufox. Если рядом лежит github_token.txt — используется для обхода лимита GitHub API.
cd "$(dirname "$0")"
source .venv/bin/activate
if [ -s github_token.txt ]; then
  export GITHUB_TOKEN="$(tr -d '[:space:]' < github_token.txt)"
  echo "=== Использую токен GitHub из github_token.txt (длина ${#GITHUB_TOKEN})"
fi
DIR="$HOME/Library/Caches/camoufox"
installed() { ls "$DIR" 2>/dev/null | grep -qv '^repo_cache.json$'; }
for i in 1 2 3; do
  echo "=== Попытка $i: camoufox fetch"
  python -m camoufox fetch
  if installed; then echo "=== OK: браузер camoufox загружен"; break; fi
  echo "Браузер не появился в $DIR — жду 60 с и пробую снова..."
  sleep 60
done
echo
echo "=== Содержимое $DIR:"; ls -la "$DIR" 2>&1
read -p "Нажмите Enter, чтобы закрыть"
