#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА СЕРВЕРЕ: комментарии + ответы к постам через yt-dlp (мобильный API TikTok, без аккаунтов и браузера).

Какие посты: comment_plan.csv (make_comment_list.py --all: уровни A → B → C → D), иначе comment_urls.txt,
иначе все из label_input.jsonl.gz. Идём строго по порядку плана, чтобы важное собралось первым.
Лимиты на пост зависят от уровня: «верхних комментариев / записей всего (с ответами) / секунд»:
    A, B = 200/1000/240    C = 100/400/120    D = 30/150/60
    поменять: --limits "A=300/2000/300,D=20/100/45"
Без дублей: пост пишется целиком (через временный файл) и только потом попадает в журнал — перезапуск его пропустит;
--skip-ids comments_done_mac_ids.txt — пропустить посты, комментарии к которым уже собраны на Маке;
в итоговой таблице дубли убираются по ID комментария.
    --only-tiers A,B  — только эти уровни;  --limit N — первые N постов.

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


DEFAULT_LIMITS = {"A": (200, 1000, 240), "B": (200, 1000, 240), "C": (100, 400, 120), "D": (30, 150, 60), "": (100, 300, 90)}


def parse_limits(spec):
    lim = dict(DEFAULT_LIMITS)
    for part in filter(None, (x.strip() for x in (spec or "").split(","))):
        t, v = part.split("=")
        lim[t.strip().upper()] = tuple(int(x) for x in v.split("/"))
    return lim


def targets(limit, only=None):
    """[(post_id, url, tier)] в порядке приоритета."""
    plan = HERE / "comment_plan.csv"
    if plan.exists():
        import csv
        out = [(r["post_id"], r["url"], r.get("tier", "")) for r in csv.DictReader(open(plan, encoding="utf-8"))]
        if only:
            out = [x for x in out if x[2] in only]
        return (out[:limit] if limit else out), "comment_plan.csv"
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
            out.append((m.group(1), u, ""))
    return (out[:limit] if limit else out), src


