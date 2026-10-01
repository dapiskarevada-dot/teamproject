#!/bin/bash
# Установка окружения для teamproject (один раз)
cd "$(dirname "$0")"
echo "=== Папка: $(pwd)"
python3 --version || { echo "Нет python3. Установите с python.org"; read -p "Enter для выхода"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install "git+https://github.com/MEOMcGill/pytok.git@master"
pip install -r requirements.txt
python -m camoufox fetch
python -c "import pytok, camoufox; print('=== OK: pytok и camoufox установлены')"
echo
echo "=== Если camoufox не скачался (rate limit) — запустите 1b_camoufox.command. Дальше: 2_login.command"
read -p "Нажмите Enter, чтобы закрыть"
