#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
ТРАНСКРИПТЫ ПО ССЫЛКАМ — самый короткий путь, без браузера и аккаунтов TikTok:
  ссылка -> yt-dlp качает ролик во временную папку -> 5 кадров сохраняются (для текста с экрана)
  -> Whisper large-v3 (GPU на Apple Silicon через mlx, иначе CPU) -> transcript.whisper.json -> видео удаляется.

Откуда ссылки: выборки школ (cases/<школа>/sample_ids.txt) или все посты с упоминанием школы (--all).
Скачивание идёт в несколько потоков параллельно с расшифровкой. Повторный запуск продолжает с места.
Результат подхватывает ТАБЛИЦЫ.command / pipeline.py --final-only (колонка «Транскрипт (Whisper)»).

    python transcribe_links.py                 # по выборкам школ
    python transcribe_links.py --all           # все посты с упоминанием школы
    python transcribe_links.py --keep-video    # не удалять видео
    python transcribe_links.py --ids-file my_ids.txt
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

MEDIA = Path("cases") / "tiktok_media" / "raw" / "posts"
TMP = Path("cases") / "tiktok_media" / "tmp_links"


def load_targets(all_relevant: bool, ids_file: str | None):
    """[(post_id, url)] — из выборок школ или всех постов с упоминанием школы (merged_posts из pipeline)."""
    from pipeline import load_plan, slug, merged_posts, CASES_ROOT
    want = None
    if ids_file:
        want = {l.strip() for l in Path(ids_file).read_text(encoding="utf-8").splitlines() if l.strip()}
    out, seen = [], set()
    for plan_file in ("plan_census.txt", "schools_plan.txt"):
        if Path(plan_file).exists():
            plan = load_plan(Path(plan_file)); break
    else:
        plan = []
    for s in plan:
        case_dir = CASES_ROOT / slug(s["name"])
        if not (case_dir / "search").exists():
            continue
        posts = merged_posts(case_dir)
        if want is not None:
            ids = [i for i in posts if i in want]
        elif all_relevant or not (case_dir / "sample_ids.txt").exists():
            own = [a.strip().lower() for a in s["name"].split(",") if a.strip() and len(a.strip()) >= 4]
            def rel(f):
                blob = " ".join(str(f.get(k) or "") for k in ("description", "hashtags", "author_username", "author_nickname", "author_bio", "slides_text", "subtitle_text")).lower()
                return any(a in blob for a in own)
            ids = [i for i, f in posts.items() if rel(f)]
        else:
            ids = [l.strip() for l in (case_dir / "sample_ids.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
        for i in ids:
            f = posts.get(i) or {}
            if i in seen or f.get("post_type") == "photo":
                continue
            url = f.get("video_url")
            if url:
                seen.add(i); out.append((i, url))
    return out


def download(url: str, folder: Path):
    """yt-dlp без браузера; куки из cookies.txt (если есть) помогают против 403."""
    import yt_dlp
    folder.mkdir(parents=True, exist_ok=True)
    opts = {"format": "best[ext=mp4]/best", "outtmpl": str(folder / "video.%(ext)s"), "quiet": True, "no_warnings": True,
            "noplaylist": True, "retries": 3, "http_headers": {"Referer": "https://www.tiktok.com/"},
            "nocheckcertificate": True}
    ck = Path("cases") / "tiktok_media" / "cookies.txt"
    if ck.exists():
        opts["cookiefile"] = str(ck)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="все посты с упоминанием школы, а не выборки")
    ap.add_argument("--ids-file", default="")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--keep-video", action="store_true")
    ap.add_argument("--workers", type=int, default=4, help="параллельных скачиваний")
    ap.add_argument("--frames", type=int, default=5, help="кадров сохранить для текста с экрана (0 = не сохранять)")
    a = ap.parse_args()
    if sys.platform.startswith("win"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    import transcribe_whisper as tw
    targets = load_targets(a.all, a.ids_file or None)
    todo = [(i, u) for i, u in targets if not (MEDIA / i / "transcript.whisper.json").exists()]
    print(f"Роликов: {len(targets)}, уже расшифровано {len(targets) - len(todo)}, в работе {len(todo)}", flush=True)
    if not todo:
        return 0
    tw.pick_engine()
    prompt = tw.build_prompt()

    q: queue.Queue = queue.Queue(maxsize=a.workers * 3)
    stop = object()
    stats = {"dl_ok": 0, "dl_fail": 0, "tr_ok": 0, "tr_empty": 0, "tr_fail": 0}
    lock = threading.Lock()

    def worker(items):
        for pid, url in items:
            d = MEDIA / pid
            v = tw.find_video(d)
            if not v:
                try:
                    v = download(url, TMP / pid)
                    with lock: stats["dl_ok"] += 1
                except Exception as exc:
                    with lock: stats["dl_fail"] += 1
                    print(f"  {pid}: не скачалось ({str(exc)[:90]})", flush=True)
                    (d).mkdir(parents=True, exist_ok=True)
                    (d / "transcript.whisper.json").write_text(json.dumps({"text": "", "segments": [], "error": f"download: {str(exc)[:200]}"}, ensure_ascii=False), encoding="utf-8")
                    continue
            q.put((pid, v))

    n = max(1, a.workers)
    chunks = [todo[i::n] for i in range(n)]
    threads = [threading.Thread(target=worker, args=(c,), daemon=True) for c in chunks if c]
    for t in threads:
        t.start()
    def closer():
        for t in threads:
            t.join()
        q.put(stop)
    threading.Thread(target=closer, daemon=True).start()

    t0 = time.monotonic(); done = 0
    while True:
        item = q.get()
        if item is stop:
            break
        pid, v = item
        d = MEDIA / pid; d.mkdir(parents=True, exist_ok=True)
        try:
            if a.frames and not (d / "frames").exists():
                try:
                    from video_frames_ocr import extract_frames
                    extract_frames(v, d / "frames", a.frames)
                except Exception as exc:
                    print(f"  {pid}: кадры не сохранены ({str(exc)[:80]})", flush=True)
            r = tw.transcribe_file(v, a.model, prompt=prompt)
            r["model"] = a.model; r["source_file"] = "yt-dlp" if TMP in v.parents else "pytok"
            (d / "transcript.whisper.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            stats["tr_ok" if r["text"] else "tr_empty"] += 1
            done += 1
            rate = done / max(1e-6, (time.monotonic() - t0) / 60)
            print(f"[{done}/{len(todo)}] {pid} {r.get('duration', 0)}с -> {r['seconds']}с | {rate:.1f}/мин | {r['text'][:80]}", flush=True)
        except Exception as exc:
            stats["tr_fail"] += 1
            print(f"  {pid}: ОШИБКА Whisper {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        finally:
            if not a.keep_video and TMP in v.parents:
                try:
                    v.unlink()
                    v.parent.rmdir()
                except Exception:
                    pass
    print("\nГотово:", stats, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
