#!/bin/bash
# НА СЕРВЕРЕ (RunPod/Vast, образ с CUDA, например "PyTorch 2.x"). В терминале пода:
#   git clone https://github.com/dapiskarevada-dot/teamproject.git && cd teamproject/server
#   (загрузить сюда links.csv с Mac; при 403 — ещё cases/tiktok_media/cookies.txt как cookies.txt)
#   bash run_server.sh
# Результат: out/transcripts.jsonl и out/frames/ -> упаковать: tar czf out.tgz out  -> скачать на Mac -> server/import_transcripts.py
set -e
cd "$(dirname "$0")"
apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null 2>&1 || true
pip install -q -U faster-whisper yt-dlp av numpy pillow curl_cffi
nvidia-smi | head -12
COOK=""; [ -f cookies.txt ] && COOK="--cookies cookies.txt"
IMP="${IMPERSONATE:-chrome}"
python -u server_transcribe.py "${LINKS:-links.csv}" --workers 8 --impersonate "$IMP" --dl "${DL:-auto}" $COOK 2>&1 | tee -a server.log
tar czf out.tgz out && echo "Готово: out.tgz (скачать на Mac и запустить server/import_transcripts.py out.tgz)"
