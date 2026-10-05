#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
#  НА НОЧЬ — один файл, двойной клик в Finder. Делает всё сам:
#   1) подтягивает свежий код из GitHub (если есть интернет),
#   2) ставит окружение, если его ещё нет (первый запуск на новом Mac: 5–15 минут),
#   3) спрашивает, добавить ли аккаунты TikTok (если не нужно — просто Enter),
#   4) запускает сбор по всем школам из schools_plan.txt, параллельно, по потоку на аккаунт.
#  Mac не уснёт, пока идёт сбор. Окно можно свернуть, НЕ закрывать.
#  Если что-то оборвалось — запустить этот же файл снова: продолжит с места обрыва.
#  Нужные секреты в этой папке: openrouter_key.txt (расшифровка слайдов). Без него слайды пропустятся.
# ═══════════════════════════════════════════════════════════════════════════════
cd "$(dirname "$0")"
REPO_ZIP="https://github.com/dapiskarevada-dot/teamproject/archive/refs/heads/main.zip"

echo "=================== ШАГ 0. ОБНОВЛЕНИЕ КОДА ИЗ GITHUB ==================="
TMP="$(mktemp -d)"
if curl -sSL -m 60 -o "$TMP/main.zip" "$REPO_ZIP" 2>/dev/null && unzip -q -o "$TMP/main.zip" -d "$TMP" 2>/dev/null; then
  SRC="$TMP/teamproject-main"
  cp "$SRC"/*.py . && cp "$SRC"/*.command . && cp "$SRC"/README.md "$SRC"/requirements.txt "$SRC"/schools.txt "$SRC"/school_aliases.tsv . && mkdir -p server && cp "$SRC"/server/*.py "$SRC"/server/*.sh "$SRC"/server/*.md "$SRC"/server/*.csv server/
  [ -f schools_plan.txt ] || cp "$SRC/schools_plan.txt" .
  for f in "$SRC"/plan_*.txt; do [ -f "$(basename "$f")" ] || cp "$f" .; done
  chmod +x *.command
  echo ">>> Код обновлён."
else
  echo ">>> GitHub недоступен (нет сети / VPN) — работаю с тем кодом, что есть в папке."
fi
rm -rf "$TMP" __pycache__

echo
echo "=================== ШАГ 1. ОКРУЖЕНИЕ ==================="
if [ ! -d .venv ]; then
  echo ">>> Первый запуск на этом Mac: ставлю окружение (5–15 минут)..."
  python3 --version || { echo "!!! Нет python3. Установите с python.org и запустите снова."; read -p "Enter"; exit 1; }
  python3 -m venv .venv
fi
source .venv/bin/activate
python -c "import pytok" 2>/dev/null || pip install -q "git+https://github.com/MEOMcGill/pytok.git@master"
python -c "import pandas, openpyxl, requests" 2>/dev/null || pip install -q -r requirements.txt
python -c "import camoufox" 2>/dev/null || pip install -q camoufox
python -m camoufox path >/dev/null 2>&1 || python -m camoufox fetch
python -c "import faster_whisper, av, numpy" 2>/dev/null || { echo ">>> Ставлю Whisper (5–10 минут)..."; pip install -q faster-whisper av numpy; }
python -c "import PIL" 2>/dev/null || pip install -q pillow
# Apple Silicon: Whisper на GPU через mlx-whisper (в разы быстрее faster-whisper на CPU)
if [ "$(uname -m)" = "arm64" ]; then python -c "import mlx_whisper" 2>/dev/null || pip install -q mlx-whisper; fi
pip install -q -U yt-dlp 2>/dev/null
python -u collect_tiktok_search_threads.py --check >/dev/null 2>&1 || { echo "!!! Проверка кода не прошла:"; python -u collect_tiktok_search_threads.py --check; read -p "Enter"; exit 1; }
[ -f openrouter_key.txt ] || echo "!!! Нет openrouter_key.txt — слайды каруселей не будут расшифрованы (остальное пойдёт)."
# старые данные Умскула -> папка школы (один раз)
if [ -d cases/tiktok_search_threads ] && [ ! -d cases/Умскул ]; then mv cases/tiktok_search_threads cases/Умскул; fi
echo ">>> Окружение в порядке."

echo
echo "=================== ШАГ 2. АККАУНТЫ TIKTOK ==================="
echo "Сейчас в пуле:"
python -m pytok.accounts.cli list
echo
while true; do
  echo ">>> Добавить аккаунт? Введите логин (email / телефон / username) и Enter."
  echo ">>> Если добавлять НЕ нужно — просто нажмите Enter."
  read -r U
  [ -z "$U" ] && break
  python -m pytok.accounts.cli add --username "$U"
  echo ">>> Откроется браузер: войдите в TikTok, пройдите капчу, дождитесь ленты — окно закроется само."
  python -m pytok.accounts.cli login --username "$U" --manual-login
  echo ">>> Аккаунт $U добавлен. В пуле:"
  python -m pytok.accounts.cli list
  echo
done

echo
echo "=================== ШАГ 3. ЗАПУСК НА НОЧЬ ==================="
echo ">>> Школы из schools_plan.txt, до 500 постов на школу, потоков = число аккаунтов в пуле."
echo ">>> Логи: night_<дата>.log (общий) и pipeline_<школа>.log (по школам)."
echo ">>> Mac не уснёт, пока идёт сбор. Это окно можно свернуть, НЕ закрывать."
echo
caffeinate -dims python -u pipeline.py --parallel 0 "$@" 2>&1 | tee "night_$(date +%Y%m%d_%H%M).log"
echo
echo "=================== ГОТОВО ==================="
echo "Итоги: ИТОГ_<школа>.xlsx по каждой школе и ИТОГ_ВСЕ_ШКОЛЫ.xlsx. Состояние: pipeline_state.json"
echo "Если что-то оборвалось — запустите этот же файл снова: готовые школы пропустит, продолжит остальные."
[ -n "$NO_PAUSE" ] || read -p "Нажмите Enter, чтобы закрыть"
