#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Скачивание видео TikTok через открытый браузер PyTok (когда прямой httpx-запрос к CDN даёт 403).

Три способа по очереди:
  1. page.request.get(playAddr)  — запрос из сетевого стека браузера с его куками и Referer.
  2. Перехват (route): открываем страницу ролика, плеер сам запрашивает видео, мы забираем
     полный ответ (без Range) — это точная копия настоящего запроса браузера.
  3. yt-dlp с куками браузера (cookies.txt).
Возвращает путь к сохранённому файлу или None.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlparse

REFERER = "https://www.tiktok.com/"


def looks_like_video(body) -> bool:
    return isinstance(body, (bytes, bytearray)) and len(body) > 20_000 and body[4:8] == b"ftyp"


def play_urls(item: dict) -> list[str]:
    v = (item or {}).get("video") or {}
    urls = [v.get("playAddr"), v.get("downloadAddr")]
    for br in v.get("bitrateInfo") or []:
        for u in ((br.get("PlayAddr") or {}).get("UrlList") or []):
            urls.append(u)
    return [u for i, u in enumerate(urls) if u and u not in urls[:i]]


async def fetch_via_request(page, url: str, timeout: float = 120) -> bytes | None:
    try:
        r = await page.request.get(url, headers={"Referer": REFERER}, timeout=timeout * 1000)
        if r.ok:
            body = await r.body()
            if looks_like_video(body):
                return body
    except Exception:
        pass
    return None


async def fetch_via_route(api, post_url: str, urls: list[str], timeout: float = 90) -> bytes | None:
    """Открыть страницу ролика; когда плеер запросит видео, забрать полный ответ."""
    page = api._page
    ctx = page.context
    paths = {urlparse(u).path for u in urls}
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()

    def matches(url: str) -> bool:
        return urlparse(url).path in paths

    async def handler(route, request):
        try:
            headers = {k: v for k, v in request.headers.items() if k.lower() != "range"}
            resp = await route.fetch(headers=headers)
            body = await resp.body()
            if looks_like_video(body) and not fut.done():
                fut.set_result(body)
            await route.fulfill(response=resp, body=body)
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            try:
                await route.continue_()
            except Exception:
                pass

    await ctx.route(matches, handler)
    try:
        try:
            await api.navigate(post_url, wait_until="domcontentloaded")
        except Exception:
            pass
        # на всякий случай «потрогать» плеер, если автоплей выключен
        try:
            await page.evaluate("document.querySelector('video') && document.querySelector('video').play()")
        except Exception:
            pass
        return await asyncio.wait_for(fut, timeout=timeout)
    except Exception:
        return None
    finally:
        try:
            await ctx.unroute(matches, handler)
        except Exception:
            pass


async def cookies_txt(page, path: Path) -> Path | None:
    """Куки браузера в формате Netscape для yt-dlp."""
    try:
        cookies = await page.context.cookies()
        lines = ["# Netscape HTTP Cookie File"]
        for c in cookies:
            dom = c.get("domain", "")
            lines.append("\t".join([
                dom, "TRUE" if dom.startswith(".") else "FALSE", c.get("path", "/"),
                "TRUE" if c.get("secure") else "FALSE", str(int(c.get("expires") or 0) if (c.get("expires") or 0) > 0 else 0),
                c.get("name", ""), c.get("value", ""),
            ]))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    except Exception:
        return None


def ytdlp_download(url: str, folder: Path, cookiefile: Path | None) -> Path | None:
    try:
        import yt_dlp
        folder.mkdir(parents=True, exist_ok=True)
        opts = {"format": "best", "outtmpl": str(folder / "video.%(ext)s"), "quiet": True, "no_warnings": True,
                "noplaylist": True, "http_headers": {"Referer": REFERER}}
        if cookiefile:
            opts["cookiefile"] = str(cookiefile)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))
    except Exception as exc:
        print("  yt-dlp:", str(exc)[:160])
        return None


async def download_video(api, post_url: str, item: dict, post_dir: Path, timeout: float = 120) -> Path | None:
    """Полная цепочка. Сохраняет post_dir/video.mp4 и возвращает путь, либо None."""
    page = api._page
    urls = play_urls(item)
    post_dir.mkdir(parents=True, exist_ok=True)
    out = post_dir / "video.mp4"

    for u in urls[:2]:
        data = await fetch_via_request(page, u, timeout)
        if data:
            out.write_bytes(data); print(f"    saved {len(data) // 1024} KB (browser request)")
            return out

    if urls:
        data = await fetch_via_route(api, post_url, urls, timeout=min(timeout, 90))
        if data:
            out.write_bytes(data); print(f"    saved {len(data) // 1024} KB (player capture)")
            return out

    ck = await cookies_txt(page, post_dir.parent.parent / "cookies.txt")
    path = await asyncio.to_thread(ytdlp_download, post_url, post_dir, ck)
    if path and path.exists() and path.stat().st_size > 20_000:
        print(f"    saved {path.stat().st_size // 1024} KB (yt-dlp + cookies)")
        return path
    return None
