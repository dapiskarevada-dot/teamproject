#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""НА MAC: импорт результатов сервера. python server/import_transcripts.py server/out.tgz  (или папка out/)
Пишет transcript.whisper.json и кадры в cases/tiktok_media/raw/posts/<id>/ — дальше ТАБЛИЦЫ.command."""
import json, shutil, sys, tarfile, tempfile
from pathlib import Path
import os; os.chdir(Path(__file__).resolve().parent.parent)
MEDIA = Path("cases") / "tiktok_media" / "raw" / "posts"
src = Path(sys.argv[1] if len(sys.argv) > 1 else "server/out.tgz")
tmp = None
if src.is_file():
    tmp = Path(tempfile.mkdtemp()); tarfile.open(src).extractall(tmp); src = tmp / "out"
n = e = 0
for line in (src / "transcripts.jsonl").read_text(encoding="utf-8").splitlines():
    try: r = json.loads(line)
    except Exception: continue
    pid = r["post_id"]; d = MEDIA / pid; d.mkdir(parents=True, exist_ok=True)
    if r.get("error", "").startswith("download:"):
        e += 1; continue                      # не скачалось на сервере — оставляем для браузера
    (d / "transcript.whisper.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    fr = src / "frames" / pid
    if fr.exists() and not (d / "frames").exists():
        shutil.copytree(fr, d / "frames")
    n += 1
m = 0
st = src / "screen_text.jsonl"
if not st.exists() and Path("server/screen_text.jsonl").exists():
    st = Path("server/screen_text.jsonl")
if st.exists():
    for line in st.read_text(encoding="utf-8").splitlines():
        try: r = json.loads(line)
        except Exception: continue
        if r.get("error"): continue
        d = MEDIA / str(r["post_id"]); d.mkdir(parents=True, exist_ok=True)
        (d / f"frames.{r.get('model', 'google/gemini-2.5-flash').replace('/', '_')}.vlm.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        m += 1
    print(f"Текст с картинок (ролики без речи и карусели): {m}")
print(f"Импортировано транскриптов: {n}, не скачалось на сервере: {e}. Дальше: ТАБЛИЦЫ.command (и ТРАНСКРИПТЫ.command для текста с кадров).")
if tmp: shutil.rmtree(tmp, ignore_errors=True)
