#!/bin/bash
# Добавление исследовательских аккаунтов TikTok и ручной логин (один раз на аккаунт).
# Можно добавить несколько аккаунтов подряд — после каждого спросит «ещё?».
cd "$(dirname "$0")"
source .venv/bin/activate
echo "=== Аккаунты уже в пуле:"
python -m pytok.accounts.cli list
echo
while true; do
  echo "=== Введите логин аккаунта TikTok (email / телефон / username) и нажмите Enter (пустая строка — закончить):"
  read -r U
  [ -z "$U" ] && break
  echo "=== Шаг 1: добавляем профиль $U"
  python -m pytok.accounts.cli add --username "$U"
  echo
  echo "=== Шаг 2: откроется браузер — залогиньтесь в TikTok вручную, пройдите капчу, дождитесь ленты (окно закроется само)"
  python -m pytok.accounts.cli login --username "$U" --manual-login
  echo
  echo "=== Аккаунты в пуле:"
  python -m pytok.accounts.cli list -v
  echo
  echo "=== Добавить ещё один аккаунт? Введите логин, или пустую строку для выхода."
done
echo
echo "=== Проверка без запросов к TikTok:"
python -u collect_tiktok_search_threads.py --check
echo
echo "=== Готово. Дальше: НА_НОЧЬ.command или RUN.command"
read -p "Нажмите Enter, чтобы закрыть"
