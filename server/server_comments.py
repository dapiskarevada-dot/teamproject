#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА СЕРВЕРЕ: комментарии + ответы к постам без аккаунтов: веб-API TikTok (curl_cffi), запасной — tikwm.
    python server_comments.py --probe <ссылка>   # проверить, какой источник работает

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


# ---- источники комментариев (yt-dlp больше не умеет комментарии TikTok) ----
_tikwm_lock = threading.Lock()
_tikwm_last = [0.0]
_sess = threading.local()


def _session(imp):
    if not hasattr(_sess, "s"):
        from curl_cffi import requests as cr
        _sess.s = cr.Session(impersonate=imp or "chrome")
    return _sess.s


def _get_json(url, a, params, headers=None):
    r = _session(a.impersonate).get(url, params=params, headers=headers or {}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    try:
        return r.json()
    except Exception:
        raise RuntimeError(f"не JSON ({len(r.text)} байт)")


WEB_H = {"Referer": "https://www.tiktok.com/", "Accept": "application/json, text/plain, */*"}


def _web_page(a, pid, cursor, cid=None):
    if cid:
        j = _get_json("https://www.tiktok.com/api/comment/list/reply/", a,
                      {"aid": "1988", "item_id": pid, "comment_id": cid, "count": 50, "cursor": cursor}, WEB_H)
    else:
        j = _get_json("https://www.tiktok.com/api/comment/list/", a,
                      {"aid": "1988", "aweme_id": pid, "count": 50, "cursor": cursor}, WEB_H)
    if "comments" not in j:
        raise RuntimeError(f"нет comments, status_code={j.get('status_code')} {str(j.get('status_msg', ''))[:60]}")
    return j.get("comments") or [], int(j.get("cursor") or 0), bool(j.get("has_more"))


def _tikwm_page(a, pid, cursor, cid=None):
    with _tikwm_lock:      # бесплатный tikwm: ~1 запрос в секунду
        w = 1.1 - (time.monotonic() - _tikwm_last[0])
        if w > 0:
            time.sleep(w)
        _tikwm_last[0] = time.monotonic()
    if cid:
        j = _get_json("https://www.tikwm.com/api/comment/reply", a, {"video_id": pid, "comment_id": cid, "count": 50, "cursor": cursor})
    else:
        j = _get_json("https://www.tikwm.com/api/comment/list", a, {"url": pid, "count": 50, "cursor": cursor})
    if j.get("code") != 0:
        raise RuntimeError(f"tikwm: {str(j.get('msg'))[:80]}")
    d = j.get("data") or {}
    return d.get("comments") or [], int(d.get("cursor") or 0), bool(d.get("hasMore") or d.get("has_more"))


def _norm(c, pid, parent):
    u = c.get("user") or {}
    uid = u.get("unique_id") or u.get("uniqueId") or ""
    return {"id": str(c.get("cid") or c.get("id") or c.get("comment_id") or ""), "parent": parent,
            "text": c.get("text"), "timestamp": c.get("create_time"),
            "like_count": c.get("digg_count"), "reply_count": c.get("reply_comment_total") or c.get("reply_total"),
            "author": u.get("nickname"), "author_id": u.get("uid") or u.get("id"),
            "author_url": f"https://www.tiktok.com/@{uid}" if uid else "",
            "is_favorited": bool(c.get("is_author_digged")), "author_is_uploader": None}


def fetch(pid, url, a, lim):
    """Комментарии одного поста с ограничениями. Возвращает (список, статус)."""
    BIG = 10 ** 9      # 0 = без ограничения
    max_roots, max_total, max_sec = (x if x else BIG for x in lim)
    order = {"web": ["web"], "tikwm": ["tikwm"]}.get(a.source, ["web", "tikwm"])
    errors = []
    for src in order:
        page = _web_page if src == "web" else _tikwm_page
        got, t0, status = [], time.monotonic(), "ok"
        try:
            roots, cursor, more = [], 0, True
            while more and len(roots) < max_roots:
                prev = cursor
                cs, cursor, more = page(a, pid, cursor)
                if not cs or cursor == prev:
                    break
                roots += [_norm(c, pid, "root") for c in cs]
                if time.monotonic() - t0 > max_sec:
                    status = f"лимит {max_sec} с"; break
            if len(roots) >= max_roots:
                status = f"лимит {max_roots} верхних"
            got = roots[:max_roots][:max_total]
            # ответы: сначала к самым залайканным веткам; ветки качаются параллельно (--reply-threads)
            glock = threading.Lock()
            bad = []

            def thread(r):
                cursor, more = 0, True
                try:
                    while more and len(got) < max_total and time.monotonic() - t0 <= max_sec:
                        prev = cursor
                        cs, cursor, more = page(a, pid, cursor, r["id"])
                        if not cs or cursor == prev:
                            break
                        with glock:
                            got.extend([_norm(c, pid, r["id"]) for c in cs][: max(0, max_total - len(got))])
                except Exception as exc:
                    bad.append(f"{type(exc).__name__}: {str(exc)[:60]}")

            todo_r = [r for r in sorted(got[:], key=lambda x: -(x.get("like_count") or 0)) if r.get("reply_count")]
            nthr = a.reply_threads if src == "web" else 1
            if nthr > 1 and len(todo_r) > 1:
                with ThreadPoolExecutor(min(nthr, len(todo_r))) as rex:
                    list(rex.map(thread, todo_r))
            else:
                for r in todo_r:
                    if len(got) >= max_total or time.monotonic() - t0 > max_sec:
                        break
                    thread(r)
            if bad:
                return got, f"{src} неполный (ответы: {len(bad)} веток с ошибкой, {bad[0]})"
            if len(got) >= max_total:
                status = f"лимит {max_total} всего"
            elif time.monotonic() - t0 > max_sec:
                status = f"лимит {max_sec} с"
            return got, f"{src} {status}"
        except Exception as exc:
            if got or src == order[-1]:
                tail = "; ".join(errors + [f"{src}: {type(exc).__name__}: {str(exc)[:100]}"])
                return got, (f"{src} ошибка после {len(got)}: " + tail) if got else f"ошибка после 0: {tail}"
            errors.append(f"{src}: {str(exc)[:80]}")
    return [], "ошибка после 0: нет источника"


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
                         "Текст": c.get("text"), "Лайки": c.get("like_count"), "Ответов (по данным TikTok)": c.get("reply_count"),
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
    ap.add_argument("--reply-threads", type=int, default=6, help="сколько веток ответов одного поста качать параллельно")
    ap.add_argument("--all-comments", action="store_true",
                    help="все комментарии и все ответы со всех постов, без лимитов; посты, собранные раньше с лимитом, догружаются")
    ap.add_argument("--limits", default="", help='например "A=300/2000/300,D=20/100/45" (верхних/всего/секунд)')
    ap.add_argument("--only-tiers", default="", help="например A,B")
    ap.add_argument("--skip-ids", default="comments_done_mac_ids.txt", help="файл с post_id, которые уже собраны (пропустить)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sleep", type=float, default=0.0, help="пауза между постами в каждом потоке, сек")
    ap.add_argument("--impersonate", default="chrome")
    ap.add_argument("--source", default="auto", choices=["auto", "web", "tikwm"],
                    help="auto: веб-API TikTok, при ошибке — tikwm (медленный, ~1 запрос/с)")
    ap.add_argument("--probe", default="", help="ссылка или ID поста: проверить источники и выйти")
    ap.add_argument("--xlsx-only", action="store_true")
    a = ap.parse_args()
    CDIR.mkdir(parents=True, exist_ok=True)
    if a.probe:
        import re as _re
        m = _re.search(r"(\d{15,})", a.probe); pid = m.group(1) if m else a.probe
        for src in ("web", "tikwm"):
            a.source = src
            got, st = fetch(pid, a.probe, a, (20, 40, 60))
            print(f"{src}: {len(got)} комментариев | {st} | пример: {(got[0].get('text') or '')[:60] if got else '-'}", flush=True)
        return
    lims = parse_limits(a.limits)
    if a.all_comments:
        lims = {k: (0, 0, 0) for k in lims}
    only = {x.strip().upper() for x in a.only_tiers.split(",") if x.strip()} or None
    posts, src = targets(a.limit, only)
    if not a.xlsx_only:
        done = set()
        if LOG.exists():
            for line in LOG.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    if not any(x in str(r.get("status", "")) for x in ("ошибка после 0", "нет метода")):
                        if a.all_comments and any(x in str(r.get("status", "")) for x in ("лимит", "неполный")):
                            done.discard(r["post_id"]); continue   # был обрезан лимитом — собрать заново целиком
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
              + ("лимитов нет: все комментарии и ответы" if a.all_comments else "лимиты (верхних/всего/сек): " + ", ".join(f"{k}={'/'.join(map(str, v))}" for k, v in lims.items() if k)), flush=True)
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
