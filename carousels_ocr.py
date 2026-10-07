#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Текст с каруселей (фото-постов) через Gemini 2.5 Flash (OpenRouter) — по файлам постов new_posts_mac*.csv.

Слайды берутся сначала из cases/tiktok_media/raw/posts/<id>/images (что уже скачал сбор на этом Маке);
каких нет — скачиваются заново по ссылке на пост (tikwm, запасной путь — страница TikTok). Аккаунт TikTok не нужен.
Ответы Gemini кэшируются рядом с картинкой (<слайд>.<модель>.vlm.json, как у ocr_vlm.py) — повторно не оплачиваются.

    python carousels_ocr.py                                   # все server/new_posts_mac*.csv
    python carousels_ocr.py server/new_posts_mac_masha.csv    # один файл
    python carousels_ocr.py --limit 5                         # проба на 5 каруселях

Ключ: openrouter_key.txt рядом со скриптом (как для ocr_vlm.py).
Выход: КАРУСЕЛИ_ТЕКСТ/карусели_текст.jsonl (журнал; повторный запуск продолжает с места)
       КАРУСЕЛИ_ТЕКСТ/карусели_текст.csv   (одна строка на карусель)
Слайды: cases/carousels/<post_id>/slide_NN.jpg
"""
from __future__ import annotations

import argparse, csv, glob, json, re, shutil, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "КАРУСЕЛИ_ТЕКСТ"
SLIDES = ROOT / "cases" / "carousels"
JL = OUT / "карусели_текст.jsonl"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
csv.field_size_limit(10 ** 9)

STOP = threading.Event()        # кончились деньги на OpenRouter (402) — останавливаемся
_tik_lock = threading.Lock(); _tik_last = [0.0]


def http_get(url, timeout=60):
    try:
        from curl_cffi import requests as cr      # маскировка под Chrome, если установлена
        r = cr.get(url, impersonate="chrome", timeout=timeout)
    except ImportError:
        import requests
        r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.tiktok.com/"}, timeout=timeout)
    r.raise_for_status()
    return r


def slide_urls_tikwm(url):
    with _tik_lock:                                  # tikwm без ключа: не чаще 1 запроса в секунду
        w = 1.1 - (time.time() - _tik_last[0])
        if w > 0:
            time.sleep(w)
        _tik_last[0] = time.time()
    import urllib.parse
    d = http_get("https://www.tikwm.com/api/?" + urllib.parse.urlencode({"url": url})).json()
    return [u for u in ((d.get("data") or {}).get("images") or []) if u]


def slide_urls_tiktok(url):
    html = http_get(url).text
    m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    d = json.loads(m.group(1))
    item = (((d.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}).get("itemInfo") or {}).get("itemStruct") or {}
    return [((im.get("imageURL") or {}).get("urlList") or [""])[0] for im in ((item.get("imagePost") or {}).get("images") or [])]


def to_jpeg_if_needed(f: Path) -> Path:
    head = f.read_bytes()[:16]
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        if head[:4] == b"RIFF":
            g = f.with_suffix(".webp"); f.rename(g); return g
        if head[:4] == b"\x89PNG":
            g = f.with_suffix(".png"); f.rename(g); return g
        return f
    # HEIC и прочее — в jpeg встроенной утилитой macOS
    if shutil.which("sips"):
        g = f.with_name(f.stem + "_conv.jpg")
        subprocess.run(["sips", "-s", "format", "jpeg", str(f), "--out", str(g)], capture_output=True)
        if g.exists() and g.stat().st_size:
            return g
    return f


IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def get_slides(pid, url):
    # 1) картинки, которые уже скачал сбор на этом Маке (cases/tiktok_media/raw/posts/<id>/images)
    local = ROOT / "cases" / "tiktok_media" / "raw" / "posts" / pid / "images"
    if local.is_dir():
        have = sorted(p for p in local.iterdir() if p.suffix.lower() in IMG_EXT)
        if have:
            return have, "с Мака"
    # 2) скачанные этим скриптом раньше; 3) скачать заново
    folder = SLIDES / pid
    have = sorted(p for p in folder.glob("slide_*") if p.suffix.lower() in (".jpg", ".png", ".webp")) if folder.exists() else []
    if have:
        return have, "кэш"
    urls, src, errs = [], "", []
    for name, fn in (("tikwm", slide_urls_tikwm), ("tiktok", slide_urls_tiktok)):
        try:
            urls = fn(url)
        except Exception as exc:
            errs.append(f"{name}: {type(exc).__name__}")
            continue
        if urls:
            src = name; break
    if not urls:
        raise RuntimeError("слайды не получены (" + ", ".join(errs or ["пусто"]) + ")")
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, u in enumerate(urls[:35]):
        f = folder / f"slide_{i:02d}.jpg"
        f.write_bytes(http_get(u).content)
        paths.append(to_jpeg_if_needed(f))
    return paths, src


def process(row, prompt, key, model, base):
    from ocr_vlm import ask_model, parse_json
    pid, url = row["post_id"], row["url"]
    try:
        slides, src = get_slides(pid, url)
    except Exception as exc:
        return {"post_id": pid, "url": url, "error": str(exc)[:200]}
    texts, schools, contexts, comparison, summaries, errs = [], [], [], False, [], 0
    for i, f in enumerate(slides, 1):
        if STOP.is_set():
            return None
        tag = re.sub(r"[^\w.-]", "_", model)
        cache = f.with_name(f"{f.stem}.{tag}.vlm.json")   # тот же кэш, что у ocr_vlm.py
        try:
            if cache.exists():
                d = json.loads(cache.read_text(encoding="utf-8"))["parsed"]
            else:
                raw = ask_model(key, base, model, f, prompt=prompt)
                d = parse_json(raw)
                cache.write_text(json.dumps({"model": model, "raw": raw, "parsed": d}, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            if "402" in str(exc):
                STOP.set(); return None
            errs += 1; continue
        t = (d.get("text") or "").strip()
        if t:
            texts.append(f"[{i}] {t}")
        schools += [s for s in d.get("schools") or [] if s and s not in schools]
        if d.get("context") and d["context"] not in contexts:
            contexts.append(d["context"])
        comparison = comparison or bool(d.get("comparison"))
        if d.get("summary"):
            summaries.append(f"[{i}] {d['summary']}")
    rec = {"post_id": pid, "url": url, "author": row.get("author_username", ""), "slides": len(slides), "slides_from": src,
           "text": "\n".join(texts), "schools_gemini": schools, "contexts": contexts, "comparison": comparison,
           "summary": "\n".join(summaries), "model": model, "slide_errors": errs}
    if slides and errs == len(slides):
        rec["error"] = "Gemini не ответил ни на один слайд"
    return rec


def write_csv():
    from school_match import find_schools
    recs = {}
    for line in JL.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line); recs[r["post_id"]] = r          # последняя запись по посту побеждает
        except Exception:
            pass
    cols = ["ID поста", "Ссылка на пост", "Автор", "Слайдов", "Текст со всех слайдов", "Школы (Gemini)", "Школы (словарь)",
            "Контекст", "Есть сравнение школ", "О чём слайды", "Ошибка"]
    with open(OUT / "карусели_текст.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(cols)
        for r in recs.values():
            hs = find_schools({"слайды": r.get("text") or ""}) if r.get("text") else []
            w.writerow([r["post_id"], r.get("url", ""), r.get("author", ""), r.get("slides", ""), r.get("text", ""),
                        "; ".join(r.get("schools_gemini") or []),
                        "; ".join(dict.fromkeys(h["school"] for h in hs if h["kind"] != "author")),
                        "; ".join(r.get("contexts") or []), "да" if r.get("comparison") else "", r.get("summary", ""),
                        r.get("error", "")])
    ok = sum(1 for r in recs.values() if not r.get("error"))
    print(f"\nТаблица: {OUT.name}/карусели_текст.csv — каруселей {len(recs)}, распознано {ok}, "
          f"с текстом {sum(1 for r in recs.values() if r.get('text'))}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="csv с постами (по умолчанию server/new_posts_mac*.csv)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-errors", action="store_true", help="повторить карусели, которые раньше не получились")
    ap.add_argument("--model", default="", help="по умолчанию google/gemini-2.5-flash; дешевле в ~8 раз: google/gemini-2.5-flash-lite")
    a = ap.parse_args()
    from ocr_vlm import load_key, load_schools, build_prompt, DEFAULT_MODEL, DEFAULT_BASE
    key = load_key()
    if not key:
        sys.exit("Нет ключа: положите openrouter_key.txt в папку проекта (рядом с ocr_vlm.py) и запустите снова.")
    files = a.files or sorted(glob.glob(str(ROOT / "server" / "new_posts_mac*.csv")))
    if not files:
        sys.exit("Не нашла csv с постами (server/new_posts_mac*.csv).")
    rows = {}
    for fn in files:
        n = 0
        for r in csv.DictReader(open(fn, encoding="utf-8-sig")):
            pid = (r.get("post_id") or "").strip(); url = (r.get("url") or "").strip()
            if pid and (r.get("post_type") == "photo" or "/photo/" in url) and pid not in rows:
                rows[pid] = {**r, "post_id": pid, "url": url or f"https://www.tiktok.com/@{r.get('author_username', '')}/photo/{pid}"}
                n += 1
        print(f"{Path(fn).name}: каруселей {n}", flush=True)
    OUT.mkdir(exist_ok=True)
    done, failed = set(), set()
    if JL.exists():
        for line in JL.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            (failed if r.get("error") else done).add(r["post_id"])
    skip = done | (set() if a.retry_errors else failed - done)
    todo = [r for pid, r in rows.items() if pid not in skip]
    if a.limit:
        todo = todo[: a.limit]
    print(f"Каруселей всего {len(rows)}, уже готово {len(done & set(rows))}, не получилось раньше {len((failed - done) & set(rows))}, "
          f"к обработке {len(todo)} | модель {a.model or DEFAULT_MODEL}", flush=True)
    prompt = build_prompt(load_schools())
    model = a.model or DEFAULT_MODEL
    n = ok = 0; t0 = time.monotonic(); lock = threading.Lock()
    with open(JL, "a", encoding="utf-8") as fout, ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(process, r, prompt, key, model, DEFAULT_BASE) for r in todo]
        for fut in as_completed(futs):
            rec = fut.result()
            if rec is None:
                continue
            with lock:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n"); fout.flush()
                n += 1; ok += not rec.get("error")
                if n % 10 == 0 or n == len(todo):
                    rate = n / max(1e-6, (time.monotonic() - t0) / 60)
                    left = (len(todo) - n) / max(rate, 1e-6)
                    print(f"[{n}/{len(todo)}] {rate:.0f}/мин, осталось ~{left:.0f} мин | ок {ok} | "
                          f"{(rec.get('text') or rec.get('error') or '')[:70].replace(chr(10), ' / ')}", flush=True)
            if STOP.is_set():
                for f in futs:
                    f.cancel()
    if STOP.is_set():
        print("\n!!! OpenRouter ответил 402 — закончились деньги на балансе. Пополните https://openrouter.ai/settings/credits "
              "и запустите снова: готовое не потеряется, продолжит с места.", flush=True)
    write_csv()


if __name__ == "__main__":
    main()
