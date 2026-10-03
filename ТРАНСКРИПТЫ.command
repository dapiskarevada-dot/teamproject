#!/bin/bash
# ТРАНСКРИПТЫ ПО ССЫЛКАМ: без браузера и аккаунтов. yt-dlp качает ролик -> 5 кадров -> Whisper large-v3 (GPU) -> видео удаляется.
# По выборкам школ (cases/<школа>/sample_ids.txt). Все посты с упоминанием школы: ./ТРАНСКРИПТЫ.command --all
# Можно запускать параллельно с ГЛУБИНА.command. Повторный запуск продолжает с места. Результат -> ТАБЛИЦЫ.command.
cd "$(dirname "$0")"
TMP="$(mktemp -d)"
if curl -sSL -m 60 -o "$TMP/main.zip" "https://github.com/dapiskarevada-dot/teamproject/archive/refs/heads/main.zip" 2>/dev/null && unzip -q -o "$TMP/main.zip" -d "$TMP" 2>/dev/null; then
  cp "$TMP"/teamproject-main/*.py . && cp "$TMP"/teamproject-main/*.command . && chmod +x *.command && echo ">>> Код обновлён."
fi
rm -rf "$TMP" __pycache__
source .venv/bin/activate
python -c "import faster_whisper, av, numpy, PIL" 2>/dev/null || pip install -q faster-whisper av numpy pillow
if [ "$(uname -m)" = "arm64" ]; then python -c "import mlx_whisper" 2>/dev/null || pip install -q mlx-whisper; fi
pip install -q -U yt-dlp 2>/dev/null
echo ">>> Whisper large-v3 по ссылкам. Лог: transcripts_$(date +%Y%m%d_%H%M).log. Окно можно свернуть, не закрывать."
caffeinate -dims python -u transcribe_links.py "$@" 2>&1 | tee "transcripts_$(date +%Y%m%d_%H%M).log"
echo; read -p "Нажмите Enter, чтобы закрыть"
