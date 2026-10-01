#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Ищет фото-посты (карусели) в лентах хэштегов и пишет photo_urls.txt.
    python find_carousels_hashtag.py --tag умскул --tag егэ --count 150 --limit 15
"""
import argparse, asyncio, json
from pathlib import Path

def item_dict(v):
    for attr in ("as_dict", "_data", "data"):
        x = getattr(v, attr, None)
        x = x() if callable(x) else x
        if isinstance(x, dict) and x: return x
    return {}

def is_photo(d):
    if any(isinstance(d.get(k), dict) and d.get(k) for k in ("imagePost", "image_post", "image_post_info")): return True
    return str(d.get("awemeType") or d.get("aweme_type") or "") in {"150", "image", "photo"}

async def main(a):
    from pytok.tiktok import PyTok
    from pytok.accounts import AccountsPool
    found, seen, stats = [], set(), {}
    async with await PyTok.from_pool(AccountsPool(), request_delay=3) as api:
        for tag in a.tag:
            n = ph = 0
            try:
                async for v in api.hashtag(name=tag).videos(count=a.count):
                    d = item_dict(v); n += 1
                    pid = str(d.get("id") or getattr(v, "id", "") or "")
                    user = (d.get("author") or {}).get("uniqueId") or getattr(v, "username", "")
                    if is_photo(d) and pid and user and pid not in seen:
                        seen.add(pid); ph += 1
                        found.append({"tag": tag, "post_id": pid, "username": user,
                                      "url": f"https://www.tiktok.com/@{user}/photo/{pid}",
                                      "desc": (d.get("desc") or "")[:120],
                                      "images": len(((d.get("imagePost") or {}).get("images")) or [])})
                        print(f"  photo  {pid} @{user} | {found[-1]['images']} img | {found[-1]['desc'][:60]}")
                    if len(found) >= a.limit: break
            except Exception as exc:
                print(f"#{tag}: STOP {type(exc).__name__}: {exc}")
            stats[tag] = (n, ph); print(f"#{tag}: просмотрено {n}, фото-постов {ph}")
            if len(found) >= a.limit: break
    Path("photo_urls.txt").write_text("\n".join(f["url"] for f in found) + ("\n" if found else ""), encoding="utf-8")
    Path("cases").mkdir(exist_ok=True)
    Path("cases/carousels_found.json").write_text(json.dumps({"stats": stats, "found": found}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Итого фото-постов: {len(found)} -> photo_urls.txt")
    return 0 if found else 1

ap = argparse.ArgumentParser()
ap.add_argument("--tag", action="append", default=[])
ap.add_argument("--count", type=int, default=150)
ap.add_argument("--limit", type=int, default=15)
a = ap.parse_args()
a.tag = a.tag or ["умскул", "umschool", "егэ"]
raise SystemExit(asyncio.run(main(a)))
