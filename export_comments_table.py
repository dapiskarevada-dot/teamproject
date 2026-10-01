#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Таблица комментариев и реплаев из сырых JSONL-файлов сборщика.

Одна строка = один комментарий (верхнего уровня или ответ). Колонки на русском,
как в таблице постов. Запускается автоматически в конце
collect_tiktok_search_threads.py, либо вручную:

    python export_comments_table.py cases/tiktok_search_threads/search/search_threads_<run>_summary.json
    python export_comments_table.py --all cases/tiktok_search_threads   # все посты в папке raw/posts
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

COMMENT_COLUMNS = {
    "post_id": "ID поста",
    "post_url": "Ссылка на пост",
    "post_author": "Автор поста",
    "level": "Уровень",                       # 1 = комментарий, 2 = ответ
    "comment_id": "ID комментария",
    "parent_comment_id": "ID родительского комментария",
    "reply_to_reply_id": "ID комментария, на который отвечают",
    "author_username": "Автор (ник)",
    "author_nickname": "Автор (имя)",
    "author_id": "ID автора",
    "author_created": "Дата создания аккаунта (UTC)",
    "create_time": "Время комментария (UTC)",
    "text": "Текст",
    "like_count": "Лайки",
    "reply_count": "Ответов (по данным TikTok)",
    "replies_collected": "Ответов собрано",
    "is_author_digged": "Лайк автора поста",
    "author_pin": "Закреплён автором",
    "language": "Язык",
    "comment_url": "Ссылка на комментарий",
}


def ts(v):
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def account_created_from_id(uid):
    try:
        t = int(uid) >> 32
        return ts(t) if t >= 1_400_000_000 else ""
    except (TypeError, ValueError):
        return ""


def row_from_comment(c, post_id, post_url, post_author, level, parent_id=None, reply_to=None, collected=None):
    u = c.get("user") or {}
    cid = str(c.get("cid") or c.get("id") or "")
    return {
        "post_id": post_id,
        "post_url": post_url,
        "post_author": post_author,
        "level": level,
        "comment_id": cid,
        "parent_comment_id": parent_id or "",
        "reply_to_reply_id": (reply_to if reply_to not in (None, "0", 0) else ""),
        "author_username": u.get("unique_id") or u.get("uniqueId") or "",
        "author_nickname": u.get("nickname") or "",
        "author_id": str(u.get("uid") or u.get("id") or ""),
        "author_created": account_created_from_id(u.get("uid") or u.get("id")),
        "create_time": ts(c.get("create_time")),
        "text": c.get("text") or "",
        "like_count": c.get("digg_count"),
        "reply_count": c.get("reply_comment_total") if level == 1 else None,
        "replies_collected": collected if level == 1 else None,
        "is_author_digged": bool(c.get("is_author_digged")),
        "author_pin": bool(c.get("author_pin")),
        "language": c.get("comment_language") or "",
        "comment_url": f"{post_url}?comment_id={cid}" if post_url and cid else "",
    }


def rows_for_post(roots_path: Path, replies_path: Path, post_url="", post_author=""):
    rows = []
    roots = [json.loads(l) for l in roots_path.open(encoding="utf-8")] if roots_path.exists() else []
    replies = [json.loads(l) for l in replies_path.open(encoding="utf-8")] if replies_path.exists() else []
    by_parent = {}
    for r in replies:
        by_parent.setdefault(str(r.get("parent_comment_id")), []).append(r)
    for rec in roots:
        c = rec["comment"]
        post_id = str(rec.get("post_id") or c.get("aweme_id") or "")
        url = post_url or rec.get("source_url") or ""
        cid = str(rec.get("comment_id") or c.get("cid") or "")
        kids = by_parent.get(cid, [])
        rows.append(row_from_comment(c, post_id, url, post_author, 1, collected=len(kids)))
        for r in kids:
            rows.append(row_from_comment(r["comment"], post_id, url, post_author, 2,
                                         parent_id=cid, reply_to=r.get("reply_to_reply_id")))
    return rows


def write_tables(rows, out_base: Path):
    import csv
    keys = list(COMMENT_COLUMNS)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    csv_path = out_base.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COMMENT_COLUMNS[k] for k in keys)
        for r in rows:
            w.writerow(r.get(k) for k in keys)
    written = [csv_path]
    try:
        import pandas as pd
        df = pd.DataFrame([{COMMENT_COLUMNS[k]: r.get(k) for k in keys} for r in rows])
        xlsx = out_base.with_suffix(".xlsx")
        df.to_excel(xlsx, index=False)
        written.append(xlsx)
    except Exception as exc:
        print("XLSX не записан (нужны pandas и openpyxl):", exc)
    return written


def export_from_summary(summary_path: Path):
    """Таблица для одного прогона поиска (search_threads_<run>_summary.json)."""
    s = json.loads(summary_path.read_text(encoding="utf-8"))
    base = summary_path.parent.parent  # cases/tiktok_search_threads
    posts_meta = {}
    for p in sorted(summary_path.parent.glob("search_posts_*.json")):
        if summary_path.name.split("_summary")[0].replace("search_threads_", "") in p.name:
            for item in json.loads(p.read_text(encoding="utf-8")).get("posts", []):
                posts_meta[str(item["post_id"])] = item
    rows = []
    for ps in s.get("post_summaries", []):
        meta = posts_meta.get(str(ps["post_id"]), {})
        rows += rows_for_post(
            Path(ps["roots_jsonl"]) if Path(ps["roots_jsonl"]).is_absolute() else base.parent.parent / ps["roots_jsonl"],
            Path(ps["replies_jsonl"]) if Path(ps["replies_jsonl"]).is_absolute() else base.parent.parent / ps["replies_jsonl"],
            post_url=ps.get("source_url") or meta.get("canonical_url", ""),
            post_author=ps.get("username") or meta.get("username", ""),
        )
    run = summary_path.name.replace("search_threads_", "").replace("_summary.json", "")
    out = write_tables(rows, summary_path.parent / f"comments_{run}")
    return rows, out


def export_all(case_dir: Path):
    """Все посты из raw/posts (берётся самый свежий файл по каждому посту)."""
    rows = []
    for d in sorted((case_dir / "raw" / "posts").glob("*")):
        roots = sorted(d.glob("comments_*.jsonl"))
        if not roots:
            continue
        r = roots[-1]
        rep = d / r.name.replace("comments_", "replies_")
        summ = d / r.name.replace("comments_", "thread_").replace(".jsonl", "_summary.json")
        url = author = ""
        if summ.exists():
            sj = json.loads(summ.read_text(encoding="utf-8"))
            url, author = sj.get("source_url", ""), sj.get("username", "")
        rows += rows_for_post(r, rep, url, author)
    out = write_tables(rows, case_dir / "search" / "comments_all")
    return rows, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", type=Path, help="search_threads_<run>_summary.json или папка кейса при --all")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    rows, out = export_all(a.target) if a.all else export_from_summary(a.target)
    n1 = sum(r["level"] == 1 for r in rows)
    print(f"Комментариев: {n1}, реплаев: {len(rows) - n1}")
    for p in out:
        print("TABLE:", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
