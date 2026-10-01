#!/usr/bin/env python
# -*- coding: utf-8 -*-

r"""
Universal TikTok thread collector for current PyTok.

What it does
------------
- Works with TikTok /video/ and /photo/ URLs.
- Collects top-level comments FIRST.
- Applies the requested top-level limit BEFORE fetching replies.
  This avoids the previous bug where PyTok could inspect replies for extra roots
  that were loaded in the DOM but were outside --count.
- Then fetches replies only for the saved roots.
- Preserves raw comment/reply dicts, parent_comment_id and reply_to_reply_id.
- Supports one URL, several --url arguments, or --urls-file.
- No automatic retries.
- Conservative delays by default.

Examples
--------
One post:
    python .\collect_tiktok_threads.py --url "https://www.tiktok.com/@name/video/123" --count 100

Photo post:
    python .\collect_tiktok_threads.py --url "https://www.tiktok.com/@name/photo/123" --count 100

Several posts:
    python .\collect_tiktok_threads.py --url "URL1" --url "URL2" --count 100

Text file, one URL per line:
    python .\collect_tiktok_threads.py --urls-file .\urls.txt --count 100

Interface check only, no TikTok request:
    python .\collect_tiktok_threads.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from urllib.parse import urlparse


DEFAULT_CASE_DIR = Path("cases") / "tiktok_threads"
DEFAULT_REQUEST_DELAY = 4.0
DEFAULT_MIN_COMMENT_WAIT = 5.0
DEFAULT_MAX_COMMENT_WAIT = 15.0
DEFAULT_REPLY_ROOT_DELAY = 4.0
DEFAULT_BETWEEN_POSTS_DELAY = 8.0
DEFAULT_BATCH_SIZE = 50
DEFAULT_COUNT = 100


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def json_dump(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(path)


def parse_tiktok_url(url: str):
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    m = re.search(r"/@([^/]+)/(video|photo)/(\d+)$", path, re.IGNORECASE)
    if not m:
        raise ValueError(
            "Expected TikTok URL like "
            "https://www.tiktok.com/@name/video/123... "
            "or https://www.tiktok.com/@name/photo/123..."
        )
    username, post_type, post_id = m.group(1), m.group(2).lower(), m.group(3)
    canonical = f"https://www.tiktok.com/@{username}/{post_type}/{post_id}"
    return {
        "username": username,
        "post_type": post_type,
        "post_id": post_id,
        "canonical_url": canonical,
    }


def comment_id(comment: dict) -> str:
    return str(comment.get("cid") or comment.get("id") or "")


def comment_key(comment: dict) -> str:
    cid = comment_id(comment)
    return cid or json.dumps(comment, sort_keys=True, ensure_ascii=False, default=str)


def check_local_api():
    from pytok.tiktok import PyTok
    from pytok.api.video import Video
    from pytok.accounts import AccountsPool

    print("Installed PyTok:", inspect.getfile(PyTok))
    print("Video._get_comments_and_req:", inspect.signature(Video._get_comments_and_req))
    print("Video._get_api_comments:", inspect.signature(Video._get_api_comments))
    print("Video._get_scroll_comments:", inspect.signature(Video._get_scroll_comments))
    print("Video._get_comment_replies:", inspect.signature(Video._get_comment_replies))
    print("PyTok.from_pool:", inspect.signature(PyTok.from_pool))
    print("AccountsPool:", inspect.getfile(AccountsPool))

    required = (
        "_get_comments_and_req",
        "_get_api_comments",
        "_get_scroll_comments",
        "_get_comment_replies",
    )
    missing = [name for name in required if not hasattr(Video, name)]
    if missing:
        print("STOP: installed PyTok is missing:", ", ".join(missing))
        return 2

    print("Compatible interface found. No TikTok request was made.")
    return 0


async def page_snapshot(page):
    try:
        return await page.evaluate(
            r"""(() => ({
              url: location.href,
              title: document.title,
              videoDetailRoot: document.querySelectorAll('#main-content-video_detail').length,
              commentIcons: document.querySelectorAll('[data-e2e="comment-icon"]').length,
              level1: document.querySelectorAll('[data-e2e="comment-level-1"]').length,
              commentInputs: document.querySelectorAll('[data-e2e="comment-input"]').length,
              loginFields: document.querySelectorAll('input[type="password"]').length,
              bodyHasCaptcha: /captcha|verify to continue|security check/i.test(document.body?.innerText || '')
            }))()"""
        )
    except Exception as exc:
        return {"diagnostic_error": f"{type(exc).__name__}: {exc}"}


async def wait_for_comments(page, min_wait: float, max_wait: float, poll: float = 0.5):
    """
    Always wait min_wait after opening comments, then poll until comments appear
    or max_wait total has elapsed.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()

    await asyncio.sleep(min(min_wait, max_wait))
    last = await page_snapshot(page)

    while True:
        elapsed = loop.time() - started

        if int(last.get("loginFields") or 0) > 0 or bool(last.get("bodyHasCaptcha")):
            raise RuntimeError("Login/captcha/security check detected; stopping without retry")

        if int(last.get("level1") or 0) > 0:
            return last, elapsed

        remaining = max_wait - elapsed
        if remaining <= 0:
            return last, elapsed

        await asyncio.sleep(min(poll, remaining))
        last = await page_snapshot(page)