def fetch(pid, url, a, lim):
    """Комментарии одного поста с ограничениями. Возвращает (список, статус)."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "getcomments": False}
    if a.impersonate:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget.from_str(a.impersonate)
    max_roots, max_total, max_sec = lim
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
                    if roots >= max_roots:
                        status = f"лимит {max_roots} верхних"; break
                    roots += 1
                got.append(c)
                if len(got) >= max_total:
                    status = f"лимит {max_total} всего"; break
                if time.monotonic() - t0 > max_sec:
                    status = f"лимит {max_sec} с"; break
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
    url_by = {p: u for p, u, _ in posts}
    tier_by = {p: t for p, _, t in posts}
    for f in sorted(CDIR.glob("*.jsonl")):
        pid = f.stem
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                c = json.loads(line)
            except Exception:
                continue
            parent = c.get("parent")
            ts = c.get("timestamp")
            rows.append({"ID поста": pid, "Ссылка на пост": url_by.get(pid, ""), "Уровень поста": tier_by.get(pid, ""),
                         "Уровень": "комментарий" if parent in (None, "root", pid) else "ответ",
                         "ID комментария": c.get("id"), "ID родительского": "" if parent in (None, "root", pid) else parent,
                         "Автор (ник)": (c.get("author_url") or "").rstrip("/").split("@")[-1] or c.get("author"),
                         "Автор (имя)": c.get("author"), "ID автора": c.get("author_id"),
                         "Время (UTC)": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) if ts else "",
                         "Текст": c.get("text"), "Лайки": c.get("like_count"),
                         "Автор поста лайкнул": c.get("is_favorited"), "Ответ автора поста": c.get("author_is_uploader")})
    df = pd.DataFrame(rows)
    if len(df):
        df = df.drop_duplicates(subset=["ID комментария"])
    df.to_csv(HERE / "КОММЕНТАРИИ_сервер.csv", index=False, encoding="utf-8-sig")
    if len(df) < 1_000_000:
        for c in df.columns:
            if df[c].dtype == object:
                df[c] = df[c].map(lambda x: x[:32000] if isinstance(x, str) else x)
        df.to_excel(HERE / "КОММЕНТАРИИ_сервер.xlsx", index=False)
    else:
        print("строк больше миллиона — xlsx не делаю (Excel не вместит), есть csv", flush=True)
    print(f"КОММЕНТАРИИ_сервер.csv/.xlsx: {len(df)} строк без дублей, постов {df['ID поста'].nunique() if len(df) else 0}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--limits", default="", help='например "A=300/2000/300,D=20/100/45" (верхних/всего/секунд)')
    ap.add_argument("--only-tiers", default="", help="например A,B")
    ap.add_argument("--skip-ids", default="comments_done_mac_ids.txt", help="файл с post_id, которые уже собраны (пропустить)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sleep", type=float, default=1.0, help="пауза между постами в каждом потоке, сек")
    ap.add_argument("--impersonate", default="chrome")
    ap.add_argument("--xlsx-only", action="store_true")
    a = ap.parse_args()
    CDIR.mkdir(parents=True, exist_ok=True)
    lims = parse_limits(a.limits)
    only = {x.strip().upper() for x in a.only_tiers.split(",") if x.strip()} or None
    posts, src = targets(a.limit, only)
    if not a.xlsx_only:
        done = set()
        if LOG.exists():
            for line in LOG.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    if not any(x in str(r.get("status", "")) for x in ("ошибка после 0", "нет метода")):
                        done.add(r["post_id"])
                except Exception:
                    pass
        skip = set()
        sf = HERE / a.skip_ids
        if a.skip_ids and sf.exists():
            skip = {l.strip() for l in sf.read_text(encoding="utf-8").splitlines() if l.strip()}
        seen, todo = set(), []
        for p, u, t in posts:
            if p in done or p in skip or p in seen:
                continue
            seen.add(p); todo.append((p, u, t))
        from collections import Counter
        print(f"Постов ({src}): {len(posts)}, уже собрано на сервере {len(done & {p for p, _, _ in posts})}, "
              f"собрано на Маке (пропуск) {len(skip & {p for p, _, _ in posts})}, в работе {len(todo)} {dict(Counter(t for _, _, t in todo))}\n"
              f"лимиты (верхних/всего/сек): " + ", ".join(f"{k}={'/'.join(map(str, v))}" for k, v in lims.items() if k), flush=True)
        lock = threading.Lock(); n = tot = 0; t0 = time.monotonic(); fails = 0

        def one(item):
            pid, url, tier = item
            got, status = fetch(pid, url, a, lims.get(tier, lims[""]))
            if got:   # сначала во временный файл, потом переименование: недописанный пост не считается готовым
                tmp = CDIR / f"{pid}.jsonl.part"
                ids = set()
                with open(tmp, "w", encoding="utf-8") as f:
                    for c in got:
                        if c.get("id") in ids:
                            continue
                        ids.add(c.get("id"))
                        f.write(json.dumps(c, ensure_ascii=False) + "\n")
                tmp.replace(CDIR / f"{pid}.jsonl")
            time.sleep(a.sleep)
            return pid, len(got), f"{tier}: {status}" if tier else status

        with open(LOG, "a", encoding="utf-8") as log, ThreadPoolExecutor(a.workers) as ex:
            for fut in as_completed([ex.submit(one, it) for it in todo]):
                pid, k, status = fut.result()
                with lock:
                    log.write(json.dumps({"post_id": pid, "n": k, "status": status}, ensure_ascii=False) + "\n"); log.flush()
                    n += 1; tot += k
                    fails = fails + 1 if "ошибка после 0" in status else 0
                    if n % 20 == 0 or n == len(todo) or n <= 5:
                        print(f"[{n}/{len(todo)}] {n / max(1e-6, (time.monotonic() - t0) / 60):.0f} постов/мин | комментариев {tot} | "
                              f"{pid}: {k} ({status})", flush=True)
                    if fails >= 30:
                        print("30 ошибок подряд — TikTok не отдаёт комментарии этому IP. Останавливаюсь.", flush=True)
                        ex.shutdown(wait=False, cancel_futures=True); break
    build_xlsx(posts)


if __name__ == "__main__":
    main()
