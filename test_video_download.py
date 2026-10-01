#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Проверка скачивания видео: берёт несколько видео-постов из уже собранного и качает их через браузер.

    python test_video_download.py            # 3 ролика из cases/*/search/search_posts_*.json
    python test_video_download.py --n 5 --url https://www.tiktok.com/@user/video/123
"""
import argparse
import asyncio
import glob
import json
import re
from pathlib import Path


async def main_async(a):
    from pytok.tiktok import PyTok
    from pytok.accounts import AccountsPool
    from tiktok_fields import unwrap_item
    from video_download import download_video

    urls = list(a.url)
    if not urls:
        seen = set()
        for f in sorted(glob.glob("cases/*/search/search_posts_*.json")):
            for p in json.load(open(f, encoding="utf-8")).get("posts", []):
                u = p.get("canonical_url", "")
                if "/video/" in u and u not in seen:
                    seen.add(u); urls.append(u)
        urls = urls[: a.n]
    if not urls:
        print("Нет видео-постов: укажите --url"); return 1
    out_root = Path("cases") / "tiktok_media" / "raw" / "posts"
    ok = 0
    async with await PyTok.from_pool(AccountsPool(), request_delay=2) as api:
        for u in urls:
            m = re.search(r"@([^/]+)/video/(\d+)", u)
            if not m:
                continue
            user, pid = m.group(1), m.group(2)
            print(f"\n{u}")
            try:
                item = unwrap_item(await api.video(id=pid, username=user).info())
                path = await download_video(api, u, item, out_root / pid)
                if path:
                    ok += 1; print("  OK:", path)
                else:
                    print("  НЕ СКАЧАЛОСЬ")
            except Exception as exc:
                print("  ОШИБКА:", type(exc).__name__, str(exc)[:200])
    print(f"\nСкачано {ok} из {len(urls)}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--url", action="append", default=[])
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