async def dismiss_cookie_banner(page):
    """Decline optional cookies in TikTok's shadow-DOM banner, or remove it."""
    try:
        return await page.evaluate(
            r"""(() => {
              const b = document.querySelector('tiktok-cookie-banner');
              if (!b) return 'absent';
              const root = b.shadowRoot || b;
              const btns = Array.from(root.querySelectorAll('button'));
              const pick = btns.find(x => /decline|отклон|reject|only necessary|refuse/i.test(x.textContent || ''))
                        || btns.find(x => /allow|accept|прин/i.test(x.textContent || ''));
              if (pick) { pick.click(); return 'clicked:' + (pick.textContent || '').trim().slice(0, 40); }
              b.remove(); return 'removed';
            })()"""
        )
    except Exception as exc:
        return f"error:{type(exc).__name__}"


REPLY_ENDPOINT = "https://www.tiktok.com/api/comment/list/reply/"


async def dump_dom_debug(page, debug_dir, tag):
    """Save a screenshot + a list of data-e2e elements and reply-like texts."""
    if debug_dir is None:
        return
    try:
        debug_dir = Path(debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)
        run = stamp()
        await page.screenshot(path=str(debug_dir / f"debug_{tag}_{run}.png"), full_page=False)
        info = await page.evaluate(
            r"""(() => {
              const e2e = Array.from(document.querySelectorAll('[data-e2e]')).map(e => ({
                e2e: e.getAttribute('data-e2e'), tag: e.tagName, text: (e.textContent || '').trim().slice(0, 60)}));
              const seen = {}; e2e.forEach(x => { seen[x.e2e] = (seen[x.e2e] || 0) + 1; });
              const texts = Array.from(document.querySelectorAll('p, span, button, div'))
                .map(e => ({tag: e.tagName, cls: (e.className || '').toString().slice(0, 80),
                            e2e: e.getAttribute('data-e2e'), text: (e.textContent || '').trim().slice(0, 60),
                            kids: e.children.length}))
                .filter(x => x.text && x.text.length <= 60 && x.kids <= 2 && /(repl|ответ)/i.test(x.text));
              return {url: location.href, e2e_counts: seen, reply_like: texts.slice(0, 40)};
            })()"""
        )
        json_dump(debug_dir / f"debug_{tag}_{run}.json", info)
        print("DOM debug saved to", debug_dir)
    except Exception as exc:
        print("DOM debug failed:", type(exc).__name__, exc)


