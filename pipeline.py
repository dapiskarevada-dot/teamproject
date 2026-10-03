#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
ЕДИНАЯ ТОЧКА ВХОДА: сбор TikTok по онлайн-школам целиком, одной командой.

    python pipeline.py                      # все школы из schools_plan.txt по очереди, лимит MAX_POSTS на школу
    python pipeline.py --parallel 4         # 4 школы одновременно (по одному аккаунту TikTok на поток)
    python pipeline.py --only Умскул,Фоксфорд
    python pipeline.py --test               # пробный прогон: 5 результатов на источник, 3 поста, 10 комментариев
    python pipeline.py --final-only         # только пересобрать итоговые книги из уже собранного
    python pipeline.py --school Фоксфорд --queries q.txt --hashtags h.txt   # одна школа «по-старому»

Для каждой школы (строка в schools_plan.txt) выполняется:
  1. Поиск по запросам + ленты хэштегов -> уникальные посты.
  2. Отбор: сначала посты, где школа упомянута в описании/хэштегах/авторе, потом остальные; не больше MAX_POSTS.
  3. Таблица постов: автор, дата создания аккаунта, статистика, хэштеги, по каким запросам найден.
  4. Карусели: слайды -> расшифровка нейросетью через API (ключ в openrouter_key.txt).
  5. Видео: субтитры TikTok + скачивание видео + Whisper (faster-whisper).
  6. Комментарии верхнего уровня + реплаи (уже собранные посты пропускаются).
  7. ИТОГ_<школа>.xlsx: Посты / Комментарии и реплаи / Сводка.  Школа помечается готовой в pipeline_state.json.
После всех школ: ИТОГ_ВСЕ_ШКОЛЫ.xlsx (все посты и комментарии с колонкой «Школа (план)»).
При обрыве просто запустите снова: готовые школы пропускаются, собранные комментарии не собираются повторно.

Аккаунты TikTok: добавляются один раз через 2_login.command (можно несколько подряд). Пул показывается при старте.
Параллельно можно запускать не больше потоков, чем залогиненных аккаунтов.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.platform.startswith("win"):
    os.environ.setdefault("PYTHONUTF8", "1")        # дочерние процессы печатают кириллицу без ошибок
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============================ НАСТРОЙКИ ПО УМОЛЧАНИЮ ============================
PLAN_FILE = "schools_plan.txt"    # школы + их запросы и хэштеги, по строке на школу (порядок = очередь)
MAX_POSTS = 0                     # лимит постов на школу для тяжёлых шагов; 0 = без лимита (объём задаёт окно по дате)
SINCE = "2025-10-01"              # окно по дате публикации (UTC), включительно; "" = без ограничения
UNTIL = "2026-10-01"
AUTHORS_MIN_POSTS = 2             # ленты авторов, у которых >= N постов о школе (амбассадоры, кураторы); 0 = выкл
AUTHOR_COUNT = 200                # постов читать из ленты автора
SAMPLE = 1000                     # глубина (--heavy): планка на школу (все посты с >=2 школами + страты official/ambassador/UGC); 0 = все
FRAMES = "on"                     # текст с кадров видео через API (on/off)
FRAMES_N = 5                      # кадров на видео
STALL_MIN = 20                    # сторож: поток без записей в логе дольше N минут считается зависшим и перезапускается
SEARCH_COUNT = 200                # результатов на запрос/хэштег (потолок TikTok ~200)
COMMENTS = 200                    # комментариев верхнего уровня на пост (+ все реплаи к ним)
WHISPER = "missing"               # missing | all | off  (off = без расшифровки речи; python transcribe_whisper.py — отдельно)
WHISPER_MODEL = "large-v3"          # полная large; turbo быстрее в ~3 раза, но чуть хуже русский
OCR_MODEL = "google/gemini-2.5-flash"
FETCH_AUTHOR = True               # дозапрашивать профиль автора, если в выдаче нет статистики
PARALLEL = 1                      # сколько школ собирать одновременно (<= числа аккаунтов в пуле)
STATE_FILE = "pipeline_state.json"
CASES_ROOT = Path("cases")
# ---- режим одной школы без плана (--school/--queries/--hashtags) ----
SCHOOL = "Умскул, Umschool"
QUERIES_FILE = "queries.txt"
HASHTAGS_FILE = "hashtags.txt"
# ===============================================================================


# ------------------------------------------------------------------ план школ
def slug(name: str) -> str:
    s = re.sub(r"[^\w]+", "_", name.split(",")[0].strip(), flags=re.U).strip("_")
    return s or "school"


