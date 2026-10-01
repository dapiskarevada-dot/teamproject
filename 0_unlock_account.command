#!/bin/bash
# Если прогон упал с "No account available" (аккаунт остался помечен как занятый
# после прерывания) — запустите этот файл, он снимет блокировку.
cd "$(dirname "$0")"
source .venv/bin/activate
python -m pytok.accounts.cli list
echo
echo "=== Введите username аккаунта из колонки Username и нажмите Enter:"
read -r U
[ -n "$U" ] && python -m pytok.accounts.cli release "$U" && python -m pytok.accounts.cli unlock "$U"
python -m pytok.accounts.cli list
read -p "Нажмите Enter, чтобы закрыть"
