#!/bin/bash
# НА ПОДЕ (видеокарта не нужна): комментарии + все ответы ко ВСЕМ постам обоих датасетов (перепись + общие ЕГЭ, 41 208).
# Пропускает 20 411 постов, уже собранных сервером раньше (comments_done_server_ids.txt).
# 631 пост, собранный раньше на Маке (там только ~20 верхних комментариев), собирается заново целиком.
#   cd /teamproject/server && bash run_all_comments.sh
# Итог: server/all_comments_result.tgz (скачать через Jupyter). Лог: server/all_comments.log
cd "$(dirname "$0")"
pip install -q -U curl_cffi pandas openpyxl
nohup bash -c '
python -u server_comments.py --plan all_posts_comment_plan.csv --outdir out_all --all-comments \
       --skip-ids comments_done_server_ids.txt --workers ${WORKERS:-16}
tar czf all_comments_result.tgz out_all && echo "ГОТОВО: server/all_comments_result.tgz ($(du -h all_comments_result.tgz | cut -f1)) — скачать через Jupyter"
' > all_comments.log 2>&1 &
echo "Запущено. Смотреть: tail -3 /teamproject/server/all_comments.log"
