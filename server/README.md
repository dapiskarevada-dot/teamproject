# Whisper на арендованном GPU-сервере

1. **На Mac**: `СЕРВЕР_ССЫЛКИ.command` → появится `server/links.csv` (и `server/cookies.txt`, если делали `КУКИ.command`).
2. **Сервер**: RunPod / Vast.ai, шаблон с CUDA (например «PyTorch 2.x»), GPU уровня RTX 3090/4090/A5000, диск 30+ ГБ.
   В терминале пода:
   ```
   git clone https://github.com/dapiskarevada-dot/teamproject.git && cd teamproject/server
   ```
   Загрузить `links.csv` (и `cookies.txt`) в эту папку (через Jupyter/файловый менеджер пода или `scp`), затем:
   Сначала проверка скачивания на 30 роликах (2–3 минуты):
   ```
   pip install -q -U faster-whisper yt-dlp curl_cffi av numpy pillow && python server_transcribe.py links.csv --dl-test "https://www.tiktok.com/@umschoolofficial/video/7644467684544957717" --impersonate chrome   # какой путь скачивания работает (web / api / tikwm)
   ```
   Если в конце «не скачалось всего: 0–3» — запускать всё:
   ```
   bash run_server.sh
   ```
   Если не скачалось большинство — положить `cookies.txt` (с Mac, `КУКИ.command`) и повторить проверку; не помогло — сменить регион пода (EU/US).
   Темп на RTX 4090: ~10–20 роликов/мин (large-v3, beam 5). Прерывать можно — повторный запуск продолжит.
   В конце появится `out.tgz`.
3. **На Mac**: скачать `out.tgz` в папку `server/` → `СЕРВЕР_ИМПОРТ.command` → таблицы пересоберутся.
   Текст с кадров (API): `./ТРАНСКРИПТЫ.command --no-download` подхватит сохранённые кадры.
Если сервер получил много `download:` ошибок (TikTok не отдаёт видео с его IP) — нужен `cookies.txt` или другой регион пода.

## Общая (discovery) выборка по ЕГЭ

`links_general.csv` — 21325 ссылок из `all_posts_dedup.csv` за окно 01.10.2025–01.10.2026 (без каруселей; `merge_general.py` тоже режет по окну). Поставить в очередь на сервере после школ:

    cd /teamproject && git pull && cd server && nohup bash -c 'while pgrep -f '[s]erver_transcribe.py' >/dev/null; do sleep 60; done; LINKS=links_general.csv bash run_server.sh' > server_general.log 2>&1 &

Когда готово — `out.tgz` содержит транскрипты и школ, и общей выборки. Склеить с таблицей discovery (на любом компьютере, где лежит `all_posts_dedup.csv`):

    python server/merge_general.py all_posts_dedup.csv out.tgz

→ `ОБЩИЕ_ЕГЭ_с_транскриптами.xlsx`: листы «Все посты», «Про школы» (кандидаты на комментарии/реплаи), «Сводка».
