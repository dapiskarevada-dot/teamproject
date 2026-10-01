#!/bin/bash
# Параллельный сбор: 1) поиск + хэштеги (один раз, со слайдами и расшифровкой), 2) посты делятся на N частей,
# 3) открываются N окон Терминала — каждое берёт свой аккаунт из пула и собирает
# комментарии+реплаи своей части, 4) когда все окна закончат — запустите 9b_merge_tables.command.
cd "$(dirname "$0")"
source .venv/bin/activate
rm -rf __pycache__
N=${1:-4}   # число потоков = число залогиненных аккаунтов
echo "=== Аккаунты в пуле:"; python -m pytok.accounts.cli list
echo
echo "=== Поиск по queries.txt + hashtags.txt (без комментариев)"
python -u collect_tiktok_search_threads.py --queries-file queries.txt --hashtags-file hashtags.txt --search-count 200 --search-only --fetch-author 2>&1 | tee "search_$(date +%Y%m%d_%H%M).log"
POSTS=$(ls -t cases/tiktok_search_threads/search/search_posts_*.json | head -1)
echo
echo "=== Делим посты из $POSTS на $N частей"
python split_urls.py "$POSTS" --parts "$N" --skip-done
echo
DIR="$(pwd)"
for i in $(seq 1 "$N"); do
  F="$DIR/parallel/urls_part_$i.txt"
  [ -s "$F" ] || continue
  CMD="cd \"$DIR\" && source .venv/bin/activate && python -u collect_tiktok_threads.py --urls-file \"$F\" --count 200 --case-dir cases/tiktok_search_threads 2>&1 | tee \"$DIR/parallel/worker_$i.log\"; echo; echo '=== Поток $i завершён'"
  osascript -e "tell application \"Terminal\" to do script \"$CMD\"" >/dev/null
  sleep 20   # чтобы браузеры стартовали не одновременно
done
echo "=== Открыто $N окон Терминала. Дождитесь в каждом строки «Поток N завершён», затем запустите 9b_merge_tables.command"
read -p "Нажмите Enter, чтобы закрыть это окно (потоки продолжат работать)"
