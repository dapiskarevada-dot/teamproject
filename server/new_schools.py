#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА СЕРВЕРЕ (GPU-под): досбор по новым и пустым школам одной командой.

  1. discover   — поиск постов без аккаунтов через tikwm: запросы, хэштеги, аккаунты школ из new_schools_plan.txt,
                  + ленты авторов, у которых >= 2 поста о школе. Окно дат --since/--until. Уже собранные раньше
                  посты (label_input.jsonl.gz, links*.csv) пропускаются. Картинки каруселей скачиваются.
  2. transcribe — видео -> Whisper large-v3 + 5 кадров (server_transcribe.py), параллельно с шагом 3
  3. comments   — все комментарии и все ответы (server_comments.py --all-comments)
  4. table      — new/НОВЫЕ_ШКОЛЫ_посты.csv: пост + расшифровка + какие школы упомянуты
  5. pack       — new_schools_result.tgz (всё из папки new/, без видео) -> скачать через Jupyter

    python new_schools.py --probe                     # проверить, что tikwm ищет
    nohup python new_schools.py > new_schools.log 2>&1 &
    python new_schools.py --steps comments,table,pack # продолжить с нужного шага (всё докачивается с места)

Текст с экрана (OCR) здесь не делается — нужен баланс OpenRouter; кадры и слайды сохраняются в архив, их можно
распознать позже.
"""
from __future__ import annotations

import argparse, csv, glob, gzip, json, re, subprocess, sys, threading, time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
NEW = HERE / "new"
POSTS = NEW / "new_posts.csv"
FIELDS = ["post_id", "url", "post_type", "author_username", "author_nickname", "create_time", "description",
          "play_count", "digg_count", "comment_count", "share_count", "collect_count", "duration",
          "music_title", "music_original", "found_by", "schools_in_text", "images"]

_lock = threading.Lock()
_last = [0.0]
_sess = None


def tikwm(path, params, retries=4):
    """GET к tikwm с паузой ~1.1 с между запросами (бесплатный лимит)."""
    global _sess
    if _sess is None:
        from curl_cffi import requests as cr
        _sess = cr.Session(impersonate="chrome")
    err = ""
    for i in range(retries):
        with _lock:
            w = 1.15 - (time.monotonic() - _last[0])
            if w > 0:
                time.sleep(w)
            _last[0] = time.monotonic()
        try:
            r = _sess.get("https://www.tikwm.com" + path, params=params, timeout=40)
            j = r.json()
            if j.get("code") == 0:
                return j.get("data") or {}
            err = str(j.get("msg"))[:100]
            if "limit" in err.lower():
                time.sleep(5 * (i + 1)); continue
            return None if "not" in err.lower() else {}
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:80]}"
            time.sleep(3 * (i + 1))
    print(f"  tikwm {path} {params}: {err}", flush=True)
    return {}


def paged(path, params, max_items, key="videos"):
    cursor, out = 0, []
    while len(out) < max_items:
        d = tikwm(path, dict(params, count=30, cursor=cursor))
        if not d:
            break
        items = d.get(key) or []
        out += items
        more = d.get("hasMore", d.get("has_more"))
        nc = d.get("cursor")
        if not items or not more or nc in (None, cursor):
            break
        cursor = nc
    return out[:max_items]


def read_plan(path):
    plan = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split("|")] + ["", "", ""]
        sp = lambda s: [x.strip().lstrip("#@") for x in s.split(";") if x.strip()]
        plan.append({"school": p[0], "queries": sp(p[1]), "hashtags": sp(p[2]), "accounts": sp(p[3])})
    return plan


def known_ids():
    ids = set()
    li = HERE / "label_input.jsonl.gz"
    if li.exists():
        for l in gzip.open(li, "rt", encoding="utf-8"):
            ids.add(json.loads(l)["post_id"])
    for f in glob.glob(str(HERE / "links*.csv")) + glob.glob(str(HERE / "all_posts*.csv")):
        try:
            for r in csv.DictReader(open(f, encoding="utf-8-sig")):
                if r.get("post_id"):
                    ids.add(r["post_id"].strip())
        except Exception:
            pass
    return ids


def to_row(v, found_by):
    a = v.get("author") or {}
    m = v.get("music_info") or {}
    uid = a.get("unique_id") or ""
    pid = str(v.get("video_id") or v.get("id") or "")
    imgs = v.get("images") or []
    kind = "photo" if imgs else "video"
    ts = v.get("create_time") or 0
    return {"post_id": pid, "url": f"https://www.tiktok.com/@{uid}/{kind}/{pid}", "post_type": kind,
            "author_username": uid, "author_nickname": a.get("nickname") or "",
            "create_time": datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
            "description": v.get("title") or "", "play_count": v.get("play_count"), "digg_count": v.get("digg_count"),
            "comment_count": v.get("comment_count"), "share_count": v.get("share_count"), "collect_count": v.get("collect_count"),
            "duration": v.get("duration"), "music_title": m.get("title") or "", "music_original": m.get("original"),
            "found_by": found_by, "schools_in_text": "", "images": json.dumps(imgs) if imgs else ""}


def discover(a):
    from school_match import find_schools
    NEW.mkdir(exist_ok=True)
    plan = read_plan(a.plan)
    skip = known_ids()
    print(f"Школ в плане: {len(plan)}; уже собранных раньше постов (пропускаем): {len(skip)}", flush=True)
    rows = {}
    if POSTS.exists():      # продолжение
        for r in csv.DictReader(open(POSTS, encoding="utf-8")):
            rows[r["post_id"]] = r
    in_window = lambda r: (not a.since or r["create_time"][:10] >= a.since) and (not a.until or r["create_time"][:10] <= a.until)

    def add(videos, found_by):
        n = 0
        for v in videos:
            r = to_row(v, found_by)
            if not r["post_id"] or r["post_id"] in skip or not in_window(r):
                continue
            if r["post_id"] in rows:
                if found_by not in rows[r["post_id"]]["found_by"]:
                    rows[r["post_id"]]["found_by"] += "; " + found_by
                continue
            rows[r["post_id"]] = r; n += 1
        return n

    def save():
        with open(POSTS, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, FIELDS); w.writeheader()
            for r in rows.values():
                w.writerow({k: r.get(k, "") for k in FIELDS})

    done_src = set(json.loads((NEW / "discover_done.json").read_text())) if (NEW / "discover_done.json").exists() else set()
    for s in plan:
        sch = s["school"]
        srcs = [("q", q) for q in s["queries"]] + [("h", h) for h in s["hashtags"]] + [("u", u) for u in s["accounts"]]
        for kind, val in srcs:
            tag = f"{sch}|{kind}|{val}"
            if tag in done_src:
                continue
            if kind == "q":
                vids = paged("/api/feed/search", {"keywords": val, "HD": 0}, a.max_per_query)
                lab = f"{sch}: запрос «{val}»"
            elif kind == "h":
                info = tikwm("/api/challenge/info", {"challenge_name": val}) or {}
                cid = info.get("id") or (info.get("challenge") or {}).get("id")
                vids = paged("/api/challenge/posts", {"challenge_id": cid}, a.max_per_hashtag) if cid else []
                lab = f"{sch}: #{val}"
            else:
                vids = paged("/api/user/posts", {"unique_id": val}, a.max_per_account)
                lab = f"{sch}: @{val}"
            n = add(vids, lab)
            print(f"  {lab}: получено {len(vids)}, новых в окне {n} | всего {len(rows)}", flush=True)
            done_src.add(tag); (NEW / "discover_done.json").write_text(json.dumps(sorted(done_src), ensure_ascii=False))
            save()

    # какие школы в тексте (описание + хэштеги) + ленты авторов с >= 2 постами о школе
    by_author = defaultdict(Counter)
    for r in rows.values():
        hs = [h["school"] for h in find_schools({"d": r["description"]}) if h["kind"] != "author"]
        r["schools_in_text"] = "; ".join(dict.fromkeys(hs))
        for h in hs:
            by_author[r["author_username"]][h] += 1
    plan_names = {s["school"] for s in plan}
    authors = [u for u, c in by_author.items() if u and any(n >= 2 for s, n in c.items() if s in plan_names)]
    print(f"Ленты авторов с >= 2 постами о новых школах: {len(authors)}", flush=True)
    for u in authors:
        tag = f"author|{u}"
        if tag in done_src:
            continue
        vids = paged("/api/user/posts", {"unique_id": u}, a.max_per_author)
        keep = []
        for v in vids:       # из ленты берём только посты, где есть какая-либо школа
            if any(h["kind"] != "author" for h in find_schools({"d": v.get("title") or ""})):
                keep.append(v)
        n = add(keep, f"лента @{u}")
        print(f"  лента @{u}: {len(vids)} постов, про школы {len(keep)}, новых {n} | всего {len(rows)}", flush=True)
        done_src.add(tag); (NEW / "discover_done.json").write_text(json.dumps(sorted(done_src), ensure_ascii=False))
        save()
    for r in rows.values():
        r["schools_in_text"] = "; ".join(dict.fromkeys(h["school"] for h in find_schools({"d": r["description"]}) if h["kind"] != "author"))
    save()

    # картинки каруселей (для OCR позже)
    photos = [r for r in rows.values() if r.get("images")]
    for r in photos:
        d = NEW / "out" / "slides" / r["post_id"]
        if d.exists() and any(d.iterdir()):
            continue
        d.mkdir(parents=True, exist_ok=True)
        for i, u in enumerate(json.loads(r["images"])[:20]):
            try:
                c = _sess.get(u, timeout=30).content
                (d / f"slide_{i:02d}.jpg").write_bytes(c)
            except Exception:
                pass
    c = Counter(s for r in rows.values() for s in r["schools_in_text"].split("; ") if s)
    print(f"НАЙДЕНО новых постов: {len(rows)} (видео {sum(1 for r in rows.values() if r['post_type'] == 'video')}, "
          f"карусели {len(photos)}). Школы в описаниях: {dict(c.most_common())}", flush=True)


def load_rows():
    return list(csv.DictReader(open(POSTS, encoding="utf-8")))


def start_transcribe(a):
    rows = [r for r in load_rows() if r["post_type"] == "video"]
    with open(NEW / "links_new.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["post_id", "url"])
        for r in rows:
            w.writerow([r["post_id"], r["url"]])
    print(f"Расшифровка: {len(rows)} видео -> new/out/transcripts.jsonl (лог new/transcribe.log)", flush=True)
    log = open(NEW / "transcribe.log", "a")
    return subprocess.Popen([sys.executable, "-u", str(HERE / "server_transcribe.py"), "links_new.csv", "--workers", "8",
                             "--impersonate", "chrome"], cwd=NEW, stdout=log, stderr=subprocess.STDOUT)


def start_comments(a):
    rows = load_rows()
    todo = [r for r in rows if str(r.get("comment_count") or "1") not in ("0", "0.0")]
    with open(NEW / "comment_plan.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["post_id", "url", "tier"])
        for r in sorted(todo, key=lambda r: -float(r.get("comment_count") or 0)):
            w.writerow([r["post_id"], r["url"], "N"])
    print(f"Комментарии: {len(todo)} постов (у {len(rows) - len(todo)} комментариев 0) -> new/comments_out (лог new/comments.log)", flush=True)
    log = open(NEW / "comments.log", "a")
    return subprocess.Popen([sys.executable, "-u", str(HERE / "server_comments.py"), "--plan", str(NEW / "comment_plan.csv"),
                             "--outdir", str(NEW / "comments_out"), "--all-comments", "--skip-ids", "",
                             "--workers", str(a.comment_workers)], cwd=HERE, stdout=log, stderr=subprocess.STDOUT)


def table(a):
    from school_match import find_schools
    tr = {}
    jl = NEW / "out" / "transcripts.jsonl"
    if jl.exists():
        for l in jl.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(l); tr[str(r.get("post_id"))] = r
            except Exception:
                pass
    ncom = Counter()
    cl = NEW / "comments_out" / "comments_done.jsonl"
    if cl.exists():
        for l in cl.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(l); ncom[r["post_id"]] = r.get("n") or 0
            except Exception:
                pass
    out = []
    for r in load_rows():
        t = tr.get(r["post_id"]) or {}
        text = t.get("text") or t.get("transcript") or ""
        hs = find_schools({"описание": r["description"], "речь": text})
        r = dict(r, **{"Транскрипт (Whisper)": text, "Whisper статус": ("карусель" if r["post_type"] == "photo" else "нет данных" if not t else
                                         "речь" if text else "не скачалось" if t.get("error") else "без речи"),
                       "Школы (описание + речь)": "; ".join(dict.fromkeys(h["school"] for h in hs if h["kind"] != "author")),
                       "Комментариев собрано": ncom.get(r["post_id"], "")})
        r.pop("images", None)
        out.append(r)
    import pandas as pd
    df = pd.DataFrame(out)
    df.to_csv(NEW / "НОВЫЕ_ШКОЛЫ_посты.csv", index=False, encoding="utf-8-sig")
    print(f"new/НОВЫЕ_ШКОЛЫ_посты.csv: {len(df)} постов, с расшифровкой {sum(1 for x in out if x['Транскрипт (Whisper)'])}, "
          f"с комментариями {sum(1 for x in out if x['Комментариев собрано'])}", flush=True)


def pack(a):
    files = ["new/new_posts.csv", "new/НОВЫЕ_ШКОЛЫ_посты.csv", "new/out", "new/comments_out", "new/comment_plan.csv"]
    files = [f for f in files if (HERE / f).exists()]
    subprocess.run(["tar", "czf", "new_schools_result.tgz", "--exclude=tmp_dl"] + files, cwd=HERE, check=True)
    size = (HERE / "new_schools_result.tgz").stat().st_size / 1e6
    print(f"ГОТОВО: server/new_schools_result.tgz ({size:.0f} МБ) — скачать через Jupyter", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default=str(HERE / "new_schools_plan.txt"))
    ap.add_argument("--steps", default="discover,transcribe,comments,table,pack")
    ap.add_argument("--since", default="2025-10-01", help="'' = без ограничения")
    ap.add_argument("--until", default="2026-10-01")
    ap.add_argument("--max-per-query", type=int, default=300)
    ap.add_argument("--max-per-hashtag", type=int, default=1000)
    ap.add_argument("--max-per-account", type=int, default=1000)
    ap.add_argument("--max-per-author", type=int, default=300)
    ap.add_argument("--comment-workers", type=int, default=16)
    ap.add_argument("--probe", action="store_true", help="проверить поиск tikwm и выйти")
    a = ap.parse_args()
    if a.probe:
        for path, p, key in (("/api/feed/search", {"keywords": "егэхаб"}, "videos"), ("/api/challenge/info", {"challenge_name": "egehub"}, None),
                             ("/api/user/posts", {"unique_id": "profimatika"}, "videos")):
            d = tikwm(path, dict(p, count=10, cursor=0))
            print(path, p, "->", (f"{len(d.get(key) or [])} постов, пример: {((d.get(key) or [{}])[0].get('title') or '')[:60]}"
                                  if key else f"id={(d or {}).get('id')}") if d else "ПУСТО/ОШИБКА", flush=True)
        return
    steps = set(x.strip() for x in a.steps.split(","))
    t0 = time.monotonic()
    if "discover" in steps:
        discover(a)
    procs = []
    if "transcribe" in steps:
        procs.append(("расшифровка", start_transcribe(a)))
    if "comments" in steps:
        procs.append(("комментарии", start_comments(a)))
    while any(p.poll() is None for _, p in procs):
        time.sleep(120)
        st = []
        for name, logf in (("расшифровка", "transcribe.log"), ("комментарии", "comments.log")):
            f = NEW / logf
            if f.exists():
                last = [l for l in f.read_text(encoding="utf-8", errors="ignore").splitlines() if l.startswith("[")][-1:]
                st.append(f"{name}: {last[0][:90] if last else '…'}")
        print(f"[{(time.monotonic() - t0) / 60:.0f} мин] " + " | ".join(st), flush=True)
    for name, p in procs:
        print(f"{name}: завершено, код {p.returncode}", flush=True)
    if "table" in steps:
        table(a)
    if "pack" in steps:
        pack(a)


if __name__ == "__main__":
    main()
