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

    return {
        "query": query,
        "query_rank": rank,
        "post_id": post_id,
        "username": username,
        "post_type_inferred": post_type,
        "canonical_url": canonical_url,
        "search_observed_at_utc": utc_now(),
        "raw_search_item": safe_json(data),
    }


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

    return [merged[pid] for pid in order]


async def main_async(args):
    from pytok.tiktok import PyTok
    from pytok.accounts import AccountsPool

    queries = gather_queries(args)
    if not queries:
        raise ValueError("Provide --query or --queries-file")

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

        for qi, query in enumerate(queries):
            try:
                records = await run_search_query(
                    api,
                    query=query,
                    count=args.search_count,
                )
                all_records.extend(records)
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

        # Save the raw retrieval layer BEFORE opening result posts.
        with raw_path.open("x", encoding="utf-8") as f:
            for rec in all_records:
                f.write(
                    json.dumps(rec, ensure_ascii=False, default=str) + "\n"
                )

        unique_posts = merge_search_records(all_records)
        json_dump(dedup_path, {
            "created_at_utc": utc_now(),
            "queries": queries,
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

        for pi, item in enumerate(unique_posts):
            summary = await collect_one(
                api,
                item["canonical_url"],
                thread_args,
            )
            # Preserve search provenance in the post summary layer.
            summary["search_matches"] = item["matched_queries"]
            post_summaries.append(summary)

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
        "--search-only",
        action="store_true",
        help="Save search results only; do not open posts or collect comments.",
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

    queries = gather_queries(args)
    if not queries:
        parser.error("Provide at least one --query or --queries-file")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
