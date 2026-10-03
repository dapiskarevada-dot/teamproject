#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Текст с кадров видео: большинство роликов TikTok про школы — музыка + текст на экране, речи нет.
Берём несколько кадров из video.mp4 (равномерно по длительности), убираем почти одинаковые,
отправляем в ту же нейросеть через API, что и слайды каруселей (ocr_vlm), склеиваем текст.

    python video_frames_ocr.py                       # все скачанные видео без frames.vlm.json
    python video_frames_ocr.py --post 123 --frames 6 --force

Кэш: cases/tiktok_media/raw/posts/<id>/frames/*.jpg и frames.<model>.vlm.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path("cases") / "tiktok_media"
DEFAULT_FRAMES = 5
MIN_GAP = 0.5          # кадры с «расстоянием» хэша меньше этого считаем одинаковыми


def extract_frames(video: Path, out_dir: Path, n: int = DEFAULT_FRAMES, max_side: int = 720):
    """n кадров равномерно по длительности -> JPEG; возвращает список путей."""
    import av
    from PIL import Image
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with av.open(str(video)) as c:
        st = c.streams.video[0]
        dur = float(c.duration / av.time_base) if c.duration else (float(st.duration * st.time_base) if st.duration else 0.0)
        if dur <= 0:
            dur = 15.0
        # моменты: не самый первый и не самый последний кадр (там часто чёрный/логотип)
        times = [dur * (i + 0.5) / n for i in range(n)]
        for i, t in enumerate(times):
            try:
                c.seek(int(t / st.time_base), stream=st, backward=True, any_frame=False)
                frame = next(c.decode(st))
                img = frame.to_image()
                img.thumbnail((max_side, max_side))
                p = out_dir / f"frame_{i:02d}_{int(t):03d}s.jpg"
                img.save(p, "JPEG", quality=85)
                paths.append(p)
            except Exception:
                continue
    return paths


def dhash(path: Path, size: int = 8):
    from PIL import Image
    img = Image.open(path).convert("L").resize((size + 1, size))
    px = list(img.getdata())
    bits = []
    for r in range(size):
        row = px[r * (size + 1):(r + 1) * (size + 1)]
        bits += [1 if row[i] > row[i + 1] else 0 for i in range(size)]
    return bits


def dedupe_frames(paths, min_gap=MIN_GAP):
    kept, hashes = [], []
    for p in paths:
        try:
            h = dhash(p)
        except Exception:
            continue
        if all(sum(a != b for a, b in zip(h, k)) / len(h) >= min_gap * 0.25 for k in hashes):
            kept.append(p); hashes.append(h)
    return kept or paths[:1]


FRAME_PROMPT = (
    "Это кадр из TikTok-видео про подготовку к ЕГЭ/ОГЭ и онлайн-школы. Ответь строго в JSON без пояснений:\n"
    '{"text": "<весь текст, видимый на кадре, дословно (надписи, стикеры, подписи, плашки); пустая строка, если текста нет>",\n'
    ' "schools": ["<названия онлайн-школ, которые видны или упомянуты на кадре: {names}; другие тоже называй; пустой список, если нет>"],\n'
    ' "promo": "<промокод, ссылка, призыв купить/записаться, если есть; иначе пустая строка>"}'
)


def transcribe_video(post_dir: Path, model, base_url, key, schools, n_frames=DEFAULT_FRAMES, force=False, quiet=False):
    """Текст с кадров одного видео. Возвращает dict или None (нет видео)."""
    from ocr_vlm import ask_model, parse_json
    from transcribe_whisper import find_video
    video = find_video(post_dir)
    if not video:
        return None
    cache = post_dir / f"frames.{model.replace('/', '_')}.vlm.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text(encoding="utf-8"))
    frames = extract_frames(video, post_dir / "frames", n_frames)
    frames = dedupe_frames(frames)
    prompt = FRAME_PROMPT.replace("{names}", ", ".join(schools))
    texts, sch, promos, per = [], [], [], []
    for f in frames:
        try:
            d = parse_json(ask_model(key, base_url, model, f, prompt=prompt))
        except Exception as exc:
            per.append({"frame": f.name, "error": str(exc)[:200]}); continue
        t = (d.get("text") or "").strip()
        if t and t not in texts:
            texts.append(t)
        for x in d.get("schools") or []:
            if x and x not in sch:
                sch.append(x)
        pr = (d.get("promo") or "").strip()
        if pr and pr not in promos:
            promos.append(pr)
        per.append({"frame": f.name, "text": t, "schools": d.get("schools") or [], "promo": pr})
    res = {"model": model, "frames": len(frames), "text": "\n".join(texts)[:16000], "schools": sch, "promo": "; ".join(promos), "per_frame": per}
    cache.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    if not quiet:
        print(f"  frames {post_dir.name}: {len(frames)} кадров | {res['text'][:90].replace(chr(10), ' / ')}", flush=True)
    return res


def transcribe_dir(root: Path = DEFAULT_ROOT, post_ids=None, model=None, base_url=None, n_frames=DEFAULT_FRAMES, force=False, quiet=False):
    from ocr_vlm import DEFAULT_MODEL, DEFAULT_BASE, load_key, load_schools
    model = model or DEFAULT_MODEL; base_url = base_url or DEFAULT_BASE
    key = load_key()
    if not key:
        raise RuntimeError("Нет ключа API: openrouter_key.txt или OPENROUTER_API_KEY")
    schools = load_schools()
    out = {}
    dirs = sorted((root / "raw" / "posts").glob("*"))
    if post_ids:
        dirs = [d for d in dirs if d.name in post_ids]
    print(f"Frames OCR: {len(dirs)} видео, модель {model}", flush=True)
    for i, d in enumerate(dirs, 1):
        try:
            r = transcribe_video(d, model, base_url, key, schools, n_frames, force, quiet)
        except Exception as exc:
            print(f"  frames {d.name}: ОШИБКА {type(exc).__name__}: {str(exc)[:160]}", flush=True); continue
        if r:
            out[d.name] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--post", action="append", default=[])
    ap.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    ap.add_argument("--model", default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    res = transcribe_dir(a.root, set(a.post) or None, a.model, None, a.frames, a.force)
    ok = sum(1 for r in res.values() if r.get("text"))
    print(f"\nГотово: с текстом на экране {ok} из {len(res)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
