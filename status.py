#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Где сейчас сбор: по каждой школе из плана — этап, посты, слайды, видео, комментарии. Сбору не мешает.

    python status.py                      # schools_plan.txt
    python status.py --plan plan_general_ege.txt
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pipeline import load_plan, slug, CASES_ROOT, load_state

MEDIA = Path("cases") / "tiktok_media" / "raw" / "posts"


def stage_from_log(path: Path):
    """Последний этап по маркерам в логе + сколько строк назад он менялся."""
    if not path.exists():
        return "не запускалась", None
    txt = path.read_text(encoding="utf-8", errors="ignore")
    markers = [
        ("Search results per query", "1/6 поиск"), ("Received usable hashtag items", "1/6 ленты хэштегов"),
        ("Unique posts after dedup", "2/6 таблица постов"), ("Photo posts", "3/6 слайды + расшифровка"),
        ("Video posts", "4/6 видео + субтитры"), ("Whisper:", "4/6 Whisper"),
        ("Comments: skipping", "5/6 комментарии"), ("roots requested", "5/6 комментарии"),
        ("FINAL SUMMARY", "6/6 итог"), ("ИТОГ [", "готово"),
    ]
    last, pos = "запуск", -1
    for m, name in markers:
        i = txt.rfind(m)
        if i > pos:
            pos, last = i, name
    if "NoAccountError" in txt[-3000:]:
        last = "ждёт свободный аккаунт"
    age = time.time() - path.stat().st_mtime
    return last, age


def school_row(s):
    name = s["name"].split(",")[0].strip()
    case_dir = CASES_ROOT / slug(s["name"])
    log = Path(f"pipeline_{slug(s['name'])}.log")
    stage, age = stage_from_log(log)
    posts, kept, photo, slides_done, videos = 0, 0, 0, 0, 0
    ids = []
    files = sorted(glob.glob(str(case_dir / "search" / "search_posts_*.json")), key=os.path.getmtime)
    if files:
        d = json.load(open(files[-1], encoding="utf-8"))
        ps = d.get("posts", [])
        kept = len(ps)
        ids = [str(p["post_id"]) for p in ps]
        photo = sum(1 for p in ps if (p.get("fields") or {}).get("post_type") == "photo")
        slides_done = sum(1 for p in ps if (p.get("fields") or {}).get("slides_text"))
        videos = sum(1 for i in ids if any((MEDIA / i).glob("video.*")))
    if log.exists():
        m = re.findall(r"Post limit: keeping \d+ of (\d+)|Unique posts after dedup: (\d+)", log.read_text(encoding="utf-8", errors="ignore"))
        if m:
            posts = max(int(a or b) for a, b in m)
    comments = 0
    for i in (ids or [p.name for p in (case_dir / "raw" / "posts").glob("*")] if (case_dir / "raw" / "posts").exists() else []):
        if any((case_dir / "raw" / "posts" / i).glob("thread_*_summary.json")):
            comments += 1
    st = load_state().get(s["name"], {}).get("status", "")
    live = "●" if age is not None and age < 300 else " "
    return [live, name, stage, posts or "", kept or "", f"{slides_done}/{photo}" if photo else ("0/0" if kept else ""),
            f"{videos}" if kept else "", f"{comments}/{kept}" if kept else "", st]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="schools_plan.txt")
    a = ap.parse_args()
    plan = load_plan(Path(a.plan))
    rows = [school_row(s) for s in plan]
    hdr = ["", "Школа", "Этап", "Найдено", "В работе", "Слайды", "Видео", "Комменты", "Статус"]
    w = [max(len(str(r[i])) for r in rows + [hdr]) for i in range(len(hdr))]
    print(datetime.now().strftime("%d.%m %H:%M"), "  ● = поток пишет лог прямо сейчас (последние 5 мин)")
    print("  ".join(str(h).ljust(w[i]) for i, h in enumerate(hdr)))
    for r in rows:
        print("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)))
    print("\nЭтапы: 1 поиск → 2 таблица постов → 3 слайды → 4 видео/Whisper → 5 комментарии → 6 итог.")
    print("Комменты = постов, по которым комментарии уже собраны / постов в работе.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
