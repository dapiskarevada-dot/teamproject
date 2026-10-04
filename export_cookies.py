#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Экспорт куки залогиненного браузера pytok в cases/tiktok_media/cookies.txt (формат Netscape) — чтобы yt-dlp
качал видео без браузера. Открывает браузер на ~30 секунд и закрывает."""
import asyncio, sys
from pathlib import Path

async def main():
    from pytok.tiktok import PyTok
    from pytok.accounts import AccountsPool
    from video_download import cookies_txt
    out = Path("cases") / "tiktok_media" / "cookies.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    async with await PyTok.from_pool(AccountsPool(), request_delay=1) as api:
        try:
            await api.navigate("https://www.tiktok.com/foryou", wait_until="domcontentloaded")
            await asyncio.sleep(5)
        except Exception:
            pass
        p = await cookies_txt(api._page, out)
    if p:
        n = sum(1 for l in p.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#"))
        print(f"Куки сохранены: {p} ({n} шт.)"); return 0
    print("Не удалось сохранить куки"); return 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