async def prime_reply_template(post, wait_seconds: float = 4.0, debug_dir=None):
    """
    PyTok's signed make_request() needs a param template captured from a
    request the webapp itself issued for api/comment/list/reply. Click one
    'View N replies' control in the open comment panel so that happens.
    """
    api = getattr(post.parent, "tiktok_api", None)
    if api is None:
        return "no_api"
    try:
        if api.get_cached_api_params(REPLY_ENDPOINT) is not None:
            return "cached"
    except Exception:
        pass

    page = post.parent._page
    try:
        clicked = await page.evaluate(
            r"""(() => {
              // TikTok web (2026): "View N replies" is a <button class="TUXButton ...">
              // inside div[class*="DivViewRepliesContainer"]. Click the BUTTON itself.
              const rx = /^(view|посмотреть|показать|see)\b.*(repl|ответ|\d)/i;
              let cands = Array.from(document.querySelectorAll('[class*="DivViewRepliesContainer"] button, [class*="ViewReplies"] button, [data-e2e^="view-more"]'))
                .filter(e => rx.test((e.textContent || '').trim()));
              if (!cands.length) {
                cands = Array.from(document.querySelectorAll('button, [role="button"], p, span'))
                  .filter(e => { const t = (e.textContent || '').trim(); return t && t.length <= 60 && e.children.length <= 2 && rx.test(t); });
              }
              if (!cands.length) return {n: 0};
              const el = cands[0];
              el.scrollIntoView({block: 'center'});
              el.click();
              return {n: cands.length, tag: el.tagName, e2e: el.getAttribute('data-e2e'),
                      cls: (el.className || '').toString().slice(0, 60), text: (el.textContent || '').trim().slice(0, 60)};
            })()"""
        )
    except Exception as exc:
        return f"error:{type(exc).__name__}"
    print("   view-more candidate:", clicked)
    if not clicked or not clicked.get("n"):
        await dump_dom_debug(page, debug_dir, "no_view_more")
        return "no_view_more_control"
    await asyncio.sleep(wait_seconds)
    try:
        if api.get_cached_api_params(REPLY_ENDPOINT) is not None:
            return "captured"
        await dump_dom_debug(page, debug_dir, "clicked_not_captured")
        return "clicked_not_captured"
    except Exception:
        return "clicked"


async def fetch_replies_signed_then_legacy(post, root, batch_size):
    """Signed API route first (reliable), legacy cached-URL route as fallback."""
    declared = int(root.get("reply_comment_total") or 0)
    if hasattr(post, "_get_comment_replies_api"):
        try:
            await post._get_comment_replies_api(root, batch_size)
        except Exception as exc:
            print(f"   signed reply route failed: {type(exc).__name__}: {str(exc)[:120]}")
    if len(root.get("reply_comment") or []) < declared:
        await post._get_comment_replies(root, batch_size)


async def ensure_comment_panel(post, args):
    await post.view()
    page = post.parent._page

    before = await page_snapshot(page)
    print("Before comment panel:", before)

    if int(before.get("loginFields") or 0) > 0 or bool(before.get("bodyHasCaptcha")):
        raise RuntimeError("Login/captcha/security check detected; stopping without retry")

    if int(before.get("level1") or 0) > 0:
        return before

    # The TikTok cookie banner (<tiktok-cookie-banner>, shadow DOM) intercepts
    # pointer events on the action bar; dismiss it (decline optional cookies)
    # before clicking the comment icon.
    await dismiss_cookie_banner(page)

    # Current PyTok exposes a plain Playwright Page; use PyTok's own element
    # finder (same one it uses internally) instead of a page.select() wrapper.
    icon = await post._find_element_by_selector('[data-e2e="comment-icon"]', timeout=3)
    if not icon:
        icon = await page.query_selector('[data-e2e="comment-icon"]')
    if not icon:
        raise RuntimeError("Comment icon not found")

    try:
        await icon.click(timeout=3000)
    except Exception as exc:
        print("Normal click blocked, retrying with force:", type(exc).__name__)
        await dismiss_cookie_banner(page)
        try:
            await icon.click(timeout=3000, force=True)
        except Exception as exc2:
            print("Force click blocked too, using JS click:", type(exc2).__name__)
            await page.evaluate(
                r"""(() => { const e = document.querySelector('[data-e2e="comment-icon"]');
                             if (!e) return false; e.scrollIntoView({block:'center'}); e.click(); return true; })()"""
            )
    print(
        f"Clicked comment icon once; waiting at least {args.min_wait:g}s, "
        f"up to {args.max_wait:g}s"
    )

    after, waited = await wait_for_comments(
        page,
        min_wait=args.min_wait,
        max_wait=args.max_wait,
        poll=0.5,
    )
    print(f"Comment panel wait finished after {waited:.1f}s")
    print("After comment panel:", after)

    if int(after.get("level1") or 0) == 0:
        await dump_dom_debug(page, getattr(args, "debug_dir", None), "no_panel")
        raise RuntimeError(
            f"No top-level comment nodes became visible within {args.max_wait:g}s"
        )

    return after


