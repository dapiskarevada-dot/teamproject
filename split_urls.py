#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Разбивает уникальные посты из search_posts_<run>.json на N файлов urls_part_N.txt (по кругу)."""
import argparse, json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("posts_json", type=Path)
ap.add_argument("--parts", type=int, default=4)
ap.add_argument("--out-dir", type=Path, default=Path("parallel"))
ap.add_argument("--skip-done", action="store_true", help="пропустить посты, у которых уже есть успешный thread_*_summary.json")
a = ap.parse_args()

posts = json.loads(a.posts_json.read_text(encoding="utf-8"))["posts"]
urls = []
for p in posts:
    if a.skip_done:
        d = Path("cases/tiktok_search_threads/raw/posts") / str(p["post_id"])
        if any(json.loads(s.read_text(encoding="utf-8")).get("status") == "success" for s in d.glob("thread_*_summary.json")):
            continue
    urls.append(p["canonical_url"])

a.out_dir.mkdir(parents=True, exist_ok=True)
for f in a.out_dir.glob("urls_part_*.txt"):
    f.unlink()
parts = [urls[i::a.parts] for i in range(a.parts)]
for i, chunk in enumerate(parts, 1):
    (a.out_dir / f"urls_part_{i}.txt").write_text("\n".join(chunk) + "\n", encoding="utf-8")
    print(f"urls_part_{i}.txt: {len(chunk)} постов")
print("Всего:", len(urls))
