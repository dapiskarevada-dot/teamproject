#!/bin/bash
# Комментарии + реплаи по приоритетному списку (server/comment_urls.txt с сервера), по потоку на аккаунт TikTok.
# Mac не уснёт. Окно не закрывать. Повторный запуск продолжит с места.
cd "$(dirname "$0")"; source .venv/bin/activate
[ -f server/comment_urls.txt ] || { echo "Нет server/comment_urls.txt — скачайте его с сервера в папку server/"; read -p Enter; exit 1; }
caffeinate -dims python -u comments_list.py server/comment_urls.txt --count 200 "$@" 2>&1 | tee "comments_$(date +%Y%m%d_%H%M).log"
echo; read -p "Нажмите Enter, чтобы закрыть"