def add_unique_root(roots, seen, comment, limit):
    if not isinstance(comment, dict):
        return False
    # Safety net: a reply carries reply_id = parent cid; top-level has "0".
    if str(comment.get("reply_id") or "0") != "0":
        return False
    key = comment_key(comment)
    if key in seen:
        return False
    if len(roots) >= limit:
        return False
    seen.add(key)
    roots.append(comment)
    return True


async def collect_roots_strict(post, count: int, batch_size: int):
    """
    Collect roots with fetch_replies=False, and hard-stop at count BEFORE replies.

    This is deliberately separate from reply fetching.
    """
    from pytok import exceptions

    roots = []
    seen = set()

    all_comments, processed_urls, finished = await post._get_comments_and_req(count)

    # IMPORTANT: PyTok may hand us more DOM comments than requested.
    # Slice/deduplicate NOW. No replies have been fetched yet.
    for comment in all_comments:
        add_unique_root(roots, seen, comment, count)

    if len(roots) >= count or finished:
        return roots[:count], {
            "initial_returned": len(all_comments),
            "initial_kept": len(roots[:count]),
            "continuation": "none",
            "finished": bool(finished),
        }

    continuation = "api"
    try:
        ids = {comment_id(c) for c in roots if comment_id(c)}
        async for comment in post._get_api_comments(
            count,
            batch_size,
            ids,
            fetch_replies=False,
        ):
            add_unique_root(roots, seen, comment, count)
            if len(roots) >= count:
                break

    except exceptions.ApiFailedException as exc:
        continuation = "scroll"
        print(
            "API continuation unavailable; using one PyTok scroll continuation:",
            f"{type(exc).__name__}: {str(exc)[:160]}",
        )
        async for comment in post._get_scroll_comments(
            count,
            len(roots),
            processed_urls,
            fetch_replies=False,
        ):
            add_unique_root(roots, seen, comment, count)
            if len(roots) >= count:
                break

    return roots[:count], {
        "initial_returned": len(all_comments),
        "initial_kept": min(len(roots), count),
        "continuation": continuation,
        "finished": bool(finished),
    }


