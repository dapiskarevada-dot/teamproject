#!/usr/bin/env python
# -*- coding: utf-8 -*-

r"""
Collect image assets from TikTok posts using the existing PyTok browser session.

Designed primarily for /photo/ posts. It can also save large post images found
on ordinary /video/ pages when present.

Important:
- This script does NOT do OCR.
- It saves original image bytes first.
- It deduplicates by SHA256.
- It writes a manifest with source URL, DOM evidence, dimensions and hashes.
- No automatic retries.

Examples:
    python .\collect_tiktok_images.py --url "https://www.tiktok.com/@user/photo/123"
    python .\collect_tiktok_images.py --url "URL1" --url "URL2"
    python .\collect_tiktok_images.py --urls-file .\urls.txt
    python .\collect_tiktok_images.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_OUT = Path("cases") / "tiktok_media"
DEFAULT_REQUEST_DELAY = 4.0
DEFAULT_PAGE_WAIT = 6.0
DEFAULT_BETWEEN_POSTS = 8.0
DEFAULT_MIN_WIDTH = 300
DEFAULT_MIN_HEIGHT = 300
DEFAULT_MAX_IMAGES = 30


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def json_dump(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(path)


def parse_url(url: str):
    url = url.strip()
    p = urlparse(url)
    m = re.search(r"/@([^/]+)/(photo|video)/(\d+)", p.path, re.I)
    if not m:
        raise ValueError(
            "Expected TikTok URL like https://www.tiktok.com/@name/photo/123 "
            "or /video/123"
        )
    username, post_type, post_id = m.group(1), m.group(2).lower(), m.group(3)
    return {
        "username": username,
        "post_type": post_type,
        "post_id": post_id,
        "url": f"https://www.tiktok.com/@{username}/{post_type}/{post_id}",
    }


def gather_urls(args):
    urls = []
    for u in args.url or []:
        if u.strip():
            urls.append(u.strip())
    if args.urls_file:
        with args.urls_file.open("r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)

    out, seen = [], set()
    for u in urls:
        canon = parse_url(u)["url"]
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_ext(data: bytes, content_type: str | None, url: str) -> str:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    known = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
    }
    if ct in known:
        return known[ct]
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and b"WEBP" in data[:16]:
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".img"


def download(url: str, referer: str, timeout: int = 30):
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type")


async def download_in_browser(page, url: str, timeout_ms: int = 30000):
    """
    Fetch the asset from inside the page (same cookies/headers/VPN as the
    browser). Direct Python downloads fail on machines where the CDN is
    unreachable outside the browser (DNS / VPN certificate issues).
    """
    import base64
    res = await page.evaluate(
        r"""async ([url, timeoutMs]) => {
          const ctrl = new AbortController();
          const t = setTimeout(() => ctrl.abort(), timeoutMs);
          try {
            const r = await fetch(url, {credentials: 'include', signal: ctrl.signal});
            const buf = await r.arrayBuffer();
            let bin = ''; const bytes = new Uint8Array(buf);
            for (let i = 0; i < bytes.length; i += 0x8000)
              bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
            return {ok: r.ok, status: r.status, ct: r.headers.get('content-type') || '', b64: btoa(bin)};
          } catch (e) { return {ok: false, status: 0, error: String(e)}; }
          finally { clearTimeout(t); }
        }""",
        [url, timeout_ms],
    )
    if not res or not res.get("ok"):
        raise RuntimeError(f"in-browser fetch failed: status={res.get('status') if res else None} {res.get('error', '') if res else ''}")
    return base64.b64decode(res["b64"]), res.get("ct") or ""


async def download_via_screenshot(page, url: str, max_side: int = 1400):
    """
    CORS-proof fallback: let the browser render the image in an injected <img>
    and screenshot that element. Returns PNG bytes (re-encoded, fine for OCR).
    """
    size = await page.evaluate(
        r"""async ([url, maxSide]) => {
          const old = document.getElementById('__ocr_img'); if (old) old.remove();
          const img = document.createElement('img');
          img.id = '__ocr_img'; img.decoding = 'sync';
          img.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483647;background:#fff;';
          const done = new Promise((res, rej) => { img.onload = () => res(true); img.onerror = () => rej(new Error('img load failed')); });
          img.src = url; document.body.appendChild(img);
          await done;
          const w = img.naturalWidth, h = img.naturalHeight;
          const k = Math.min(1, maxSide / Math.max(w, h), (window.innerWidth - 4) / w, (window.innerHeight - 4) / h);
          img.style.width = Math.floor(w * k) + 'px'; img.style.height = Math.floor(h * k) + 'px';
          return {w, h, k};
        }""",
        [url, max_side],
    )
    try:
        data = await page.locator("#__ocr_img").screenshot(type="png", timeout=15000)
    finally:
        try:
            await page.evaluate("() => { const e = document.getElementById('__ocr_img'); if (e) e.remove(); }")
        except Exception:
            pass
    return data, "image/png"


async def browser_image_candidates(page, min_width: int, min_height: int):
    script = r"""
    (() => {
      const items = [];
      const seen = new Set();

      const badContext = (el) => {
        const txt = [
          el.getAttribute('class') || '',
          el.getAttribute('data-e2e') || '',
          el.getAttribute('alt') || '',
          el.closest('[class]')?.getAttribute('class') || '',
          el.closest('[data-e2e]')?.getAttribute('data-e2e') || ''
        ].join(' ').toLowerCase();
        return /avatar|profile|emoji|icon|logo|comment|creator|suggest|recommend|ad-card|ads|banner/.test(txt);
      };

      const inPostArea = (el) => !!(
        el.closest('#main-content-video_detail') ||
        el.closest('[data-e2e="browse-photo"]') ||
        el.closest('[data-e2e*="photo"]') ||
        el.closest('[class*="Photo"]') ||
        el.closest('[class*="photo"]') ||
        el.closest('[class*="carousel"]') ||
        el.closest('[class*="Carousel"]') ||
        el.closest('main')
      );

      const add = (src, el, source) => {
        if (!src || !/^https?:\/\//i.test(src) || seen.has(src)) return;
        const r = el.getBoundingClientRect();
        const nw = Number(el.naturalWidth || 0);
        const nh = Number(el.naturalHeight || 0);
        const rw = Number(r.width || 0);
        const rh = Number(r.height || 0);
        if (badContext(el) || !inPostArea(el)) return;
        if (Math.max(nw, rw) < %MINW%) return;
        if (Math.max(nh, rh) < %MINH%) return;

        seen.add(src);
        items.push({
          url: src,
          source,
          natural_width: nw,
          natural_height: nh,
          rendered_width: rw,
          rendered_height: rh,
          alt: el.getAttribute('alt') || '',
          class_name: el.getAttribute('class') || '',
          data_e2e: el.getAttribute('data-e2e') || '',
          visible: !!(rw > 1 && rh > 1),
          in_main_content: !!el.closest('#main-content-video_detail'),
          in_photo_context: !!(
            el.closest('[data-e2e="browse-photo"]') ||
            el.closest('[data-e2e*="photo"]') ||
            el.closest('[class*="Photo"]') ||
            el.closest('[class*="photo"]') ||
            el.closest('[class*="carousel"]') ||
            el.closest('[class*="Carousel"]')
          )
        });
      };

      for (const img of document.querySelectorAll('img')) {
        add(img.currentSrc || img.src, img, 'img.currentSrc/src');
        const srcset = img.getAttribute('srcset');
        if (srcset) {
          for (const part of srcset.split(',')) {
            const u = part.trim().split(/\s+/)[0];
            if (u) add(u, img, 'img.srcset');
          }
        }
      }
      return items;
    })()
    """.replace("%MINW%", str(min_width)).replace("%MINH%", str(min_height))
    return await page.evaluate(script)


async def hydration_candidates(page):
    script = r"""
    (() => {
      const out = [];
      const seen = new Set();

      const looksImageUrl = (s) => {
        if (typeof s !== 'string' || !/^https?:\/\//i.test(s)) return false;
        const x = s.toLowerCase();
        const hostish = x.includes('tiktokcdn') || x.includes('byteimg') || x.includes('muscdn') || x.includes('p16-') || x.includes('p19-');
        const imageish = x.includes('image') || x.includes('photo') || x.includes('jpeg') || x.includes('.jpg') || x.includes('.png') || x.includes('.webp');
        return hostish && imageish;
      };

      const add = (url, path) => {
        if (!looksImageUrl(url) || seen.has(url)) return;
        seen.add(url);
        out.push({url, source: 'hydration', json_path: path});
      };

      const walk = (x, path, depth) => {
        if (depth > 14 || x == null) return;
        if (typeof x === 'string') { add(x, path); return; }
        if (Array.isArray(x)) {
          for (let i = 0; i < Math.min(x.length, 2000); i++) walk(x[i], path + '[' + i + ']', depth + 1);
          return;
        }
        if (typeof x === 'object') {
          let n = 0;
          for (const [k, v] of Object.entries(x)) {
            if (++n > 4000) break;
            walk(v, path ? path + '.' + k : k, depth + 1);
          }
        }
      };

      for (const sel of ['#__UNIVERSAL_DATA_FOR_REHYDRATION__', '#SIGI_STATE', 'script[type="application/json"]']) {
        for (const node of document.querySelectorAll(sel)) {
          const txt = node.textContent || '';
          if (!txt || txt.length < 2) continue;
          try { walk(JSON.parse(txt), sel, 0); } catch (_) {}
        }
      }
      return out;
    })()
    """
    return await page.evaluate(script)


def candidate_score(c):
    score = 0
    if c.get("in_photo_context"):
        score += 100
    if c.get("in_main_content"):
        score += 50
    if c.get("visible"):
        score += 20
    nw = int(c.get("natural_width") or 0)
    nh = int(c.get("natural_height") or 0)
    rw = float(c.get("rendered_width") or 0)
    rh = float(c.get("rendered_height") or 0)
    area = max(nw * nh, int(rw * rh))
    if area >= 500_000:
        score += 20
    elif area >= 200_000:
        score += 10
    if c.get("source") == "hydration":
        score += 5
    u = str(c.get("url") or "").lower()
    if "avatar" in u:
        score -= 200
    if "cropcenter:100" in u or "cropcenter:720" in u:
        score -= 100
    return score


async def collect_one(api, url: str, args):
    info = parse_url(url)
    pid = info["post_id"]
    post_root = args.out_dir / "raw" / "posts" / pid
    image_dir = post_root / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "post_id": pid,
        "username": info["username"],
        "post_type": info["post_type"],
        "source_url": info["url"],
        "observed_at_utc": utc_now(),
        "status": "error",
        "error": None,
        "page_url": None,
        "candidates_total": 0,
        "files_saved": [],
    }

    print("\n" + "=" * 76)
    print("POST:", info["url"])
    print("=" * 76)

    try:
        page = api._page
        # Current PyTok exposes a plain Playwright Page (no .get()); use PyTok's
        # own navigate() so its request tracking and delays stay in effect.
        await api.navigate(info["url"], wait_until="domcontentloaded")
        await asyncio.sleep(args.page_wait)
        manifest["page_url"] = page.url
        print("Browser:", page.url)

        dom = await browser_image_candidates(page, args.min_width, args.min_height)
        hyd = await hydration_candidates(page)

        # TikTok renders only the current slide (+/-1). Step through the
        # carousel with ArrowRight and collect the rendered <img> on each
        # step, so every slide is captured from the DOM (the DOM URLs are the
        # ones the browser can actually load).
        try:
            await page.keyboard.press("Escape")
            prev_urls = {c.get("url") for c in dom}
            stale_steps = 0
            for step in range(60):
                await page.keyboard.press("ArrowRight")
                await asyncio.sleep(1.2)
                more = await browser_image_candidates(page, args.min_width, args.min_height)
                new = [c for c in more if c.get("url") not in prev_urls]
                if new:
                    dom += new
                    prev_urls.update(c.get("url") for c in new)
                    stale_steps = 0
                else:
                    stale_steps += 1
                    if stale_steps >= 2:
                        break
            print(f"Carousel stepping: {step + 1} steps, DOM images {len(prev_urls)}")
        except Exception as exc:
            print("Carousel stepping failed:", type(exc).__name__, exc)

        merged = {}
        for c in hyd + dom:
            u = c.get("url")
            if not u:
                continue
            if u not in merged:
                merged[u] = c
            else:
                merged[u].update({k: v for k, v in c.items() if v not in (None, "", 0, False)})

        candidates = sorted(merged.values(), key=candidate_score, reverse=True)
        manifest["candidates_total"] = len(candidates)
        print("Candidates:", len(candidates))

        seen_hashes = set()
        saved = []

        for idx, c in enumerate(candidates, start=1):
            if len(saved) >= args.max_images:
                break
            u = c.get("url")
            if not u:
                continue
            try:
                try:
                    data, ct = await download_in_browser(page, u)
                except Exception as exc_b:
                    try:
                        data, ct = await download_via_screenshot(page, u)
                    except Exception as exc_s:
                        print(f"   browser fetch/screenshot {idx} failed ({type(exc_s).__name__}), trying direct download")
                        data, ct = await asyncio.to_thread(download, u, info["url"])
            except Exception as exc:
                print(f"skip download {idx}: {type(exc).__name__}: {exc}")
                continue

            if len(data) < 20_000:
                continue
            h = sha256_bytes(data)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            ext = sniff_ext(data, ct, u)
            filename = f"{len(saved):03d}_{h[:12]}{ext}"
            path = image_dir / filename
            path.write_bytes(data)

            rec = {
                "index": len(saved),
                "file": str(path),
                "filename": filename,
                "sha256": h,
                "bytes": len(data),
                "content_type": ct,
                "source_asset_url": u,
                "candidate_score": candidate_score(c),
                "evidence": c,
            }
            saved.append(rec)
            print(f"saved {len(saved):>2}: {filename} ({len(data)//1024} KB)")

        manifest["files_saved"] = saved
        manifest["status"] = "success"

    except Exception as exc:
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        print("STOP for this post; no automatic retry:", manifest["error"])

    manifest_path = post_root / "images_manifest.json"
    json_dump(manifest_path, manifest)
    print("MANIFEST:", manifest_path)
    print("Saved:", len(manifest["files_saved"]))
    return manifest


def check_local_api():
    from pytok.tiktok import PyTok
    from pytok.accounts import AccountsPool
    print("PyTok:", inspect.getfile(PyTok))
    print("AccountsPool:", inspect.getfile(AccountsPool))
    print("PyTok.from_pool:", inspect.signature(PyTok.from_pool))
    print("No TikTok request was made.")
    return 0


async def run(args):
    from pytok.tiktok import PyTok
    from pytok.accounts import AccountsPool

    urls = gather_urls(args)
    if not urls:
        raise ValueError("Provide --url or --urls-file")

    print("Posts:", len(urls))
    print("Request delay:", f"{args.request_delay:g}s")
    print("Page wait:", f"{args.page_wait:g}s")
    print("Between posts:", f"{args.between_posts:g}s")
    print("Automatic retries: OFF")

    pool = AccountsPool()
    results = []
    async with await PyTok.from_pool(pool, request_delay=args.request_delay) as api:
        for i, url in enumerate(urls):
            results.append(await collect_one(api, url, args))
            if i < len(urls) - 1 and args.between_posts > 0:
                print(f"Waiting {args.between_posts:g}s...")
                await asyncio.sleep(args.between_posts)

    batch_manifest = args.out_dir / f"batch_images_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    json_dump(batch_manifest, {"created_at_utc": utc_now(), "posts": results})
    print("\nBATCH:", batch_manifest)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--url", action="append")
    parser.add_argument("--urls-file", type=Path)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY)
    parser.add_argument("--page-wait", type=float, default=DEFAULT_PAGE_WAIT)
    parser.add_argument("--between-posts", type=float, default=DEFAULT_BETWEEN_POSTS)
    parser.add_argument("--min-width", type=int, default=DEFAULT_MIN_WIDTH)
    parser.add_argument("--min-height", type=int, default=DEFAULT_MIN_HEIGHT)
    parser.add_argument("--max-images", type=int, default=DEFAULT_MAX_IMAGES)
    args = parser.parse_args()

    if args.check:
        return check_local_api()
    if args.request_delay < 1:
        parser.error("--request-delay must be >= 1")
    if args.page_wait < 3:
        parser.error("--page-wait must be >= 3")
    if args.between_posts < 0:
        parser.error("--between-posts must be >= 0")
    if args.max_images < 1:
        parser.error("--max-images must be >= 1")

    gather_urls(args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
