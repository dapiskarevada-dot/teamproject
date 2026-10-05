#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА СЕРВЕРЕ: комментарии + ответы к постам через yt-dlp (мобильный API TikTok, без аккаунтов и браузера).

Ограничения на пост (чтобы не утонуть в вирусных роликах):
    --roots 100        не больше N комментариев верхнего уровня
    --max-per-post 300 не больше N записей всего (комменты + ответы)
    --timeout 90       не больше N секунд на пост
Какие посты: comment_urls.txt (приоритет из make_comment_list.py), иначе все из label_input.jsonl.gz
(посты с упоминанием школы, сначала сравнения и самые просматриваемые). --limit N — первые N постов.

    python server_comments.py --limit 5                    # проба
    nohup python server_comments.py > comments.log 2>&1 &  # всё
    python server_comments.py --xlsx-only                  # пересобрать таблицу

Выход: out/comments/<post_id>.jsonl, out/comments_done.jsonl (журнал), КОММЕНТАРИИ_сервер.xlsx. Перезапуск продолжает.
"""
from __future__ import annotations

import argparse, gzip, json, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
CDIR = OUT / "comments"
LOG = OUT / "comments_done.jsonl"


def targets(limit):
    f = HERE / "comment_urls.txt"
    if f.exists():
        urls = [u.strip() for u in f.read_text(encoding="utf-8").splitlines() if u.strip()]
        src = "comment_urls.txt"
    else:
        urls = [json.loads(l)["url"] for l in gzip.open(HERE / "label_input.jsonl.gz", "rt", encoding="utf-8")]
        src = "label_input.jsonl.gz"
    out = []
    for u in urls:
        m = re.search(r"/(?:video|photo)/(\d+)", u)
        if m:
            out.append((m.group(1), u))
    return (out[:limit] if limit else out), src


def fetch(pid, url, a, ydl_lock=threading.Lock()):
    """Комментарии одного поста с ограничениями. Возвращает (список, статус)."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "getcomments": False}
    if a.impersonate:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget.from_str(a.impersonate)
    got, roots, t0 = [], 0, time.monotonic()
    with yt_dlp.YoutubeDL(opts) as ydl:
        ie = ydl.get_info_extractor("TikTok")
        gen = None
        for name in ("_get_comments", "_comment_iter"):
            if hasattr(ie, name):
                try:
                    gen = getattr(ie, name)(pid)
                    break
                except TypeError:
                    continue
        if gen is None:      # запасной путь: все комментарии разом, потом обрезка
            try:
                ydl.params["getcomments"] = True
                info = ydl.extract_info(url, download=False)
                gen = iter(info.get("comments") or [])
            except Exception as exc:
                return [], f"ошибка после 0: {type(exc).__name__}: {str(exc)[:120]}"
        status = "ok"
        try:
            for c in gen:
                if not isinstance(c, dict):
                    continue
                is_root = c.get("parent") in (None, "root", pid)
                if is_root:
                    if roots >= a.roots:
                        status = f"лимит {a.roots} верхних"; break
                    roots += 1
                got.append(c)
                if len(got) >= a.max_per_post:
                    status = f"лимит {a.max_per_post} всего"; break
                if time.monotonic() - t0 > a.timeout:
                    status = f"лимит {a.timeout} с"; break
        except Exception as exc:
            status = f"ошибка после {len(got)}: {type(exc).__name__}: {str(exc)[:120]}"
        finally:
            try:
                gen.close()
            except Exception:
                pass
    return got, status


def build_xlsx(posts):
    import pandas as pd
    rows = []
    url_by = dict(posts)
    for f in sorted(CDIR.glob("*.jsonl")):
        pid = f.stem
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                c = json.loads(line)
            except Exception:
                continue
            parent = c.get("parent")
            ts = c.get("timestamp")
            rows.append({"ID поста": pid, "Ссылка на пост": url_by.get(pid, ""),
                         "Уровень": "комментарий" if parent in (None, "root", pid) else "ответ",
                         "ID комментария": c.get("id"), "ID родительского": "" if parent in (None, "root", pid) else parent,
                         "Автор (ник)": (c.get("author_url") or "").rstrip("/").split("@")[-1] or c.get("author"),
                         "Автор (имя)": c.get("author"), "ID автора": c.get("author_id"),
                         "Время (UTC)": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) if ts else "",
                         "Текст": c.get("text"), "Лайки": c.get("like_count"),
                         "Автор поста лайкнул": c.get("is_favorited"), "Ответ автора поста": c.get("author_is_uploader")})
    df = pd.DataFrame(rows)
    df.to_excel(HERE / "КОММЕНТАРИИ_сервер.xlsx", index=False)
    print(f"КОММЕНТАРИИ_сервер.xlsx: {len(df)} строк, постов {df['ID поста'].nunique() if len(df) else 0}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--roots", type=int, default=100)
    ap.add_argument("--max-per-post", type=int, default=300)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sleep", type=float, default=1.0, help="пауза между постами в каждом потоке, сек")
    ap.add_argument("--impersonate", default="chrome")
    ap.add_argument("--xlsx-only", action="store_true")
    a = ap.parse_args()
    CDIR.mkdir(parents=True, exist_ok=True)
    posts, src = targets(a.limit)
    if not a.xlsx_only:
        done = set()
        if LOG.exists():
            for line in LOG.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    if not str(r.get("status", "")).startswith(("ошибка после 0", "нет метода")):
                        done.add(r["post_id"])
                except Exception:
                    pass
        todo = [(p, u) for p, u in posts if p not in done]
        print(f"Постов ({src}): {len(posts)}, уже собрано {len(posts) - len(todo)}, в работе {len(todo)} | "
              f"лимиты: {a.roots} верхних, {a.max_per_post} всего, {a.timeout} с на пост", flush=True)
        lock = threading.Lock(); n = tot = 0; t0 = time.monotonic(); fails = 0

        def one(item):
            pid, url = item
            got, status = fetch(pid, url, a)
            if got:
                with open(CDIR / f"{pid}.jsonl", "w", encoding="utf-8") as f:
                    for c in got:
                        f.write(json.dumps(c, ensure_ascii=False) + "\n")
            time.sleep(a.sleep)
            return pid, len(got), status

        with open(LOG, "a", encoding="utf-8") as log, ThreadPoolExecutor(a.workers) as ex:
            for fut in as_completed([ex.submit(one, it) for it in todo]):
                pid, k, status = fut.result()
                with lock:
                    log.write(json.dumps({"post_id": pid, "n": k, "status": status}, ensure_ascii=False) + "\n"); log.flush()
                    n += 1; tot += k
                    fails = fails + 1 if status.startswith("ошибка после 0") else 0
                    if n % 20 == 0 or n == len(todo) or n <= 5:
                        print(f"[{n}/{len(todo)}] {n / max(1e-6, (time.monotonic() - t0) / 60):.0f} постов/мин | комментариев {tot} | "
                              f"{pid}: {k} ({status})", flush=True)
                    if fails >= 30:
                        print("30 ошибок подряд — TikTok не отдаёт комментарии этому IP. Останавливаюсь.", flush=True)
                        ex.shutdown(wait=False, cancel_futures=True); break
    build_xlsx(posts)


if __name__ == "__main__":
    main()