def load_plan(path: Path):
    """[{name, aliases, queries, hashtags}] из schools_plan.txt."""
    plan = []
    if not path.exists():
        return plan
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        queries = [q.strip() for q in (parts[1] if len(parts) > 1 else "").split(";") if q.strip()]
        hashtags = [h.strip().lstrip("#") for h in (parts[2] if len(parts) > 2 else "").split(";") if h.strip()]
        authors = [x.strip().lstrip("@") for x in (parts[3] if len(parts) > 3 else "").split(";") if x.strip()]
        if not queries and not hashtags:
            queries = [name.split(",")[0].strip()]
        plan.append({"name": name, "aliases": name, "queries": queries, "hashtags": hashtags, "authors": authors})
    return plan


def read_list(path: str):
    p = Path(path)
    if not p.exists():
        return []
    return [l.strip().lstrip("#") for l in p.read_text(encoding="utf-8-sig").splitlines() if l.strip() and not l.startswith("#")]


# ------------------------------------------------------------------ состояние
def load_state():
    try:
        return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    Path(STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def mark(name, **kw):
    st = load_state()
    st.setdefault(name, {}).update(kw, updated=datetime.now().isoformat(timespec="seconds"))
    save_state(st)


# ------------------------------------------------------------------ аккаунты
def show_accounts(reset_locks=False):
    """Печатает активные аккаунты пула и возвращает их число (0, если пул недоступен)."""
    try:
        import asyncio
        from pytok.accounts import AccountsPool
        async def _go():
            pool = AccountsPool()
            active = await pool.get_active_accounts()
            if reset_locks:                       # снять «in use» и блокировки от прошлых оборванных запусков
                await pool.reset_locks()
                for acc in active:
                    try:
                        await pool.release_account(acc.username)
                    except Exception:
                        pass
            return active, await pool.get_inactive_accounts()
        active, inactive = asyncio.run(_go())
    except Exception as exc:
        print("Не удалось прочитать пул аккаунтов:", exc)
        return 0
    print(f"=== Аккаунты TikTok в пуле: активных {len(active)}, неактивных {len(inactive)}")
    for a in active:
        print("    ", getattr(a, "username", a))
    return len(active)


# ------------------------------------------------------------------ сбор одной школы
MEDIA_ROOT = CASES_ROOT / "tiktok_media"


def latest_posts_json(case_dir: Path):
    files = sorted(glob.glob(str(case_dir / "search" / "search_posts_*.json")), key=os.path.getmtime)
    return Path(files[-1]) if files else None


def merged_posts(case_dir: Path):
    """Все посты школы по всем прогонам (дедуп по ID, свежие поля побеждают, slides_/transcript_ не теряются)."""
    posts = {}
    for f in sorted(glob.glob(str(case_dir / "search" / "search_posts_*.json")), key=os.path.getmtime):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for p in d.get("posts", []):
            pid = str(p["post_id"]); fld = dict(p.get("fields") or {})
            q = set(fld.get("matched_queries") or [])
            if pid in posts:
                old = posts[pid]; q |= set(old.get("matched_queries") or [])
                for k, v in old.items():
                    if (k.startswith("slides_") or k.startswith("transcript_") or k.startswith("screen_") or k == "subtitle_text") and not fld.get(k):
                        fld[k] = v
            fld["matched_queries"] = sorted(q); posts[pid] = fld
    return posts


def all_school_aliases():
    """{короткое имя: [алиасы]} из schools.txt — для подсчёта упоминаний других школ."""
    out = {}
    try:
        from ocr_vlm import load_schools
        for line in load_schools():
            parts = [x.strip() for x in line.split(",") if x.strip()]
            if parts:
                out[parts[0]] = [x.lower() for x in parts if len(x) >= 4]
    except Exception:
        pass
    return out


def schools_in_fields(f: dict, aliases_by_school):
    import re
    blob = " ".join(str(f.get(k) or "") for k in ("description", "hashtags", "subtitle_text", "transcript_whisper",
                                                   "slides_text", "slides_schools", "screen_text", "screen_schools", "author_username", "author_bio")).lower()
    found = []
    for name, al in aliases_by_school.items():
        if any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(a), blob) for a in al):
            found.append(name)
    return found


