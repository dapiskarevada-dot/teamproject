#!/usr/bin/env python
# -*- coding: utf-8 -*-

r"""
Search TikTok with PyTok, save the search corpus, deduplicate posts, then
collect top-level comments + replies using collect_tiktok_threads.py.

IMPORTANT
---------
- Put this file next to collect_tiktok_threads.py.
- Search retrieval is NOT a random or exhaustive sample of TikTok.
- No automatic retries.
- Conservative delays by default.
- Search results are saved BEFORE comment collection.
- Posts duplicated across queries are collected only once, while matched
  queries/ranks are preserved in the search manifest.
- --hashtag TAG adds posts from a hashtag feed to the same pipeline (that is
  where photo carousels live; search(...).videos() returns videos only).
- Photo posts: carousel slides are downloaded (collect_tiktok_images) and
  transcribed by a vision model through the API (ocr_vlm); the text and the
  schools mentioned on the slides go into the posts table.
- Besides the JSON layer, a flat table of unique posts is written
  (search_posts_<run>.xlsx / .csv) with author account creation date,
  author stats, post stats, hashtags and (optionally) subtitles.
  Fields come from tiktok_fields.py (merged from scrapping2.py).

Examples
--------
Single query:
    python .\collect_tiktok_search_threads.py --query "умскул" --search-count 10 --comments 20

Several queries:
    python .\collect_tiktok_search_threads.py --query "умскул" --query "егэленд" --search-count 10 --comments 20

Queries file, one query per line:
    python .\collect_tiktok_search_threads.py --queries-file .\queries.txt --search-count 20 --comments 50

Only search, no post/comment collection:
    python .\collect_tiktok_search_threads.py --query "умскул" --search-count 20 --search-only

Search + table with subtitles and author profiles (extra requests):
    python .\collect_tiktok_search_threads.py --query "умскул" --search-count 50 --search-only --subtitles --fetch-author

Interface check only, no TikTok request:
    python .\collect_tiktok_search_threads.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any

from collect_tiktok_threads import (
    collect_one,
    json_dump,
    stamp,
    utc_now,
)
from tiktok_fields import (
    author_fields_missing,
    download_subtitles,
    fill_author_from_profile,
    flatten_post,
    rows_to_table,
)

DEFAULT_CASE_DIR = Path("cases") / "tiktok_search_threads"
DEFAULT_SEARCH_COUNT = 10
DEFAULT_COMMENTS = 20
DEFAULT_REQUEST_DELAY = 4.0
DEFAULT_MIN_WAIT = 5.0
DEFAULT_MAX_WAIT = 15.0
DEFAULT_REPLY_ROOT_DELAY = 4.0
DEFAULT_BETWEEN_POSTS_DELAY = 8.0
DEFAULT_BETWEEN_QUERIES_DELAY = 10.0
DEFAULT_BATCH_SIZE = 50


def safe_json(value: Any):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(v) for v in value]
    if hasattr(value, "tolist"):
        try:
            return safe_json(value.tolist())
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return safe_json(value.item())
        except Exception:
            pass
    return str(value)


def extract_data(video):
    """Read data already attached to a search result without a new request."""
    candidates = []

    for attr in ("as_dict", "_data", "data"):
        try:
            value = getattr(video, attr, None)
        except Exception:
            value = None

        if callable(value):
            try:
                value = value()
            except Exception:
                value = None

        if isinstance(value, dict) and value:
            candidates.append(value)

    return candidates[0] if candidates else {}


def nested_get(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def infer_username(video, data):
    for value in (
        getattr(video, "username", None),
        nested_get(data, "author", "uniqueId"),
        nested_get(data, "author", "unique_id"),
        nested_get(data, "author", "username"),
        data.get("authorName") if isinstance(data, dict) else None,
    ):
        if value:
            return str(value).lstrip("@")
    return None


def infer_post_id(video, data):
    for value in (
        getattr(video, "id", None),
        data.get("id") if isinstance(data, dict) else None,
        data.get("aweme_id") if isinstance(data, dict) else None,
        data.get("item_id") if isinstance(data, dict) else None,
    ):
        if value:
            return str(value)
    return None


def has_photo_payload(data):
    if not isinstance(data, dict):
        return False

    direct = (
        data.get("imagePost"),
        data.get("image_post"),
        data.get("image_post_info"),
    )
    if any(isinstance(x, dict) and x for x in direct):
        return True

    # Common TikTok representations for image posts.
    for key in ("awemeType", "aweme_type", "type"):
        value = data.get(key)
        if str(value).lower() in {"150", "image", "photo", "photo_mode"}:
            return True

    return False


def infer_post_type(data):
    return "photo" if has_photo_payload(data) else "video"


def build_search_record(video, query, rank):
    data = extract_data(video)
    post_id = infer_post_id(video, data)
    username = infer_username(video, data)
    post_type = infer_post_type(data)

    if not post_id:
        raise RuntimeError("Search result has no stable post/video id")
    if not username:
        raise RuntimeError(f"Search result {post_id} has no username")

    canonical_url = (
        f"https://www.tiktok.com/@{username}/{post_type}/{post_id}"
    )

    fields = flatten_post(data, post_type=post_type)
    fields["post_id"] = post_id
    fields["video_url"] = canonical_url
    if not fields.get("author_username"):
        fields["author_username"] = username

    return {
        "query": query,
        "query_rank": rank,
        "post_id": post_id,
        "username": username,
        "post_type_inferred": post_type,
        "canonical_url": canonical_url,
        "search_observed_at_utc": utc_now(),
        "fields": fields,
        "raw_search_item": safe_json(data),
    }


def gather_hashtags(args):
    tags = [t.strip().lstrip("#") for t in (args.hashtag or []) if t.strip()]
    if getattr(args, "hashtags_file", None):
        with args.hashtags_file.open("r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip().lstrip("#")
                if line and not line.startswith("#"):
                    tags.append(line)
    out, seen = [], set()
    for t in tags:
        if t.casefold() not in seen:
            seen.add(t.casefold()); out.append(t)
    return out


async def run_hashtag_feed(api, tag, count):
    """One hashtag feed, same record shape as search (query = '#tag')."""
    records, rank = [], 0
    print("\n" + "=" * 76)
    print("HASHTAG:", "#" + tag, "| requested:", count)
    print("=" * 76)
    async for video in api.hashtag(name=tag).videos(count=count):
        rank += 1
        try:
            record = build_search_record(video, "#" + tag, rank)
        except Exception as exc:
            print(f"hashtag item {rank}: skipped — {type(exc).__name__}: {exc}")
            continue
        records.append(record)
        print(f"{rank:>3}. {record['post_type_inferred']:<5} {record['post_id']} @{record['username']}")
        if len(records) >= count:
            break
    print("Received usable hashtag items:", len(records))
    return records


async def collect_slides_and_ocr(api, unique_posts, args):
    """
    For every photo post: download carousel slides with the open PyTok session,
    then transcribe them through the vision API and fill the slides_* fields.
    """
    photo = [p for p in unique_posts if p.get("post_type_inferred") == "photo"]
    if not photo:
        print("Photo posts: none — slides/OCR step skipped")
        return
    media_root = args.case_dir.parent / "tiktok_media"
    if not args.no_images:
        import collect_tiktok_images as images
        img_args = argparse.Namespace(out_dir=media_root, page_wait=6.0, min_width=300, min_height=300,
                                      max_images=40, request_delay=args.request_delay, between_posts=args.between_posts_delay)
        print(f"\nPhoto posts: {len(photo)} — downloading carousel slides")
        for i, p in enumerate(photo):
            pid = str(p["post_id"])
            if any((media_root / "raw" / "posts" / pid / "images").glob("*")) and not getattr(args, "force_images", False):
                print(f"slides for {pid} already on disk — skipped")
                continue
            try:
                await images.collect_one(api, p["canonical_url"], img_args)
            except Exception as exc:
                print(f"slides {pid}: {type(exc).__name__}: {exc}")
            await drain_responses(api)
            if i < len(photo) - 1 and args.between_posts_delay > 0:
                await asyncio.sleep(args.between_posts_delay)
    if args.no_ocr:
        return
    try:
        import ocr_vlm
        if not ocr_vlm.load_key():
            print("OCR skipped: no API key (openrouter_key.txt / OPENROUTER_API_KEY)")
            return
        print(f"\nTranscribing slides with {args.ocr_model}")
        rows, per_post = ocr_vlm.transcribe(media_root, model=args.ocr_model, post_ids={str(p["post_id"]) for p in photo})
    except Exception as exc:
        print("OCR failed:", f"{type(exc).__name__}: {exc}")
        return
    filled = 0
    for p in photo:
        r = per_post.get(str(p["post_id"]))
        if not r:
            continue
        f = p["fields"]
        f["slides_count"] = r["slides"]
        f["slides_text"] = r["text"]
        f["slides_schools"] = "; ".join(r["schools"])
        f["slides_context"] = "; ".join(r["contexts"])
        f["slides_comparison"] = bool(r["comparison"])
        f["slides_summary"] = "; ".join(x.get("summary", "") for x in rows if x["post_id"] == str(p["post_id"]) and x.get("summary"))
        filled += 1
    print(f"Slides text filled for {filled}/{len(photo)} photo posts")


async def collect_videos_and_transcribe(api, unique_posts, args):
    """
    For every video post: video.info() (TikTok auto-subtitles when present),
    save the video with the open PyTok session, then Whisper (faster-whisper)
    according to --whisper: missing (only posts without TikTok subtitles),
    all, off. Fills subtitle_*, transcript_* and video_file fields.
    """
    videos = [p for p in unique_posts if p.get("post_type_inferred") != "photo"]
    if not videos:
        return
    media_root = args.case_dir.parent / "tiktok_media"
    from tiktok_fields import download_subtitles, subtitle_langs, unwrap_item
    print(f"\nVideo posts: {len(videos)} — subtitles + video download (--whisper {args.whisper})")
    for i, p in enumerate(videos):
        pid, user = str(p["post_id"]), p["username"]
        f = p["fields"]
        post_dir = media_root / "raw" / "posts" / pid
        have_video = any(post_dir.glob("video.*"))
        need_video = (not args.no_videos) and (not have_video)      # видео нужно и для Whisper, и для текста с кадров
        need_subs = not f.get("subtitle_text")
        if not need_video and not need_subs:
            continue
        try:
            video = api.video(id=pid, username=user)
            info = await video.info()
            item = unwrap_item(info)
            if need_subs:
                langs = subtitle_langs(item)
                if langs:
                    f["subtitle_langs"] = langs
                    f["subtitle_text"] = await asyncio.to_thread(download_subtitles, item)
                    if f["subtitle_text"]:
                        f["transcript_source"] = f.get("transcript_source") or "tiktok"
            if need_video:
                data = None
                try:
                    data = await asyncio.wait_for(video.bytes(), timeout=120)
                except Exception as exc:
                    print(f"  video {pid}: pytok bytes failed ({str(exc)[:80]}) -> browser download")
                if data and len(data) > 20_000:
                    post_dir.mkdir(parents=True, exist_ok=True)
                    (post_dir / "video.mp4").write_bytes(data)
                    f["video_file"] = str(post_dir / "video.mp4")
                    print(f"  video {pid}: saved {len(data) // 1024} KB" + (" | subs: tiktok" if f.get("subtitle_text") else ""))
                else:
                    from video_download import download_video
                    path = await download_video(api, p["canonical_url"], item, post_dir)
                    if path:
                        f["video_file"] = str(path)
                    else:
                        print(f"  video {pid}: NOT downloaded (all methods failed)")
        except Exception as exc:
            print(f"  video {pid}: {type(exc).__name__}: {str(exc)[:160]}")
        await drain_responses(api)
        if i < len(videos) - 1 and args.request_delay > 0:
            await asyncio.sleep(args.request_delay)
        if args.whisper == "off":
            continue

    if args.whisper == "off":
        return
    try:
        import transcribe_whisper as tw
    except Exception as exc:
        print("Whisper skipped: faster-whisper not installed:", exc)
        return
    targets = {str(p["post_id"]) for p in videos if args.whisper == "all" or not p["fields"].get("subtitle_text")}
    if not targets:
        return
    print(f"\nWhisper ({args.whisper_model}) for {len(targets)} videos")
    try:
        res = tw.transcribe_dir(media_root, post_ids=targets, model_name=args.whisper_model)
    except Exception as exc:
        print("Whisper failed:", f"{type(exc).__name__}: {exc}")
        return
    filled = 0
    for p in videos:
        r = res.get(str(p["post_id"]))
        if not r:
            continue
        f = p["fields"]
        if r.get("text"):
            f["transcript_whisper"] = r["text"]; f["transcript_source"] = "whisper"; filled += 1
        elif r.get("source"):
            f["transcript_source"] = f.get("transcript_source") or r["source"]
    try:
        tw.write_table(res, media_root)
    except Exception:
        pass
    print(f"Whisper transcripts filled for {filled}/{len(targets)} videos")


def gather_queries(args):
    queries = []

    for q in args.query or []:
        q = q.strip()
        if q:
            queries.append(q)

    if args.queries_file:
        with args.queries_file.open("r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    queries.append(line)

    out = []
    seen = set()
    for q in queries:
        key = q.casefold()
        if key not in seen:
            seen.add(key)
            out.append(q)

    return out


def check_local_api():
    from pytok.tiktok import PyTok
    from pytok.api.search import Search

    print("PyTok:", inspect.getfile(PyTok))
    print("Search:", inspect.getfile(Search))
    print("Search.videos:", inspect.signature(Search.videos))

    try:
        import collect_tiktok_threads as threads
        print("Thread collector:", inspect.getfile(threads))
    except Exception as exc:
        print("STOP: cannot import collect_tiktok_threads.py:", exc)
        return 2

    try:
        import tiktok_fields as fields
        print("Fields module:", inspect.getfile(fields))
        print("Account date from id 7000000000000000000:",
              fields.account_created_from_id(7000000000000000000) or "(old id)")
    except Exception as exc:
        print("STOP: cannot import tiktok_fields.py:", exc)
        return 2

    print("Compatible local modules found. No TikTok request was made.")
    return 0


async def run_search_query(api, query, count):
    """
    Run one PyTok search. We consume at most `count` yielded items and do not
    call video.info(), so this stage uses the data already returned by search.
    """
    records = []
    rank = 0

    print("\n" + "=" * 76)
    print("SEARCH:", query)
    print("Requested results:", count)
    print("=" * 76)

    search = api.search(query)

    async for video in search.videos(count=count):
        rank += 1
        if rank > count:
            break

        try:
            record = build_search_record(video, query, rank)
        except Exception as exc:
            print(
                f"search result {rank}: skipped — "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        records.append(record)
        print(
            f"{rank:>3}. {record['post_type_inferred']:<5} "
            f"{record['post_id']} @{record['username']}"
        )

        if len(records) >= count:
            break

    print("Received usable search results:", len(records))
    return records


def merge_search_records(all_records):
    """
    Deduplicate by stable post_id while preserving every query and rank that
    retrieved the post.
    """
    merged = {}
    order = []

    for rec in all_records:
        post_id = rec["post_id"]

        if post_id not in merged:
            merged[post_id] = {
                "post_id": post_id,
                "username": rec["username"],
                "post_type_inferred": rec["post_type_inferred"],
                "canonical_url": rec["canonical_url"],
                "matched_queries": [],
                "fields": dict(rec.get("fields") or {}),
                "raw_search_item": rec["raw_search_item"],
            }
            order.append(post_id)

        item = merged[post_id]
        item["matched_queries"].append({
            "query": rec["query"],
            "query_rank": rec["query_rank"],
            "observed_at_utc": rec["search_observed_at_utc"],
        })

        # If any occurrence clearly carries imagePost, prefer photo.
        if rec["post_type_inferred"] == "photo":
            item["post_type_inferred"] = "photo"
            item["canonical_url"] = (
                f"https://www.tiktok.com/@{item['username']}/photo/{post_id}"
            )
            item["fields"]["post_type"] = "photo"
            item["fields"]["video_url"] = item["canonical_url"]

    for item in merged.values():
        item["fields"]["matched_queries"] = [
            f"{m['query']} (#{m['query_rank']})" for m in item["matched_queries"]
        ]

    return [merged[pid] for pid in order]


async def enrich_posts_table(api, unique_posts, args):
    """
    Fill the flat table: subtitles (CDN download) and, if requested,
    missing author stats via api.user(username).info() with a cache.
    Both are optional and off by default to keep the search stage request-free.
    """
    author_cache = {}

    for i, item in enumerate(unique_posts, start=1):
        row = item["fields"]

        if args.subtitles:
            raw = item.get("raw_search_item") or {}
            row["subtitle_text"] = await asyncio.to_thread(download_subtitles, raw)

        if args.fetch_author and author_fields_missing(row) and row.get("author_username"):
            username = row["author_username"]
            if username not in author_cache:
                try:
                    author_cache[username] = await api.user(username=username).info()
                except Exception as exc:
                    print(f"  author @{username}: profile request failed: "
                          f"{type(exc).__name__}: {exc}")
                    author_cache[username] = {}
                if args.request_delay > 0:
                    await asyncio.sleep(args.request_delay)
            fill_author_from_profile(row, author_cache[username])

        if args.subtitles or args.fetch_author:
            print(f"table {i}/{len(unique_posts)}: {row.get('post_id')} "
                  f"@{row.get('author_username')} "
                  f"created={row.get('author_created') or '?'} "
                  f"subs={'yes' if row.get('subtitle_text') else 'no'}")


async def drain_responses(api):
    """pytok копит тела всех перехваченных ответов (в т.ч. видео) до конца сессии — чистим, иначе память растёт на гигабайты."""
    try:
        await api.process_pending_responses()
    except Exception:
        pass


def mentions_aliases(fields: dict, aliases) -> bool:
    if not aliases:
        return True
    blob = " ".join(str(fields.get(k) or "") for k in ("description", "hashtags", "author_username", "author_nickname", "author_bio")).lower()
    return any(a in blob for a in aliases)


def parse_aliases(s: str):
    return [a.strip().lower() for a in (s or "").split(",") if a.strip()]


def apply_window(unique_posts, args):
    """--since / --until (YYYY-MM-DD): keep posts whose create_time is inside the window."""
    if not (args.since or args.until):
        return unique_posts
    lo = (args.since or "0000-01-01") + " 00:00:00"
    hi = (args.until or "9999-12-31") + " 23:59:59"
    def ok(p):
        t = str((p.get("fields") or {}).get("create_time") or "")
        return (not t) or (lo <= t <= hi)      # без даты — оставляем (дата неизвестна, не доказано, что вне окна)
    kept = [p for p in unique_posts if ok(p)]
    print(f"Date window {args.since or '...'} .. {args.until or '...'}: keeping {len(kept)} of {len(unique_posts)} posts")
    return kept


async def run_author_feed(api, username, count, aliases):
    """All posts of one author (channel '@username'); only those mentioning the school aliases are kept."""
    records, rank, kept = [], 0, 0
    print("\n" + "=" * 76)
    print("AUTHOR FEED:", "@" + username, "| requested:", count)
    print("=" * 76)
    try:
        async for video in api.user(username=username).videos(count=count):
            rank += 1
            try:
                record = build_search_record(video, "@" + username, rank)
            except Exception as exc:
                print(f"author item {rank}: skipped — {type(exc).__name__}: {exc}")
                continue
            if mentions_aliases(record.get("fields") or {}, aliases):
                records.append(record); kept += 1
            if rank >= count:
                break
    except Exception as exc:
        print(f"AUTHOR STOP @{username}; no automatic retry: {type(exc).__name__}: {exc}")
    print(f"Author @{username}: {rank} posts seen, {kept} mention the school")
    return records


def pick_authors(unique_posts, args):
    """Authors with >= --authors-min-posts posts mentioning the school, plus --author list."""
    aliases = parse_aliases(args.prefer)
    counts = {}
    for p in unique_posts:
        f = p.get("fields") or {}
        u = (f.get("author_username") or "").strip().lstrip("@")
        if u and mentions_aliases(f, aliases):
            counts[u] = counts.get(u, 0) + 1
    chosen = [u for u, n in sorted(counts.items(), key=lambda x: -x[1]) if n >= args.authors_min_posts] if args.authors_min_posts else []
    for u in args.author or []:
        u = u.strip().lstrip("@")
        if u and u not in chosen:
            chosen.insert(0, u)
    if args.max_authors:
        chosen = chosen[: args.max_authors]
    return chosen, counts


def coverage_report(all_records, kept_ids):
    """Per-channel catch / new-in-order, and a Lincoln-Petersen estimate of the total from
    the overlap of independent channel groups (search queries vs hashtag feeds vs author feeds)."""
    order, by_channel = [], {}
    for r in all_records:
        if str(r["post_id"]) not in kept_ids:
            continue
        q = r["query"]
        if q not in by_channel:
            by_channel[q] = []; order.append(q)
        by_channel[q].append(str(r["post_id"]))
    seen, rows = set(), []
    for q in order:
        ids = set(by_channel[q]); new = ids - seen; seen |= ids
        rows.append({"channel": q, "type": "хэштег" if q.startswith("#") else ("автор" if q.startswith("@") else "поиск"),
                     "caught": len(ids), "new": len(new), "new_pct": round(100 * len(new) / max(1, len(seen)), 1)})
    groups = {"поиск": set(), "хэштег": set(), "автор": set()}
    for q, ids in by_channel.items():
        groups["хэштег" if q.startswith("#") else ("автор" if q.startswith("@") else "поиск")] |= set(ids)
    total_found = len(seen)
    est = None
    n1, n2, m = len(groups["поиск"]), len(groups["хэштег"]), len(groups["поиск"] & groups["хэштег"])
    if m:
        est = round(n1 * n2 / m)
    last_new_pct = rows[-1]["new_pct"] if rows else None
    # покрытие считаем по «видимому через поиск/хэштеги»; ленты авторов — отдельная прибавка сверх этого
    visible = len(groups["поиск"] | groups["хэштег"])
    coverage_pct = round(100 * min(visible, est) / est, 1) if est else None
    verdict = "ок" if (coverage_pct is not None and coverage_pct >= 90 and (last_new_pct is None or last_new_pct < 5)) else "добавить запросы"
    rep = {"channels": rows, "found": total_found, "search": n1, "hashtags": n2, "both": m, "authors": len(groups["автор"]),
           "visible_search_hashtags": visible, "authors_extra": total_found - visible,
           "estimate": est, "coverage_pct": coverage_pct, "last_channel_new_pct": last_new_pct, "verdict": verdict}
    print("\nCOVERAGE: found", total_found, "| search", n1, "| hashtags", n2, "| both", m, "| authors", len(groups["автор"]),
          "| estimate", est, "| coverage", coverage_pct, "% | last channel new", last_new_pct, "% ->", verdict)
    return rep


def already_collected(case_dir: Path, post_id: str) -> bool:
    """True if a successful comments run for this post already exists in case_dir."""
    d = case_dir / "raw" / "posts" / str(post_id)
    for f in d.glob("thread_*_summary.json"):
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("status") == "success":
                return True
        except Exception:
            pass
    return False


def apply_post_limit(unique_posts, args):
    """
    --prefer "Умскул,Umschool": posts whose description/hashtags/author mention an alias go first
    (stable order otherwise). --max-posts N: keep at most N posts for the heavy steps
    (slides, videos, Whisper, comments). 0 = no limit.
    """
    aliases = [a.strip().lower() for a in (args.prefer or "").split(",") if a.strip()]
    if aliases:
        def rel(p):
            f = p.get("fields") or {}
            blob = " ".join(str(f.get(k) or "") for k in ("description", "hashtags", "author_username", "author_nickname")).lower()
            return 0 if any(a in blob for a in aliases) else 1
        unique_posts = sorted(unique_posts, key=rel)
        print("Posts mentioning", aliases, ":", sum(rel(p) == 0 for p in unique_posts), "of", len(unique_posts))
    if args.max_posts and len(unique_posts) > args.max_posts:
        print(f"Post limit: keeping {args.max_posts} of {len(unique_posts)} unique posts")
        unique_posts = unique_posts[:args.max_posts]
    return unique_posts


async def main_async(args):
    from pytok.tiktok import PyTok
    from pytok.accounts import AccountsPool

    queries = gather_queries(args)
    hashtags = gather_hashtags(args)
    reuse = None
    if args.posts_json:
        reuse = json.loads(Path(args.posts_json).read_text(encoding="utf-8"))
        print("REUSE posts from", args.posts_json, "| posts:", len(reuse.get("posts", [])))
    elif not queries and not hashtags:
        raise ValueError("Provide --query / --queries-file and/or --hashtag / --hashtags-file (or --posts-json)")

    run_id = stamp()
    search_dir = args.case_dir / "search"
    search_dir.mkdir(parents=True, exist_ok=True)

    raw_path = search_dir / f"search_results_{run_id}.jsonl"
    dedup_path = search_dir / f"search_posts_{run_id}.json"
    batch_path = search_dir / f"search_threads_{run_id}_summary.json"

    print("Queries:", len(queries))
    print("Search results per query:", args.search_count)
    print("Top-level comments per unique post:", args.comments)
    print("Request delay:", f"{args.request_delay:g}s")
    print("Between-query delay:", f"{args.between_queries_delay:g}s")
    print("Between-post delay:", f"{args.between_posts_delay:g}s")
    print("Reply-thread delay:", f"{args.reply_root_delay:g}s")
    print("Automatic retries: OFF")

    all_records = []
    post_summaries = []

    pool = AccountsPool()

    async with await PyTok.from_pool(
        pool,
        request_delay=args.request_delay,
    ) as api:

        for qi, query in enumerate([] if reuse else queries):
            try:
                records = await run_search_query(
                    api,
                    query=query,
                    count=args.search_count,
                )
                all_records.extend(records)
                await drain_responses(api)
            except Exception as exc:
                print(
                    "SEARCH STOP for this query; no automatic retry:",
                    f"{type(exc).__name__}: {exc}",
                )

            if qi < len(queries) - 1 and args.between_queries_delay > 0:
                print(
                    f"Waiting {args.between_queries_delay:g}s "
                    "before next search query..."
                )
                await asyncio.sleep(args.between_queries_delay)

        for hi, tag in enumerate([] if reuse else hashtags):
            try:
                all_records.extend(await run_hashtag_feed(api, tag, args.search_count))
                await drain_responses(api)
            except Exception as exc:
                print("HASHTAG STOP for this tag; no automatic retry:", f"{type(exc).__name__}: {exc}")
            if hi < len(hashtags) - 1 and args.between_queries_delay > 0:
                await asyncio.sleep(args.between_queries_delay)

        # Save the raw retrieval layer BEFORE opening result posts.
        with raw_path.open("x", encoding="utf-8") as f:
            for rec in all_records:
                f.write(
                    json.dumps(rec, ensure_ascii=False, default=str) + "\n"
                )

        if reuse:
            unique_posts = list(reuse.get("posts", []))
            authors = reuse.get("authors") or []
            coverage = reuse.get("coverage") or {}
            if args.only_ids:
                keep = {l.strip() for l in Path(args.only_ids).read_text(encoding="utf-8").splitlines() if l.strip()}
                unique_posts = [p for p in unique_posts if str(p["post_id"]) in keep]
                print(f"Only-ids: {len(unique_posts)} posts selected")
        else:
            unique_posts = apply_window(merge_search_records(all_records), args)

        # Author feeds: official accounts + authors with several posts about the school.
        authors, author_counts = ([], {}) if reuse else pick_authors(unique_posts, args)
        if authors:
            print(f"\nAuthor feeds: {len(authors)} accounts ({', '.join('@' + a for a in authors[:12])}{'...' if len(authors) > 12 else ''})")
            aliases = parse_aliases(args.prefer)
            for ai, u in enumerate(authors):
                all_records.extend(await run_author_feed(api, u, args.author_count, aliases))
                await drain_responses(api)
                if ai < len(authors) - 1 and args.between_queries_delay > 0:
                    await asyncio.sleep(args.between_queries_delay)
            unique_posts = apply_window(merge_search_records(all_records), args)

        if not reuse:
            coverage = coverage_report(all_records, {str(p["post_id"]) for p in unique_posts})
        unique_posts = apply_post_limit(unique_posts, args)
        json_dump(dedup_path, {
            "coverage": coverage,
            "window": {"since": args.since, "until": args.until},
            "authors": authors,
            "created_at_utc": utc_now(),
            "queries": queries + ["#" + t for t in hashtags],
            "requested_per_query": args.search_count,
            "raw_result_count": len(all_records),
            "unique_post_count": len(unique_posts),
            "posts": unique_posts,
            "note": (
                "TikTok/PyTok search retrieval is a retrieval mechanism, not "
                "an exhaustive or random sample. Duplicates across queries are "
                "deduplicated by stable post_id."
            ),
        })

        print("\nRaw search results:", len(all_records))
        print("Unique posts after dedup:", len(unique_posts))
        print("SEARCH RAW:", raw_path)
        print("SEARCH DEDUP:", dedup_path)

        # Flat table layer (account creation date, author/post stats, subtitles).
        await enrich_posts_table(api, unique_posts, args)
        # Carousels: download slides + transcribe them, fill slides_* columns.
        await collect_slides_and_ocr(api, unique_posts, args)
        # Videos: TikTok subtitles + video download + Whisper, fill transcript_* columns.
        await collect_videos_and_transcribe(api, unique_posts, args)
        table_paths = rows_to_table(
            [p["fields"] for p in unique_posts],
            search_dir / f"search_posts_{run_id}",
        )
        # Re-save dedup JSON so it carries the enriched fields too.
        json_dump(dedup_path, {
            "coverage": coverage,
            "window": {"since": args.since, "until": args.until},
            "authors": authors,
            "created_at_utc": utc_now(),
            "queries": queries + ["#" + t for t in hashtags],
            "requested_per_query": args.search_count,
            "raw_result_count": len(all_records),
            "unique_post_count": len(unique_posts),
            "posts": unique_posts,
            "note": (
                "TikTok/PyTok search retrieval is a retrieval mechanism, not "
                "an exhaustive or random sample. Duplicates across queries are "
                "deduplicated by stable post_id."
            ),
        })
        for p in table_paths:
            print("SEARCH TABLE:", p)

        if args.search_only:
            json_dump(batch_path, {
                "created_at_utc": utc_now(),
                "queries": queries,
                "search_only": True,
                "raw_result_count": len(all_records),
                "unique_post_count": len(unique_posts),
                "post_summaries": [],
            })
            print("Search-only mode: no post/comment pages opened.")
            print("SUMMARY:", batch_path)
            return 0

        # Reuse the tested strict root -> reply collector.
        thread_args = argparse.Namespace(
            case_dir=args.case_dir,
            count=args.comments,
            batch_size=args.batch_size,
            request_delay=args.request_delay,
            min_wait=args.min_wait,
            max_wait=args.max_wait,
            reply_root_delay=args.reply_root_delay,
            between_posts_delay=args.between_posts_delay,
        )

        if args.skip_collected:
            before = len(unique_posts)
            unique_posts = [p for p in unique_posts if not already_collected(args.case_dir, p["post_id"])]
            print(f"Comments: skipping {before - len(unique_posts)} posts already collected in {args.case_dir}")

        for pi, item in enumerate(unique_posts):
            summary = await collect_one(
                api,
                item["canonical_url"],
                thread_args,
            )
            # Preserve search provenance in the post summary layer.
            summary["search_matches"] = item["matched_queries"]
            post_summaries.append(summary)
            await drain_responses(api)

            if (
                pi < len(unique_posts) - 1
                and args.between_posts_delay > 0
            ):
                print(
                    f"\nWaiting {args.between_posts_delay:g}s "
                    "before next search-result post..."
                )
                await asyncio.sleep(args.between_posts_delay)

    json_dump(batch_path, {
        "created_at_utc": utc_now(),
        "queries": queries,
        "search_only": False,
        "requested_per_query": args.search_count,
        "comments_per_post": args.comments,
        "raw_result_count": len(all_records),
        "unique_post_count": len(merge_search_records(all_records)),
        "post_success_count": sum(
            s.get("status") == "success" for s in post_summaries
        ),
        "post_error_count": sum(
            s.get("status") != "success" for s in post_summaries
        ),
        "post_summaries": post_summaries,
    })

    print("\nFINAL SUMMARY:", batch_path)

    # Separate comments+replies table for this run (one row per comment).
    try:
        from export_comments_table import export_from_summary
        rows, tables = export_from_summary(batch_path)
        n1 = sum(r["level"] == 1 for r in rows)
        print(f"COMMENTS TABLE: {n1} comments, {len(rows) - n1} replies")
        for t in tables:
            print("COMMENTS TABLE:", t)
    except Exception as exc:
        print("Comments table export failed:", f"{type(exc).__name__}: {exc}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=(
            "TikTok search -> deduplicated posts -> top-level comments -> replies."
        )
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--query",
        action="append",
        help="TikTok search query. Repeat for several queries.",
    )
    parser.add_argument(
        "--queries-file",
        type=Path,
        help="UTF-8 text file, one search query per line.",
    )
    parser.add_argument(
        "--search-count",
        type=int,
        default=DEFAULT_SEARCH_COUNT,
        help=f"Maximum search results per query (default {DEFAULT_SEARCH_COUNT}).",
    )
    parser.add_argument(
        "--comments",
        type=int,
        default=DEFAULT_COMMENTS,
        help=f"Top-level comments per unique result post (default {DEFAULT_COMMENTS}).",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=0,
        help="Keep at most N unique posts for slides/videos/comments (0 = all).",
    )
    parser.add_argument(
        "--prefer",
        default="",
        help="Comma-separated school aliases; posts mentioning them are processed first (used with --max-posts).",
    )
    parser.add_argument("--posts-json", default="", help="Reuse posts from an earlier search_posts_*.json instead of searching.")
    parser.add_argument("--only-ids", default="", help="Text file with post IDs (one per line): process only these.")
    parser.add_argument("--since", default="", help="Keep only posts created on/after YYYY-MM-DD (UTC).")
    parser.add_argument("--until", default="", help="Keep only posts created on/before YYYY-MM-DD (UTC).")
    parser.add_argument("--author", action="append", help="Author feed to add (official account). Repeat for several.")
    parser.add_argument("--authors-min-posts", type=int, default=0,
                        help="Also pull feeds of authors with at least N posts mentioning --prefer aliases (0 = off).")
    parser.add_argument("--author-count", type=int, default=200, help="Posts to read per author feed.")
    parser.add_argument("--max-authors", type=int, default=40, help="Cap on author feeds per run.")
    parser.add_argument(
        "--skip-collected",
        action="store_true",
        help="Do not re-collect comments for posts that already have a successful run in --case-dir.",
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="Save search results only; do not open posts or collect comments.",
    )
    parser.add_argument(
        "--hashtag",
        action="append",
        help="Hashtag feed to add (without #). Repeat for several.",
    )
    parser.add_argument(
        "--hashtags-file",
        type=Path,
        help="UTF-8 text file, one hashtag per line.",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Do not download carousel slides of photo posts.",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Do not transcribe slides through the vision API.",
    )
    parser.add_argument(
        "--ocr-model",
        default="google/gemini-2.5-flash",
        help="Vision model for slide transcription (OpenRouter id).",
    )
    parser.add_argument(
        "--no-videos",
        action="store_true",
        help="Do not download videos (then Whisper runs only on videos already on disk).",
    )
    parser.add_argument(
        "--whisper",
        choices=["missing", "all", "off"],
        default="missing",
        help="Speech transcription: missing = only posts without TikTok subtitles (default), all, off.",
    )
    parser.add_argument(
        "--whisper-model",
        default="large-v3-turbo",
        help="faster-whisper model (large-v3-turbo, large-v3, medium, small).",
    )
    parser.add_argument(
        "--subtitles",
        action="store_true",
        help="Download TikTok auto-subtitles text into the posts table (HTTP to CDN).",
    )
    parser.add_argument(
        "--fetch-author",
        action="store_true",
        help="If author stats are missing in search data, request the author profile.",
    )
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=DEFAULT_CASE_DIR,
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY,
    )
    parser.add_argument(
        "--min-wait",
        type=float,
        default=DEFAULT_MIN_WAIT,
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=DEFAULT_MAX_WAIT,
    )
    parser.add_argument(
        "--reply-root-delay",
        type=float,
        default=DEFAULT_REPLY_ROOT_DELAY,
    )
    parser.add_argument(
        "--between-posts-delay",
        type=float,
        default=DEFAULT_BETWEEN_POSTS_DELAY,
    )
    parser.add_argument(
        "--between-queries-delay",
        type=float,
        default=DEFAULT_BETWEEN_QUERIES_DELAY,
    )

    args = parser.parse_args()

    if args.check:
        return check_local_api()

    if not 1 <= args.search_count <= 200:
        parser.error("--search-count must be between 1 and 200")
    if not 1 <= args.comments <= 1000:
        parser.error("--comments must be between 1 and 1000")
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
    if args.between_queries_delay < 0:
        parser.error("--between-queries-delay must be >= 0")

    if not args.posts_json and not gather_queries(args) and not gather_hashtags(args):
        parser.error("Provide at least one --query/--queries-file or --hashtag/--hashtags-file (or --posts-json)")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
