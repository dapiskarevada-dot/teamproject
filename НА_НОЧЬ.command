#!/bin/bash
# ОДИН ФАЙЛ НА НОЧЬ: добавить аккаунты TikTok -> проверить Whisper -> запустить все школы параллельно.
# Двойной клик в Finder. Единственное, что нужно сделать руками, — залогиниться в браузере для каждого
# нового аккаунта (TikTok не даёт войти без человека: капча/код). Потом можно уходить — Mac не уснёт.
cd "$(dirname "$0")"
source .venv/bin/activate
rm -rf __pycache__

echo "=================== ШАГ 1. АККАУНТЫ TIKTOK ==================="
echo "Сейчас в пуле:"
python -m pytok.accounts.cli list
echo
while true; do
  echo ">>> Введите логин нового аккаунта (email / телефон / username) и Enter."
  echo ">>> Пустая строка — аккаунтов больше нет, переходим к запуску."
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

echo "=================== ШАГ 2. ПРОВЕРКИ ==================="
# старые данные Умскула -> новая папка школы (один раз)
if [ -d cases/tiktok_search_threads ] && [ ! -d cases/Умскул ]; then
  mv cases/tiktok_search_threads cases/Умскул && echo ">>> Данные Умскула перенесены в cases/Умскул"
fi
# Whisper
if ! python -c "import faster_whisper" 2>/dev/null; then
  echo ">>> Whisper не установлен, ставлю (5–10 минут)..."
  pip install -q faster-whisper av numpy yt-dlp
fi
[ -f openrouter_key.txt ] || echo "!!! Нет openrouter_key.txt — слайды каруселей не будут расшифрованы (остальное пойдёт)."
python -u collect_tiktok_search_threads.py --check || { echo "!!! Проверка кода не прошла"; read -p "Enter"; exit 1; }
echo

echo "=================== ШАГ 3. ЗАПУСК НА НОЧЬ ==================="
echo ">>> Школы из schools_plan.txt, до 500 постов на школу, потоков = число аккаунтов в пуле. Логи: pipeline_<школа>.log"
echo ">>> Mac не уснёт, пока идёт сбор. Это окно можно свернуть, НЕ закрывать."
echo
caffeinate -dims python -u pipeline.py --parallel 0 "$@" 2>&1 | tee "night_$(date +%Y%m%d_%H%M).log"
echo
echo "=================== ГОТОВО ==================="
echo "Итоги: ИТОГ_<школа>.xlsx по каждой школе и ИТОГ_ВСЕ_ШКОЛЫ.xlsx. Состояние: pipeline_state.json"
echo "Если что-то оборвалось — запустите этот же файл снова (аккаунты не вводить, просто Enter): продолжит с места обрыва."
read -p "Нажмите Enter, чтобы закрыть"
