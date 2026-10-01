#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Выбирает фото-посты (карусели) из результатов поиска и пишет photo_urls.txt
для collect_tiktok_images.py.

    python pick_photo_posts.py                      # из всех search_posts_*.json
    python pick_photo_posts.py --limit 15
"""

import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--search-dir", type=Path, default=Path("cases/tiktok_search_threads/search"))
    ap.add_argument("--out", type=Path, default=Path("photo_urls.txt"))
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    urls, seen = [], set()
    for f in sorted(a.search_dir.glob("search_posts_*.json")):
        for p in json.loads(f.read_text(encoding="utf-8")).get("posts", []):
            if p.get("post_type_inferred") == "photo" and p["post_id"] not in seen:
                seen.add(p["post_id"])
                urls.append(p["canonical_url"])
    if a.limit:
        urls = urls[: a.limit]
    a.out.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
    print(f"Фото-постов: {len(urls)} -> {a.out}")
    for u in urls[:10]:
        print(" ", u)
    return 0 if urls else 1


if __name__ == "__main__":
    raise SystemExit(main())
