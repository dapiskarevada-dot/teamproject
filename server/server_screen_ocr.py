#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА СЕРВЕРЕ: текст с кадров для роликов без речи (музыка + надписи на экране).
Берёт out/transcripts.jsonl, выбирает ролики с пустым транскриптом, у которых есть кадры в out/frames/<id>/,
отправляет кадры в Gemini 2.5 Flash (OpenRouter) и пишет out/screen_text.jsonl. Можно запускать
параллельно с Whisper — это сетевые запросы, видеокарту не трогает. Повторный запуск продолжает с места.

Ключ: файл openrouter_key.txt в /teamproject/ (загрузить через Jupyter) или переменная OPENROUTER_API_KEY.

    python server_screen_ocr.py                 # все ролики без речи
    python server_screen_ocr.py --all           # все ролики с кадрами
    python server_screen_ocr.py --limit 20      # проба
"""
from __future__ import annotations

import argparse, json, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
OUT = HERE / "out"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="все ролики с кадрами, а не только без речи")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    from ocr_vlm import ask_model, parse_json, load_key, load_schools, DEFAULT_MODEL, DEFAULT_BASE
    from video_frames_ocr import FRAME_PROMPT, dedupe_frames
    key = load_key()
    if not key:
        sys.exit("Нет ключа: положите openrouter_key.txt в /teamproject/ (через Jupyter) и запустите снова")
    prompt = FRAME_PROMPT.replace("{names}", ", ".join(load_schools()))

    targets = []
    for line in (OUT / "transcripts.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("error"):
            continue
        if a.all or not (r.get("text") or "").strip():
            targets.append(str(r["post_id"]))
    targets = list(dict.fromkeys(targets))
    res_path = OUT / "screen_text.jsonl"
    done = set()
    if res_path.exists():
        for line in res_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if not r.get("error"):
                    done.add(r["post_id"])
            except Exception:
                pass
    todo = [p for p in targets if p not in done and (OUT / "frames" / p).is_dir()]
    if a.limit:
        todo = todo[: a.limit]
    print(f"Роликов-кандидатов {len(targets)}, уже готово {len(done)}, в работе {len(todo)} (модель {DEFAULT_MODEL})", flush=True)

    def one(pid):
        frames = dedupe_frames(sorted((OUT / "frames" / pid).glob("*.jpg")))
        texts, sch, promos, errs = [], [], [], 0
        for f in frames:
            try:
                d = parse_json(ask_model(key, DEFAULT_BASE, DEFAULT_MODEL, f, prompt=prompt))
            except Exception:
                errs += 1
                continue
            t = (d.get("text") or "").strip()
            if t and t not in texts:
                texts.append(t)
            for x in d.get("schools") or []:
                if x and x not in sch:
                    sch.append(x)
            p = (d.get("promo") or "").strip()
            if p and p not in promos:
                promos.append(p)
        rec = {"post_id": pid, "model": DEFAULT_MODEL, "frames": len(frames), "text": "\n".join(texts)[:16000],
               "schools": sch, "promo": "; ".join(promos)}
        if frames and errs == len(frames):
            rec["error"] = "api"
        return rec

    lock = threading.Lock(); n = 0; t0 = time.monotonic()
    with open(res_path, "a", encoding="utf-8") as fout, ThreadPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(one, p) for p in todo]):
            rec = fut.result()
            with lock:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n"); fout.flush(); n += 1
                if n % 20 == 0 or n == len(todo):
                    rate = n / max(1e-6, (time.monotonic() - t0) / 60)
                    print(f"[{n}/{len(todo)}] {rate:.0f}/мин | {rec['text'][:70].replace(chr(10), ' / ')}", flush=True)
    print(f"Готово: {n} -> out/screen_text.jsonl", flush=True)


if __name__ == "__main__":
    main()
