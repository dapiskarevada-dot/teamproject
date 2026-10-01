#!/bin/bash
# Добавление исследовательского аккаунта TikTok и ручной логин (один раз на аккаунт)
cd "$(dirname "$0")"
source .venv/bin/activate
echo "=== Введите логин аккаунта TikTok (email / телефон / username), которым будем парсить, и нажмите Enter:"
read -r U
[ -z "$U" ] && { echo "Логин пустой, выходим"; read -p "Enter для выхода"; exit 1; }
echo "=== Шаг 1: добавляем профиль $U"
python -m pytok.accounts.cli add --username "$U"
echo
echo "=== Шаг 2: откроется браузер — залогиньтесь в TikTok вручную, пройдите капчу, дождитесь ленты (окно закроется само)"
python -m pytok.accounts.cli login --username "$U" --manual-login
echo
echo "=== Аккаунты в пуле:"
python -m pytok.accounts.cli list -v
echo
echo "=== Проверка без запросов к TikTok:"
python -u collect_tiktok_search_threads.py --check
echo
echo "=== Готово. Дальше: 3_test.command"
read -p "Нажмите Enter, чтобы закрыть"