async def fetch_replies_for_roots(post, roots, batch_size: int, root_delay: float, debug_dir=None):
    """
    Fetch replies ONLY for roots that will actually be saved.
    """
    diagnostics = {}
    reply_records = []
    seen_reply_ids = set()

    if any(int(r.get("reply_comment_total") or 0) > len(r.get("reply_comment") or []) for r in roots):
        print("Reply template priming:", await prime_reply_template(post, debug_dir=debug_dir))

    for i, root in enumerate(roots, start=1):
        cid = comment_id(root)
        declared = int(root.get("reply_comment_total") or 0)
        before = len(root.get("reply_comment") or [])

        diag = {
            "comment_id": cid,
            "declared_total": declared,
            "before": before,
            "after": before,
            "status": "not_needed" if declared <= before else "pending",
            "error": None,
        }
        diagnostics[cid] = diag

        if declared <= before:
            continue

        try:
            await fetch_replies_signed_then_legacy(post, root, batch_size)
        except Exception as exc:
            diag["status"] = "error"
            diag["error"] = f"{type(exc).__name__}: {exc}"
            print(f"root {i}/{len(roots)} {cid}: REPLY ERROR: {diag['error']}")
        else:
            replies = root.get("reply_comment") or []
            after = len(replies)
            diag["after"] = after

            if after >= declared:
                diag["status"] = "complete"
            elif after > before:
                diag["status"] = "partial"
            else:
                diag["status"] = "missing"

            print(
                f"root {i}/{len(roots)} {cid}: "
                f"{after}/{declared} replies ({diag['status']})"
            )

            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                rid = comment_id(reply)
                rkey = rid or json.dumps(
                    reply, sort_keys=True, ensure_ascii=False, default=str
                )
                if rkey in seen_reply_ids:
                    continue
                seen_reply_ids.add(rkey)

                reply_records.append({
                    "record_type": "reply",
                    "parent_comment_id": cid,
                    "reply_comment_id": rid or None,
                    "reply_to_reply_id": (
                        str(reply.get("reply_to_reply_id"))
                        if reply.get("reply_to_reply_id") is not None
                        else None
                    ),
                    "comment": reply,
                })

        # Deliberate pause between separate reply threads.
        if root_delay > 0:
            await asyncio.sleep(root_delay)

    return reply_records, diagnostics


