#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Расшифровка текста с картинок TikTok-каруселей мультимодальной нейросетью через API
(OpenRouter по умолчанию; подойдёт любой OpenAI-совместимый endpoint).

Вход:  cases/tiktok_media/raw/posts/<post_id>/images/*.png|jpg  (как сохраняет collect_tiktok_images.py)
Выход: cases/tiktok_media/images_text.xlsx / .csv  — одна строка на слайд
       cases/tiktok_media/posts_text.xlsx  / .csv  — одна строка на пост (все слайды подряд)
       рядом с картинками: <имя>.vlm.json (сырой ответ модели) — повторно не запрашивается

Ключ: переменная OPENROUTER_API_KEY или файл openrouter_key.txt рядом со скриптом.

    python ocr_vlm.py                                   # все посты, модель по умолчанию
    python ocr_vlm.py --model openai/gpt-4.1-mini
    python ocr_vlm.py --limit 20 --post 7682302594538130709
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

DEFAULT_MODEL = "google/gemini-2.5-flash"
DEFAULT_BASE = "https://openrouter.ai/api/v1"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

DEFAULT_SCHOOLS = ["Умскул", "ЕГЭленд", "Фоксфорд", "Сотка", "Вебиум", "Турбо", "Школково",
                   "Lomonosov School", "Skysmart", "Учи.ру", "99 баллов", "Insperia", "Тетрика"]


def load_schools(path: Path | None = None):
    """schools.txt: одна школа в строке; через запятую можно дать синонимы (Умскул, Umschool)."""
    path = path or Path(__file__).with_name("schools.txt")
    if not path.exists():
        return DEFAULT_SCHOOLS
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(line)
    return out or DEFAULT_SCHOOLS


def build_prompt(schools):
    names = ", ".join(schools)
    return (
        "Это слайд из TikTok-карусели про подготовку к ЕГЭ/ОГЭ. Ответь строго в JSON без пояснений:\n"
        '{"text": "<весь текст со слайда дословно, в порядке чтения, включая стикеры и мелкий текст; '
        'пустая строка, если текста нет>",\n'
        f' "schools": ["<названия онлайн-школ, упомянутых на слайде (в т.ч. в написании латиницей или с опечатками): {names}; '
        'другие школы тоже называй; пустой список, если нет>"],\n'
        ' "context": "<один из: реклама, отзыв, сравнение школ, мем/юмор, учебный контент, личная история, другое>",\n'
        ' "comparison": <true, если на слайде сравниваются или противопоставляются школы, иначе false>,\n'
        ' "summary": "<одна фраза по-русски: о чём слайд>"}'
    )


PROMPT = build_prompt(DEFAULT_SCHOOLS)

COLUMNS = {
    "post_id": "ID поста", "post_url": "Ссылка на пост", "slide": "№ слайда", "file": "Файл",
    "text": "Текст со слайда", "schools": "Школы", "context": "Контекст", "comparison": "Сравнение школ",
    "summary": "О чём слайд", "model": "Модель", "seconds": "Секунд", "error": "Ошибка",
}


def load_key():
    key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        f = Path(__file__).with_name("openrouter_key.txt")
        if f.exists():
            key = f.read_text(encoding="utf-8").strip()
    return key


def ask_model(key, base_url, model, image: Path, timeout=120, retries=3, prompt=None):
    import requests
    prompt = prompt or PROMPT
    mime = "image/png" if image.suffix.lower() == ".png" else ("image/webp" if image.suffix.lower() == ".webp" else "image/jpeg")
    b64 = base64.b64encode(image.read_bytes()).decode()
    body = {"model": model, "temperature": 0, "max_tokens": 1500,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{base_url}/chat/completions", json=body, timeout=timeout,
                              headers={"Authorization": f"Bearer {key}", "HTTP-Referer": "https://github.com/dapiskarevada-dot/teamproject",
                                       "X-Title": "umschool-tiktok-ocr"})
            if r.status_code in (429, 500, 502, 503):
                last = f"HTTP {r.status_code}: {r.text[:200]}"; time.sleep(3 * (attempt + 1)); continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:200]}"; time.sleep(2 * (attempt + 1))
    raise RuntimeError(last or "unknown error")


def parse_json(raw: str):
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"text": raw.strip(), "schools": [], "context": "", "comparison": None, "summary": ""}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"text": raw.strip(), "schools": [], "context": "", "comparison": None, "summary": ""}
    d.setdefault("text", ""); d.setdefault("schools", []); d.setdefault("context", "")
    d.setdefault("comparison", None); d.setdefault("summary", "")
    if isinstance(d["schools"], str):
        d["schools"] = [x.strip() for x in re.split(r"[,;]", d["schools"]) if x.strip()]
    return d


def post_meta(post_dir: Path):
    m = post_dir / "images_manifest.json"
    if m.exists():
        try:
            j = json.loads(m.read_text(encoding="utf-8"))
            return j.get("source_url", ""), j.get("username", "")
        except Exception:
            pass
    return "", ""


def write_tables(rows, out_dir: Path, model: str):
    keys = list(COLUMNS)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "images_text.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(COLUMNS[k] for k in keys)
        for r in rows:
            w.writerow(("; ".join(r[k]) if isinstance(r.get(k), list) else r.get(k)) for k in keys)
    # per-post aggregation
    posts = {}
    for r in rows:
        p = posts.setdefault(r["post_id"], {"post_id": r["post_id"], "post_url": r["post_url"], "slides": 0,
                                            "texts": [], "schools": set(), "contexts": [], "comparison": False, "errors": 0})
        p["slides"] += 1
        if r.get("error"):
            p["errors"] += 1; continue
        if r.get("text"):
            p["texts"].append(f"[{r['slide']}] {r['text']}")
        p["schools"].update(r.get("schools") or [])
        if r.get("context"):
            p["contexts"].append(r["context"])
        p["comparison"] = p["comparison"] or bool(r.get("comparison"))
    prow = [{"ID поста": p["post_id"], "Ссылка на пост": p["post_url"], "Слайдов": p["slides"],
             "Текст со всех слайдов": "\n".join(p["texts"]), "Школы": "; ".join(sorted(p["schools"])),
             "Контексты": "; ".join(sorted(set(p["contexts"]))), "Есть сравнение школ": p["comparison"],
             "Упомянут Умскул": any("умскул" in s.lower() or "umschool" in s.lower() for s in p["schools"]),
             "Слайдов с ошибкой": p["errors"], "Модель": model} for p in posts.values()]
    with (out_dir / "posts_text.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(prow[0].keys()) if prow else ["ID поста"]); w.writeheader(); w.writerows(prow)
    try:
        import pandas as pd
        pd.DataFrame([{COLUMNS[k]: ("; ".join(r[k]) if isinstance(r.get(k), list) else r.get(k)) for k in keys} for r in rows]
                     ).to_excel(out_dir / "images_text.xlsx", index=False)
        pd.DataFrame(prow).to_excel(out_dir / "posts_text.xlsx", index=False)
    except Exception as exc:
        print("XLSX не записан:", exc)
    return [out_dir / "images_text.xlsx", out_dir / "posts_text.xlsx"]


def school_mentioned(schools_found, school):
    """True, если среди найденных школ есть целевая (учитывая синонимы через запятую в schools.txt)."""
    aliases = [a.strip().lower() for a in school.split(",") if a.strip()]
    for s in schools_found or []:
        sl = str(s).lower()
        if any(a in sl or sl in a for a in aliases):
            return True
    return False


def transcribe(root: Path, model=DEFAULT_MODEL, base_url=DEFAULT_BASE, post_ids=None, limit=None,
               force=False, schools=None, key=None, quiet=False):
    """
    Распознаёт все слайды в root/raw/posts/*/images, пишет таблицы и возвращает (rows, per_post),
    где per_post = {post_id: {"slides", "text", "schools", "contexts", "comparison", "errors"}}.
    """
    key = key or load_key()
    if not key:
        raise RuntimeError("Нет ключа API: openrouter_key.txt или OPENROUTER_API_KEY")
    schools = schools or load_schools()
    prompt = build_prompt(schools)
    posts_dir = root / "raw" / "posts"
    images = []
    for d in sorted(posts_dir.glob("*")):
        if post_ids and d.name not in post_ids:
            continue
        for f in sorted((d / "images").glob("*")):
            if f.suffix.lower() in IMAGE_EXT:
                images.append((d, f))
    if limit:
        images = images[:limit]
    if not quiet:
        print(f"Картинок: {len(images)} | модель: {model}")
    rows = []
    tag = re.sub(r"[^\w.-]", "_", model)
    for post_dir, img in images:
        url, _ = post_meta(post_dir)
        cache = img.with_name(f"{img.stem}.{tag}.vlm.json")
        row = {"post_id": post_dir.name, "post_url": url, "slide": int(img.name[:3]) + 1 if img.name[:3].isdigit() else "",
               "file": str(img), "model": model, "seconds": None, "error": ""}
        if cache.exists() and not force:
            d = json.loads(cache.read_text(encoding="utf-8")); row.update(d["parsed"]); row["seconds"] = d.get("seconds")
            if not quiet: print(f"  cached  {img.name}  | {str(row.get('text',''))[:70]}")
        else:
            t0 = time.time()
            try:
                raw = ask_model(key, base_url, model, img, prompt=prompt)
                parsed = parse_json(raw)
                row.update(parsed); row["seconds"] = round(time.time() - t0, 1)
                cache.write_text(json.dumps({"model": model, "raw": raw, "parsed": parsed, "seconds": row["seconds"]},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
                if not quiet: print(f"  {row['seconds']:>5}s  {img.name}  | {str(parsed.get('text',''))[:70]}")
            except Exception as exc:
                row["error"] = str(exc)[:300]; row["seconds"] = round(time.time() - t0, 1)
                print(f"  ERROR  {img.name}: {row['error'][:120]}")
        rows.append(row)
    per_post = {}
    for r in rows:
        p = per_post.setdefault(r["post_id"], {"slides": 0, "texts": [], "schools": set(), "contexts": [], "comparison": False, "errors": 0})
        p["slides"] += 1
        if r.get("error"):
            p["errors"] += 1; continue
        if r.get("text"): p["texts"].append(f"[{r['slide']}] {r['text']}")
        p["schools"].update(r.get("schools") or [])
        if r.get("context"): p["contexts"].append(r["context"])
        p["comparison"] = p["comparison"] or bool(r.get("comparison"))
    for p in per_post.values():
        p["text"] = "\n".join(p.pop("texts")); p["schools"] = sorted(p["schools"]); p["contexts"] = sorted(set(p["contexts"]))
    if rows:
        write_tables(rows, root, model)
    return rows, per_post


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("cases/tiktok_media"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--post", action="append", default=[], help="только эти post_id")
    ap.add_argument("--limit", type=int, help="не больше N картинок (для теста)")
    ap.add_argument("--force", action="store_true", help="переспросить модель даже если есть .vlm.json")
    a = ap.parse_args()

    try:
        rows, per_post = transcribe(a.root, a.model, a.base_url, post_ids=a.post or None, limit=a.limit, force=a.force)
    except RuntimeError as exc:
        print(exc); return 2
    if not rows:
        print("Картинок нет в", a.root / "raw" / "posts"); return 1
    ok = sum(1 for r in rows if not r["error"])
    print(f"\nГотово: {ok}/{len(rows)} картинок распознано")
    print("TABLE:", a.root / "images_text.xlsx"); print("TABLE:", a.root / "posts_text.xlsx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