def choose_sample(school, posts: dict, n: int, seed: int = 42):
    """Стратифицированная выборка для глубины: все посты с >=2 школами + пропорционально из страт
    official / ambassador (автор с >=5 постами о школе) / ugc. n=0 — все посты."""
    import random, re
    aliases = all_school_aliases()
    short = school["name"].split(",")[0].strip()
    own = [a.strip().lower() for a in school["name"].split(",") if a.strip() and len(a.strip()) >= 4]
    def mentions_own(f):
        blob = " ".join(str(f.get(k) or "") for k in ("description", "hashtags", "subtitle_text", "transcript_whisper", "slides_text",
                                                       "slides_schools", "screen_text", "author_username", "author_nickname", "author_bio")).lower()
        return any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(a), blob) for a in own)
    relevant = {pid: f for pid, f in posts.items() if mentions_own(f)}
    print(f"[{short}] постов всего {len(posts)}, с упоминанием школы {len(relevant)} — выборка только из них", flush=True)
    posts = relevant
    ids = list(posts.keys())
    if n <= 0 or len(ids) <= n:
        def prio(pid):
            f = posts[pid]
            others = [x for x in schools_in_fields(f, aliases) if x != short]
            try:
                views = int(f.get("play_count") or f.get("views") or 0)
            except Exception:
                views = 0
            return (0 if others else 1, -views)
        ids.sort(key=prio)      # сначала посты с >=2 школами, затем по просмотрам — при обрыве самое ценное уже собрано
        return ids, {"all_relevant": len(ids), "with_2plus_schools": sum(1 for pid in ids if prio(pid)[0] == 0)}
    official = {u.lower() for u in (school.get("authors") or [])}
    by_author = {}
    for pid, f in posts.items():
        by_author.setdefault((f.get("author_username") or "").lower(), []).append(pid)
    strata = {"official": [], "ambassador": [], "ugc": []}
    must = []
    for pid, f in posts.items():
        u = (f.get("author_username") or "").lower()
        others = [x for x in schools_in_fields(f, aliases) if x != short]
        if others:
            must.append(pid); continue
        if u in official or any(a in u for a in own if len(a) >= 4):
            strata["official"].append(pid)
        elif len(by_author.get(u, [])) >= 5:
            strata["ambassador"].append(pid)
        else:
            strata["ugc"].append(pid)
    rnd = random.Random(seed)
    rest = max(0, n - len(must))
    total = sum(len(v) for v in strata.values()) or 1
    chosen = list(must)
    for k, v in strata.items():
        take = min(len(v), round(rest * len(v) / total))
        chosen += rnd.sample(v, take)
    # добить до n из ugc, если округление недобрало
    pool = [x for x in strata["ugc"] + strata["ambassador"] + strata["official"] if x not in set(chosen)]
    while len(chosen) < n and pool:
        chosen.append(pool.pop(rnd.randrange(len(pool))))
    info = {"must_2plus_schools": len(must), **{k: sum(1 for x in chosen if x in set(v)) for k, v in strata.items()}, "total": len(chosen)}
    return chosen, info