async def collect_one(api, url: str, args):
    info = parse_tiktok_url(url)
    username = info["username"]
    post_type = info["post_type"]
    post_id = info["post_id"]
    canonical_url = info["canonical_url"]

    post_dir = args.case_dir / "raw" / "posts" / post_id
    post_dir.mkdir(parents=True, exist_ok=True)

    run = stamp()
    roots_path = post_dir / f"comments_{run}.jsonl"
    replies_path = post_dir / f"replies_{run}.jsonl"
    summary_path = post_dir / f"thread_{run}_summary.json"

    summary = {
        "post_id": post_id,
        "username": username,
        "post_type": post_type,
        "source_url": canonical_url,
        "started_at_utc": utc_now(),
        "finished_at_utc": None,
        "status": "error",
        "error": None,
        "requested_top_level": args.count,
        "received_top_level_unique": 0,
        "replies_collected_unique": 0,
        "roots_declaring_replies": 0,
        "roots_replies_complete": 0,
        "roots_replies_partial": 0,
        "roots_replies_missing": 0,
        "roots_reply_errors": 0,
        "request_delay_seconds": args.request_delay,
        "comment_wait_min_seconds": args.min_wait,
        "comment_wait_max_seconds": args.max_wait,
        "reply_root_delay_seconds": args.reply_root_delay,
        "batch_size": args.batch_size,
        "root_collection_diagnostic": None,
        "reply_diagnostics": {},
        "page_diagnostic": None,
        "roots_jsonl": str(roots_path),
        "replies_jsonl": str(replies_path),
        "scope": (
            "Top-level comments are limited by --count. Replies are fetched only "
            "for those saved roots. Completeness is checked against "
            "reply_comment_total returned by TikTok/PyTok."
        ),
    }

    print("\n" + "=" * 76)
    print("POST:", canonical_url)
    print("Type:", post_type, "| roots requested:", args.count)
    print("=" * 76)

    try:
        post = api.video(id=post_id, username=username)

        if post_type == "photo":
            async def photo_view(self, **kwargs):
                page = self.parent._page
                if "/photo/" not in page.url or post_id not in page.url:
                    print("Opening photo URL:", canonical_url)
                    await self.parent.navigate(canonical_url, wait_until="domcontentloaded")
                    await asyncio.sleep(5)
                if "/photo/" not in page.url or post_id not in page.url:
                    raise RuntimeError("Browser did not remain on requested /photo/ post")
                print("Browser landed on:", page.url)

            post.view = MethodType(photo_view, post)

        # PyTok matches banked responses by substring, and 'api/comment/list'
        # also matches 'api/comment/list/reply'. Reply responses left over from
        # the previous post would otherwise be parsed as this post's top-level
        # comments, so drain the buffer before opening the page.
        stale = await api.process_pending_responses("api/comment/list")
        if stale:
            print(f"Discarded {len(stale)} stale comment/reply responses from previous posts")

        args.debug_dir = post_dir
        await ensure_comment_panel(post, args)

        roots, root_diag = await collect_roots_strict(
            post,
            count=args.count,
            batch_size=args.batch_size,
        )
        summary["root_collection_diagnostic"] = root_diag

        # Save roots BEFORE touching replies.
        with roots_path.open("x", encoding="utf-8") as sink:
            for root in roots:
                rec = {
                    "record_type": "top_level_comment",
                    "post_id": post_id,
                    "post_type": post_type,
                    "source_url": canonical_url,
                    "observed_at_utc": utc_now(),
                    "comment_id": comment_id(root) or None,
                    "comment": root,
                }
                sink.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

        summary["received_top_level_unique"] = len(roots)
        print("Saved top-level roots:", len(roots))

        replies, reply_diag = await fetch_replies_for_roots(
            post,
            roots,
            batch_size=args.batch_size,
            root_delay=args.reply_root_delay,
            debug_dir=post_dir,
        )

        with replies_path.open("x", encoding="utf-8") as sink:
            for reply in replies:
                reply.update({
                    "post_id": post_id,
                    "post_type": post_type,
                    "source_url": canonical_url,
                    "observed_at_utc": utc_now(),
                })
                sink.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")

        summary["replies_collected_unique"] = len(replies)
        summary["reply_diagnostics"] = reply_diag

        relevant = [
            d for d in reply_diag.values()
            if int(d.get("declared_total") or 0) > 0
        ]
        summary["roots_declaring_replies"] = len(relevant)
        summary["roots_replies_complete"] = sum(
            d.get("status") == "complete" for d in relevant
        )
        summary["roots_replies_partial"] = sum(
            d.get("status") == "partial" for d in relevant
        )
        summary["roots_replies_missing"] = sum(
            d.get("status") == "missing" for d in relevant
        )
        summary["roots_reply_errors"] = sum(
            d.get("status") == "error" for d in relevant
        )

        summary["page_diagnostic"] = await page_snapshot(api._page)
        summary["status"] = "success"

    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        try:
            summary["page_diagnostic"] = await page_snapshot(api._page)
        except Exception:
            pass
        print("STOP for this post; no automatic retry:", summary["error"])

    summary["finished_at_utc"] = utc_now()
    json_dump(summary_path, summary)

    print("\nStatus:", summary["status"])
    print("Top-level saved:", summary["received_top_level_unique"])
    print("Replies saved:", summary["replies_collected_unique"])
    print(
        "Reply roots complete/partial/missing/error:",
        summary["roots_replies_complete"],
        summary["roots_replies_partial"],
        summary["roots_replies_missing"],
        summary["roots_reply_errors"],
    )
    print("COMMENTS:", roots_path)
    print("REPLIES: ", replies_path)
    print("SUMMARY: ", summary_path)

    return summary


def gather_urls(args):
    urls = []

    for value in args.url or []:
        value = value.strip()
        if value:
            urls.append(value)

    if args.urls_file:
        with args.urls_file.open("r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)

    # Canonicalize and deduplicate without changing order.
    out = []
    seen = set()
    for url in urls:
        info = parse_tiktok_url(url)
        canonical = info["canonical_url"]
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)

    return out


