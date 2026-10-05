#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""НА MAC, после ПЕРЕПИСЬ_НОВЫЕ.command: посты всех школ из plan_new_schools.txt -> server/new_posts_mac.csv
(формат new_schools.py). На сервере new_schools.py подхватит этот файл сам и сделает расшифровку + комментарии."""
import csv, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
from pipeline import load_plan, merged_posts, slug, CASES_ROOT

FIELDS = ["post_id", "url", "post_type", "author_username", "author_nickname", "create_time", "description",
          "play_count", "digg_count", "comment_count", "share_count", "collect_count", "duration",
          "music_title", "music_original", "found_by", "schools_in_text", "images"]
rows = {}
for s in load_plan(ROOT / "plan_new_schools.txt"):
    name = s["name"].split(",")[0].strip()
    posts = merged_posts(CASES_ROOT / slug(s["name"]))
    print(f"{name}: {len(posts)} постов")
    for pid, f in posts.items():
        pid = str(pid)
        lab = f"{name} (Мак): " + ", ".join((f.get("matched_queries") or [])[:3])
        if pid in rows:
            rows[pid]["found_by"] += "; " + lab; continue
        url = f.get("video_url") or f"https://www.tiktok.com/@{f.get('author_username')}/video/{pid}"
        rows[pid] = {"post_id": pid, "url": url, "post_type": "photo" if "/photo/" in url or str(f.get("post_type")).lower().startswith(("photo", "карус", "image")) else "video",
                     "author_username": f.get("author_username"), "author_nickname": f.get("author_nickname"),
                     "create_time": f.get("create_time"), "description": f.get("description"),
                     "play_count": f.get("play_count"), "digg_count": f.get("like_count"), "comment_count": f.get("comment_count"),
                     "share_count": f.get("share_count"), "collect_count": f.get("collect_count"), "duration": f.get("duration_sec"),
                     "music_title": f.get("music_title"), "music_original": "", "found_by": lab, "schools_in_text": "", "images": ""}
out = ROOT / "server" / "new_posts_mac.csv"
with open(out, "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, FIELDS); w.writeheader(); w.writerows(rows.values())
print(f"Всего {len(rows)} постов -> {out}  (загрузить на под в /teamproject/server/)")