def run_heavy(a, school):
    """Глубина по уже собранным постам: выборка -> видео + комментарии/реплаи (Whisper и кадры — потом, одним процессом)."""
    case_dir = CASES_ROOT / slug(school["name"])
    pj = latest_posts_json(case_dir)
    if not pj:
        print(f"[{school['name']}] нет собранных постов (cases/{slug(school['name'])}/search) — сначала ПЕРЕПИСЬ"); return 2, case_dir
    posts = merged_posts(case_dir)
    ids, info = choose_sample(school, posts, a.sample)
    # посты, которых нет в последнем json (из старых прогонов), собрать в единый файл для --posts-json
    merged_file = case_dir / "search" / "posts_merged_for_depth.json"
    json.dump({"posts": [{"post_id": pid, "canonical_url": f.get("video_url"), "post_type_inferred": f.get("post_type"),
                          "username": f.get("author_username"), "matched_queries": f.get("matched_queries"), "fields": f}
                         for pid, f in posts.items()],
               "coverage": (json.load(open(pj, encoding="utf-8")).get("coverage") or {}), "authors": []},
              open(merged_file, "w", encoding="utf-8"), ensure_ascii=False)
    ids_file = case_dir / "sample_ids.txt"
    ids_file.write_text("\n".join(ids) + "\n", encoding="utf-8")
    json.dump({"n": a.sample, "chosen": len(ids), "strata": info}, open(case_dir / "sample_info.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    cmd = [sys.executable, "-u", "collect_tiktok_search_threads.py",
           "--posts-json", str(merged_file), "--only-ids", str(ids_file), "--skip-collected",
           "--comments", str(a.comments), "--whisper", "off", "--ocr-model", a.ocr_model, "--case-dir", str(case_dir),
           "--prefer", school["aliases"], "--max-posts", "0"]
    if a.no_images:
        cmd.append("--no-images")
    if a.no_ocr:
        cmd.append("--no-ocr")
    if a.no_videos:
        cmd.append("--no-videos")
    if a.search_only:
        cmd.append("--search-only")
    print(f"\n{'=' * 78}\n=== ГЛУБИНА: {school['name']}  |  постов {len(posts)}, в выборке {len(ids)} {info}\n{'=' * 78}", flush=True)
    print("RUN:", " ".join(cmd), flush=True)
    mark(school["name"], status="running", started=datetime.now().isoformat(timespec="seconds"), case_dir=str(case_dir), stage="heavy")
    return subprocess.call(cmd), case_dir


def run_transcriptions(a, plan):
    """После всех потоков, ОДНИМ процессом: Whisper (где нет субтитров TikTok) и текст с кадров — по постам выборки."""
    targets_whisper, targets_frames = set(), set()
    for s in plan:
        case_dir = CASES_ROOT / slug(s["name"])
        f = case_dir / "sample_ids.txt"
        if not f.exists():
            continue
        ids = {l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip()}
        posts = merged_posts(case_dir)
        for pid in ids:
            fl = posts.get(pid) or {}
            if fl.get("post_type") == "photo":
                continue
            targets_frames.add(pid)
            if a.whisper == "all" or (a.whisper == "missing" and not fl.get("subtitle_text")):
                targets_whisper.add(pid)
    if a.whisper != "off" and targets_whisper:
        try:
            import transcribe_whisper as tw
            print(f"\n=== Whisper ({a.whisper_model}) одним процессом: {len(targets_whisper)} видео", flush=True)
            tw.transcribe_dir(MEDIA_ROOT, post_ids=targets_whisper, model_name=a.whisper_model)
        except Exception as exc:
            print("Whisper пропущен:", type(exc).__name__, exc)
    if a.frames != "off" and targets_frames:
        try:
            import video_frames_ocr as vf
            print(f"\n=== Текст с кадров видео: {len(targets_frames)} видео", flush=True)
            vf.transcribe_dir(MEDIA_ROOT, post_ids=targets_frames, model=a.ocr_model, n_frames=a.frames_n)
        except Exception as exc:
            print("Текст с кадров пропущен:", type(exc).__name__, exc)


def apply_caches(rows_by_id: dict):
    """Подтянуть в поля постов кэши расшифровок: transcript.whisper.json и frames.*.vlm.json."""
    for pid, f in rows_by_id.items():
        d = MEDIA_ROOT / "raw" / "posts" / pid
        if not d.exists():
            continue
        if not f.get("transcript_whisper"):
            c = d / "transcript.whisper.json"
            if c.exists():
                try:
                    j = json.loads(c.read_text(encoding="utf-8"))
                    if j.get("text"):
                        f["transcript_whisper"] = j["text"]; f["transcript_source"] = f.get("transcript_source") or "whisper"
                    elif not f.get("transcript_source"):
                        f["transcript_source"] = "whisper: речи нет"
                except Exception:
                    pass
        if not f.get("screen_text"):
            for c in d.glob("frames.*.vlm.json"):
                try:
                    j = json.loads(c.read_text(encoding="utf-8"))
                    f["screen_text"] = j.get("text") or ""; f["screen_schools"] = "; ".join(j.get("schools") or []); f["screen_promo"] = j.get("promo") or ""
                except Exception:
                    pass
                break


def run_collect(a, school):
    case_dir = CASES_ROOT / slug(school["name"])
    cmd = [sys.executable, "-u", "collect_tiktok_search_threads.py",
           "--search-count", str(a.search_count), "--comments", str(a.comments),
           "--max-posts", str(a.max_posts), "--prefer", school["aliases"], "--skip-collected",
           "--whisper", a.whisper, "--whisper-model", a.whisper_model, "--ocr-model", a.ocr_model,
           "--case-dir", str(case_dir),
           "--authors-min-posts", str(a.authors_min_posts), "--author-count", str(a.author_count)]
    if a.since:
        cmd += ["--since", a.since]
    if a.until:
        cmd += ["--until", a.until]
    for u in school.get("authors") or []:
        cmd += ["--author", u]
    for q in school["queries"]:
        cmd += ["--query", q]
    for h in school["hashtags"]:
        cmd += ["--hashtag", h]
    if a.fetch_author:
        cmd.append("--fetch-author")
    for flag in ("search_only", "no_images", "no_ocr", "no_videos"):
        if getattr(a, flag):
            cmd.append("--" + flag.replace("_", "-"))
    print(f"\n{'=' * 78}\n=== ШКОЛА: {school['name']}  |  запросов {len(school['queries'])}, хэштегов {len(school['hashtags'])}, лимит постов {a.max_posts or 'нет'}\n{'=' * 78}", flush=True)
    print("RUN:", " ".join(cmd), flush=True)
    mark(school["name"], status="running", started=datetime.now().isoformat(timespec="seconds"), case_dir=str(case_dir))
    return subprocess.call(cmd), case_dir


# ------------------------------------------------------------------ итоговые книги
def build_final_workbook(school_name: str, case_dir: Path):
    """ИТОГ_<школа>.xlsx из всех прогонов в case_dir (дедуп постов по ID, свежие поля побеждают)."""
    import pandas as pd
    from tiktok_fields import COLUMNS
    from export_comments_table import export_all

    short = school_name.split(",")[0].strip()
    posts = merged_posts(case_dir)
    apply_caches(posts)
    sample_file = case_dir / "sample_ids.txt"
    sample = {l.strip() for l in sample_file.read_text(encoding="utf-8").splitlines() if l.strip()} if sample_file.exists() else set()
    aliases_all = all_school_aliases()
    for pid, f in posts.items():
        f["in_sample"] = bool(sample) and pid in sample
        f["schools_mentioned"] = "; ".join(schools_in_fields(f, aliases_all))
    rows = list(posts.values())
    if not rows:
        print(f"[{short}] нет данных для итоговой книги"); return None
    keys = [k for k in COLUMNS if any(r.get(k) not in (None, "", []) for r in rows)]
    dfp = pd.DataFrame([{COLUMNS[k]: ("; ".join(r[k]) if isinstance(r.get(k), list) else r.get(k)) for k in keys} for r in rows])

    aliases = [x.strip().lower() for x in school_name.split(",") if x.strip()]
    def mentions(r):
        blob = " ".join(str(r.get(k) or "") for k in ("description", "hashtags", "slides_text", "slides_schools", "transcript_whisper", "subtitle_text")).lower()
        return any(al in blob for al in aliases)
    mention_col = f"Упомянута школа ({short})" if len(aliases) <= 3 else "Упомянута какая-либо школа из списка"
    dfp.insert(0, "Школа (план)", short)
    dfp[mention_col] = [mentions(r) for r in rows]

    export_all(case_dir)
    cpath = case_dir / "search" / "comments_all.xlsx"
    dfc = pd.read_excel(cpath) if cpath.exists() else pd.DataFrame()
    if not dfc.empty:
        dfc.insert(0, "Школа (план)", short)
        dfp["Комментарии собраны"] = dfp["ID поста"].astype(str).isin(set(dfc["ID поста"].astype(str)))

    def cnt(col, val=None):
        if col not in dfp: return 0
        if val is not None:
            return int((dfp[col] == val).sum())
        s = dfp[col]
        return int((s.notna() & (s.astype(str).str.strip() != "")).sum())
    summary = pd.DataFrame({"Показатель": [
        "Постов всего", "из них video", "из них photo (карусели)", "с текстом слайдов",
        "с транскриптом Whisper", "с субтитрами TikTok", f"упомянута {short}",
        "с собранными комментариями", "Комментариев верхнего уровня", "Реплаев"],
        "Значение": [len(dfp), cnt("Тип поста", "video"), cnt("Тип поста", "photo"), cnt("Текст со слайдов"),
                     cnt("Транскрипт (Whisper)"), cnt("Субтитры"), int(dfp[mention_col].sum()),
                     int(dfp["Комментарии собраны"].sum()) if "Комментарии собраны" in dfp else 0,
                     int((dfc["Уровень"] == 1).sum()) if not dfc.empty else 0, int((dfc["Уровень"] == 2).sum()) if not dfc.empty else 0]})
    out = Path(f"ИТОГ_{short}.xlsx")
    with pd.ExcelWriter(out) as w:
        dfp.to_excel(w, sheet_name="Посты", index=False)
        (dfc if not dfc.empty else pd.DataFrame({"ID поста": []})).to_excel(w, sheet_name="Комментарии и реплаи", index=False)
        summary.to_excel(w, sheet_name="Сводка", index=False)
        cov = load_coverage(case_dir)
        if cov:
            coverage_frames(cov, short)[0].to_excel(w, sheet_name="Покрытие", index=False)
    print(f"\nИТОГ [{short}]: {out}"); print(summary.to_string(index=False))
    return out, dfp, dfc, summary


def load_coverage(case_dir: Path):
    """Отчёт о покрытии из последнего прогона (search_posts_*.json -> coverage)."""
    files = sorted(glob.glob(str(case_dir / "search" / "search_posts_*.json")), key=os.path.getmtime)
    for f in reversed(files):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if d.get("coverage"):
            d["coverage"]["window"] = d.get("window"); d["coverage"]["run"] = Path(f).name
            return d["coverage"]
    return None


def coverage_frames(cov, short):
    """(лист «Покрытие» для школы, строка для общей сводки)."""
    import pandas as pd
    rows = [{"Канал": r["channel"], "Тип": r["type"], "Улов": r["caught"], "Новых": r["new"], "Новых, % от накопленного": r["new_pct"]}
            for r in cov.get("channels", [])]
    w = cov.get("window") or {}
    rows += [{}, {"Канал": "Найдено уникальных (в окне)", "Улов": cov.get("found")},
             {"Канал": "Поиск / хэштеги / в обоих", "Улов": f"{cov.get('search')} / {cov.get('hashtags')} / {cov.get('both')}"},
             {"Канал": "Видимо через поиск+хэштеги", "Улов": cov.get("visible_search_hashtags")},
             {"Канал": "Ленты авторов дали сверх этого", "Улов": cov.get("authors_extra")},
             {"Канал": "Оценка всего видимого (N ≈ n1·n2/m)", "Улов": cov.get("estimate")},
             {"Канал": "Покрытие, %", "Улов": cov.get("coverage_pct")},
             {"Канал": "Последний канал добавил, %", "Улов": cov.get("last_channel_new_pct")},
             {"Канал": "Окно", "Улов": f"{w.get('since') or '…'} — {w.get('until') or '…'}"},
             {"Канал": "Вердикт", "Улов": cov.get("verdict")}]
    line = {"Школа": short, "Найдено": cov.get("found"), "Через поиск+хэштеги": cov.get("visible_search_hashtags"), "Оценка видимого": cov.get("estimate"),
            "Покрытие видимого, %": cov.get("coverage_pct"), "Сверх из лент авторов": cov.get("authors_extra"),
            "Последний канал, % нового": cov.get("last_channel_new_pct"), "Каналов": len(cov.get("channels", [])), "Вердикт": cov.get("verdict")}
    return pd.DataFrame(rows), line


def build_all_schools_workbook(results):
    """ИТОГ_ВСЕ_ШКОЛЫ.xlsx: все посты, все комментарии, сводка по школам."""
    import pandas as pd
    parts = [r for r in results if r]
    if not parts:
        return None
    posts = pd.concat([r[1] for r in parts], ignore_index=True)
    comments = pd.concat([r[2] for r in parts if not r[2].empty], ignore_index=True) if any(not r[2].empty for r in parts) else pd.DataFrame({"ID поста": []})
    sv = []
    for r in parts:
        d = dict(zip(r[3]["Показатель"], r[3]["Значение"]))
        sv.append({"Школа": r[1]["Школа (план)"].iloc[0], "Постов": d.get("Постов всего", 0), "video": d.get("из них video", 0),
                   "photo": d.get("из них photo (карусели)", 0), "с текстом слайдов": d.get("с текстом слайдов", 0),
                   "с Whisper": d.get("с транскриптом Whisper", 0), "с комментариями": d.get("с собранными комментариями", 0),
                   "Комментариев": d.get("Комментариев верхнего уровня", 0), "Реплаев": d.get("Реплаев", 0)})
    out = Path("ИТОГ_ВСЕ_ШКОЛЫ.xlsx")
    with pd.ExcelWriter(out) as w:
        posts.to_excel(w, sheet_name="Посты", index=False)
        comments.to_excel(w, sheet_name="Комментарии и реплаи", index=False)
        pd.DataFrame(sv).to_excel(w, sheet_name="Сводка по школам", index=False)
        cv = []
        for r in parts:
            short = r[1]["Школа (план)"].iloc[0]
            cov = load_coverage(CASES_ROOT / slug(short))
            if cov:
                cv.append(coverage_frames(cov, short)[1])
        if cv:
            pd.DataFrame(cv).to_excel(w, sheet_name="Покрытие", index=False)
            print("\nПОКРЫТИЕ:"); print(pd.DataFrame(cv).to_string(index=False))
    print("\nОБЩИЙ ИТОГ:", out); print(pd.DataFrame(sv).to_string(index=False))
    return out


# ------------------------------------------------------------------ не давать компьютеру уснуть
def keep_awake():
    """Windows: системный флаг «не спать» на время работы (на Mac это делает caffeinate в .command)."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000040)
            print("=== Windows: режим «не засыпать» включён на время сбора")
        except Exception as exc:
            print("Не удалось включить режим «не засыпать»:", exc)


# ------------------------------------------------------------------ оркестрация
def process_school(a, school):
    """Полный цикл для одной школы (вызывается в основном процессе или в дочернем при --parallel)."""
    case_dir = CASES_ROOT / slug(school["name"])
    rc = 0
    if not a.final_only:
        rc, case_dir = run_heavy(a, school) if a.heavy else run_collect(a, school)
        if rc != 0:
            print(f"\n[{school['name']}] сбор завершился с кодом {rc}; собираю ИТОГ из того, что есть", flush=True)
    res = build_final_workbook(school["name"], case_dir)
    n = len(res[1]) if res else 0
    mark(school["name"], status="done" if rc == 0 else f"error rc={rc}", posts=n,
         finished=datetime.now().isoformat(timespec="seconds"), workbook=str(res[0]) if res else None)
    return rc, res


def child_args(a, school_name):
    """Аргументы для дочернего процесса одной школы (при --parallel)."""
    cmd = [sys.executable, "-u", __file__, "--one", school_name, "--plan", a.plan,
           "--max-posts", str(a.max_posts), "--search-count", str(a.search_count), "--comments", str(a.comments),
           "--whisper", a.whisper, "--whisper-model", a.whisper_model, "--ocr-model", a.ocr_model,
           "--since", a.since, "--until", a.until, "--authors-min-posts", str(a.authors_min_posts), "--author-count", str(a.author_count)]
    for flag in ("search_only", "no_images", "no_ocr", "no_videos", "final_only", "redo", "heavy"):
        if getattr(a, flag):
            cmd.append("--" + flag.replace("_", "-"))
    cmd += ["--sample", str(a.sample), "--frames", a.frames, "--frames-n", str(a.frames_n)]
    if not a.fetch_author:
        cmd.append("--no-fetch-author")
    return cmd


def run_parallel(a, todo):
    """До a.parallel школ одновременно; каждая — отдельный процесс и свой аккаунт из пула.
    Если потоку не достался аккаунт (NoAccountError), школа возвращается в очередь и ждёт."""
    running, queue = [], list(todo)
    retries = {}
    while queue or running:
        while queue and len(running) < a.parallel:
            s = queue.pop(0)
            logname = f"pipeline_{slug(s['name'])}.log"
            log = open(logname, "a", encoding="utf-8")
            print(f"=== старт потока: {s['name']}  (лог {logname})", flush=True)
            p = subprocess.Popen(child_args(a, s["name"]), stdout=log, stderr=subprocess.STDOUT)
            running.append((s, p, log, logname))
            time.sleep(25)        # браузеры стартуют не одновременно
        for item in list(running):
            s, p, log, logname = item
            # сторож: лог потока не менялся STALL_MIN минут -> поток завис, перезапускаем (продолжит с места)
            try:
                idle = time.time() - Path(logname).stat().st_mtime
            except Exception:
                idle = 0
            if p.poll() is None and idle > STALL_MIN * 60:
                print(f"=== {s['name']}: нет записей в логе {int(idle // 60)} мин — перезапускаю поток", flush=True)
                try:
                    p.kill()
                except Exception:
                    pass
                log.close(); running.remove(item)
                queue.insert(0, s)
                continue
            if p.poll() is not None:
                log.close(); running.remove(item)
                tail = ""
                try:
                    tail = Path(logname).read_text(encoding="utf-8", errors="ignore")[-4000:]
                except Exception:
                    pass
                if p.returncode != 0 and "NoAccountError" in tail and retries.get(s["name"], 0) < 20:
                    retries[s["name"]] = retries.get(s["name"], 0) + 1
                    print(f"=== {s['name']}: свободного аккаунта нет — вернул в очередь (попытка {retries[s['name']]}), жду 2 мин", flush=True)
                    queue.append(s)
                    if a.parallel > 1:
                        a.parallel -= 1          # аккаунтов меньше, чем думали — сужаем параллель
                        print(f"=== параллельных потоков теперь {a.parallel}", flush=True)
                    time.sleep(120)
                else:
                    print(f"=== поток завершён: {s['name']} (код {p.returncode})", flush=True)
        time.sleep(5)


class _Tee:
    """stdout и в консоль, и в файл (для Windows, где нет tee)."""
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8"); self.c = sys.stdout
    def write(self, d):
        self.c.write(d); self.f.write(d); self.f.flush()
    def flush(self):
        self.c.flush(); self.f.flush()


def main():
    if "--log" in sys.argv:
        sys.argv.remove("--log")
        sys.stdout = sys.stderr = _Tee(f"night_{datetime.now():%Y%m%d_%H%M}.log")
    ap = argparse.ArgumentParser(description="Весь конвейер одной командой (все школы из плана)")
    ap.add_argument("--plan", default=PLAN_FILE, help="файл плана школ")
    ap.add_argument("--only", default="", help="только эти школы из плана, через запятую")
    ap.add_argument("--one", default="", help="(служебное) одна школа из плана, в дочернем процессе")
    ap.add_argument("--max-posts", type=int, default=MAX_POSTS)
    ap.add_argument("--since", default=SINCE, help="окно по дате публикации: с YYYY-MM-DD ('' = без)")
    ap.add_argument("--until", default=UNTIL, help="окно по дате публикации: по YYYY-MM-DD ('' = без)")
    ap.add_argument("--authors-min-posts", type=int, default=AUTHORS_MIN_POSTS)
    ap.add_argument("--author-count", type=int, default=AUTHOR_COUNT)
    ap.add_argument("--parallel", type=int, default=PARALLEL, help="школ одновременно (<= аккаунтов в пуле); 0 = по числу аккаунтов")
    ap.add_argument("--redo", action="store_true", help="заново пройти и уже готовые школы")
    ap.add_argument("--school", default="", help="режим одной школы без плана (с --queries/--hashtags)")
    ap.add_argument("--queries", default=QUERIES_FILE)
    ap.add_argument("--hashtags", default=HASHTAGS_FILE)
    ap.add_argument("--search-count", type=int, default=SEARCH_COUNT)
    ap.add_argument("--comments", type=int, default=COMMENTS)
    ap.add_argument("--whisper", choices=["missing", "all", "off"], default=WHISPER)
    ap.add_argument("--whisper-model", default=WHISPER_MODEL)
    ap.add_argument("--ocr-model", default=OCR_MODEL)
    ap.add_argument("--fetch-author", dest="fetch_author", action="store_true", default=FETCH_AUTHOR)
    ap.add_argument("--no-fetch-author", dest="fetch_author", action="store_false")
    ap.add_argument("--search-only", action="store_true", help="без комментариев")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--no-videos", action="store_true")
    ap.add_argument("--test", action="store_true", help="маленький прогон: 5 результатов, 3 поста, 10 комментариев")
    ap.add_argument("--final-only", action="store_true", help="только собрать ИТОГ из уже собранного")
    ap.add_argument("--heavy", action="store_true", help="глубина по уже собранным постам: выборка -> видео + комментарии/реплаи, затем Whisper и текст с кадров")
    ap.add_argument("--sample", type=int, default=SAMPLE, help="размер выборки на школу для глубины (0 = все посты)")
    ap.add_argument("--frames", choices=["on", "off"], default=FRAMES, help="текст с кадров видео через API")
    ap.add_argument("--frames-n", type=int, default=FRAMES_N, help="кадров на видео")
    a = ap.parse_args()
    if a.test:
        a.search_count, a.comments, a.max_posts = 5, 10, 3

    # --- режим одной школы без плана ---------------------------------------------
    if a.school:
        school = {"name": a.school, "aliases": a.school, "queries": read_list(a.queries), "hashtags": read_list(a.hashtags)}
        rc, _ = process_school(a, school)
        return rc

    plan = load_plan(Path(a.plan))
    if not plan:
        print(f"План {a.plan} не найден или пуст. Либо создайте его, либо запустите с --school/--queries/--hashtags."); return 2

    # --- дочерний процесс одной школы (--parallel) ---------------------------------
    if a.one:
        sch = next((s for s in plan if s["name"] == a.one or s["name"].split(",")[0].strip() == a.one), None)
        if not sch:
            print("Школа не найдена в плане:", a.one); return 2
        rc, _ = process_school(a, sch)
        return rc

    # --- основной процесс: очередь школ --------------------------------------------
    only = {x.strip().lower() for x in a.only.split(",") if x.strip()}
    state = load_state()
    todo, skipped = [], []
    for s in plan:
        short = s["name"].split(",")[0].strip()
        if only and short.lower() not in only and s["name"].lower() not in only:
            continue
        if not a.redo and not a.final_only and state.get(s["name"], {}).get("status") == "done":
            skipped.append(short); continue
        todo.append(s)
    print(f"План: {len(plan)} школ; к обработке {len(todo)}" + (f"; уже готовы (пропуск): {', '.join(skipped)}" if skipped else ""))
    print("Лимит постов на школу:", a.max_posts or "нет", "| результатов на запрос:", a.search_count, "| комментариев на пост:", a.comments)
    if not a.final_only:
        n_acc = show_accounts(reset_locks=True)
        if a.parallel <= 0:                       # --parallel 0 = по числу аккаунтов
            a.parallel = max(1, n_acc)
        if a.parallel > 1 and n_acc and a.parallel > n_acc:
            print(f"ВНИМАНИЕ: --parallel {a.parallel} больше, чем аккаунтов в пуле ({n_acc}); ставлю {n_acc}")
            a.parallel = max(1, n_acc)

    rc_all = 0
    if not a.final_only:
        keep_awake()
    if a.parallel > 1 and len(todo) > 1 and not a.final_only:
        run_parallel(a, todo)
    else:
        for s in todo:
            rc, _ = process_school(a, s)
            rc_all = rc_all or rc

    if a.heavy and not a.final_only:
        run_transcriptions(a, [s for s in plan if not only or s["name"].split(",")[0].strip().lower() in only or s["name"].lower() in only])

    # общий итог по всем школам, у которых есть данные (включая готовые ранее)
    results = []
    for s in plan:
        case_dir = CASES_ROOT / slug(s["name"])
        if (case_dir / "search").exists():
            try:
                results.append(build_final_workbook(s["name"], case_dir))
            except Exception as exc:
                print(f"[{s['name']}] ИТОГ не собран: {type(exc).__name__}: {exc}")
    build_all_schools_workbook(results)
    print("\nСостояние по школам:", STATE_FILE)
    return rc_all


if __name__ == "__main__":
    sys.exit(main())
