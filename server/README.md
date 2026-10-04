# Whisper на арендованном GPU-сервере

1. **На Mac**: `СЕРВЕР_ССЫЛКИ.command` → появится `server/links.csv` (и `server/cookies.txt`, если делали `КУКИ.command`).
2. **Сервер**: RunPod / Vast.ai, шаблон с CUDA (например «PyTorch 2.x»), GPU уровня RTX 3090/4090/A5000, диск 30+ ГБ.
   В терминале пода:
   ```
   git clone https://github.com/dapiskarevada-dot/teamproject.git && cd teamproject/server
   ```
   Загрузить `links.csv` (и `cookies.txt`) в эту папку (через Jupyter/файловый менеджер пода или `scp`), затем:
   ```
   bash run_server.sh
   ```
   Темп на RTX 4090: ~10–20 роликов/мин (large-v3, beam 5). Прерывать можно — повторный запуск продолжит.
   В конце появится `out.tgz`.
3. **На Mac**: скачать `out.tgz` в папку `server/` → `СЕРВЕР_ИМПОРТ.command` → таблицы пересоберутся.
   Текст с кадров (API): `./ТРАНСКРИПТЫ.command --no-download` подхватит сохранённые кадры.
Если сервер получил много `download:` ошибок (TikTok не отдаёт видео с его IP) — нужен `cookies.txt` или другой регион пода.