async def run(args):
    from pytok.tiktok import PyTok
    from pytok.accounts import AccountsPool

    urls = gather_urls(args)
    if not urls:
        raise ValueError("Provide at least one --url or --urls-file")

    print("Posts:", len(urls))
    print("Request delay:", f"{args.request_delay:g}s")
    print(
        "Comment panel wait:",
        f"minimum {args.min_wait:g}s, maximum {args.max_wait:g}s"
    )
    print("Reply-thread delay:", f"{args.reply_root_delay:g}s")
    print("Between-post delay:", f"{args.between_posts_delay:g}s")
    print("Automatic retries: OFF")
    print("Replies: ON for every saved root that declares replies")

    summaries = []
    pool = AccountsPool()

    async with await PyTok.from_pool(
        pool,
        request_delay=args.request_delay,
    ) as api:
        for idx, url in enumerate(urls):
            summaries.append(await collect_one(api, url, args))

            if idx < len(urls) - 1 and args.between_posts_delay > 0:
                print(
                    f"\nWaiting {args.between_posts_delay:g}s before next post..."
                )
                await asyncio.sleep(args.between_posts_delay)

    batch_path = args.case_dir / f"batch_{stamp()}_summary.json"
    json_dump(batch_path, {
        "created_at_utc": utc_now(),
        "post_count": len(urls),
        "success_count": sum(s.get("status") == "success" for s in summaries),
        "error_count": sum(s.get("status") != "success" for s in summaries),
        "summaries": summaries,
    })
    print("\nBATCH SUMMARY:", batch_path)

    return 0 if all(s.get("status") == "success" for s in summaries) else 1


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Collect TikTok top-level comments and all replies for those roots "
            "from /video/ and /photo/ posts."
        )
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--url",
        action="append",
        help="TikTok /video/ or /photo/ URL. Repeat for multiple posts.",
    )
    parser.add_argument(
        "--urls-file",
        type=Path,
        help="UTF-8 text file with one TikTok URL per line.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"Maximum top-level comments per post (default {DEFAULT_COUNT}).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"PyTok page size for comments/replies (default {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY,
        help=f"Base PyTok request delay in seconds (default {DEFAULT_REQUEST_DELAY:g}).",
    )
    parser.add_argument(
        "--min-wait",
        type=float,
        default=DEFAULT_MIN_COMMENT_WAIT,
        help=f"Minimum wait after opening comment panel (default {DEFAULT_MIN_COMMENT_WAIT:g}s).",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=DEFAULT_MAX_COMMENT_WAIT,
        help=f"Maximum total wait for comments (default {DEFAULT_MAX_COMMENT_WAIT:g}s).",
    )
    parser.add_argument(
        "--reply-root-delay",
        type=float,
        default=DEFAULT_REPLY_ROOT_DELAY,
        help=f"Pause between separate reply threads (default {DEFAULT_REPLY_ROOT_DELAY:g}s).",
    )
    parser.add_argument(
        "--between-posts-delay",
        type=float,
        default=DEFAULT_BETWEEN_POSTS_DELAY,
        help=f"Pause between different posts (default {DEFAULT_BETWEEN_POSTS_DELAY:g}s).",
    )
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=DEFAULT_CASE_DIR,
        help=f"Output directory (default {DEFAULT_CASE_DIR}).",
    )

    args = parser.parse_args()

    if args.check:
        return check_local_api()

    if not 1 <= args.count <= 1000:
        parser.error("--count must be between 1 and 1000")
    if not 1 <= args.batch_size <= 100:
        parser.error("--batch-size must be between 1 and 100")
    if args.request_delay < 1:
        parser.error("--request-delay must be at least 1 second")
    if args.min_wait < 0:
        parser.error("--min-wait must be >= 0")
    if args.max_wait < 5:
        parser.error("--max-wait must be at least 5 seconds")
    if args.min_wait > args.max_wait:
        parser.error("--min-wait cannot exceed --max-wait")
    if args.reply_root_delay < 0:
        parser.error("--reply-root-delay must be >= 0")
    if args.between_posts_delay < 0:
        parser.error("--between-posts-delay must be >= 0")

    # Validate before making any network request.
    try:
        gather_urls(args)
    except Exception as exc:
        parser.error(str(exc))

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
