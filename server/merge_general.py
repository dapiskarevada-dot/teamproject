#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Общая (discovery) выборка + транскрипты с GPU-сервера -> одна таблица для фильтрации.

    python server/merge_general.py all_posts_dedup.csv out/transcripts.jsonl
    python server/merge_general.py all_posts_dedup.csv out.tgz          # можно сразу архив с сервера

Результат: ОБЩИЕ_ЕГЭ_с_транскриптами.xlsx
  лист «Все посты»   — строки из CSV за окно 01.10.2025–01.10.2026 + «Транскрипт (Whisper)», «Whisper статус», «Школы (упоминания)»,
                       «Школ упомянуто», «Промокод», «Сравнение школ»
  лист «Про школы»   — только строки, где упомянута хотя бы одна школа из schools.txt
                       (описание / хэштеги / субтитры / Whisper / текст на экране и слайдах / автор)
  Текст с картинок берётся из screen_text.jsonl (внутри архива или рядом с файлом транскриптов). — кандидаты на комментарии и реплаи
  лист «Сводка»      — сколько постов упоминают каждую школу, сколько со сравнением, промокодом
Работает на Mac и Windows, нужны pandas + openpyxl. Школы берутся из schools.txt рядом с репозиторием.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import tarfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
XL_MAX = 32000
SINCE, UNTIL = "2025-10-01", "2026-10-01"     # окно, как у переписи школ; строки вне окна отбрасываются
csv.field_size_limit(10**9)

TEXT_FIELDS = ("description", "hashtags", "subtitle_text", "transcript_whisper", "slides_text", "slides_schools",
               "author_username", "author_nickname", "author_bio")
PROMO_RE = re.compile(r"промокод|promo|промик|скидк[аи] по коду|по коду", re.I)
COMPARE_RE = re.compile(r"\b(или|vs|против|лучше|хуже|сравнени|перешл[аи]|ушл[аи] из|топ онлайн|рейтинг)\b", re.I)


def load_schools():
    out = {}
    for line in (ROOT / "schools.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",") if x.strip()]
        out[parts[0]] = [x.lower() for x in parts if len(x) >= 4]
    return out


def load_transcripts(path: Path):
    """post_id -> (text, status). Принимает transcripts.jsonl или out.tgz."""
    res = {}
    if path.suffix == ".tgz":
        with tarfile.open(path) as t:
            m = [x for x in t.getmembers() if x.name.endswith("transcripts.jsonl")]
            if not m:
                raise SystemExit("в архиве нет transcripts.jsonl")
            lines = t.extractfile(m[0]).read().decode("utf-8").splitlines()
    else:
        lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        err = str(r.get("error") or "")
        if err.startswith("download:"):
            st = "не скачано"
        elif err:
            st = "ошибка"
        elif r.get("text"):
            st = "речь"
        else:
            st = "нет речи"
        res[str(r["post_id"])] = (r.get("text") or "", st)
    return res


def load_screen(tr: Path):
    """post_id -> (text, schools, promo) из screen_text.jsonl: внутри архива или файлом рядом с транскриптами."""
    lines = []
    if tr.suffix == ".tgz":
        with tarfile.open(tr) as t:
            m = [x for x in t.getmembers() if x.name.endswith("screen_text.jsonl")]
            if m:
                lines = t.extractfile(m[0]).read().decode("utf-8").splitlines()
    for cand in (tr.parent / "screen_text.jsonl", Path("server") / "screen_text.jsonl"):
        if not lines and cand.exists():
            lines = cand.read_text(encoding="utf-8").splitlines()
    res = {}
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not r.get("error"):
            res[str(r["post_id"])] = (r.get("text") or "", "; ".join(r.get("schools") or []), r.get("promo") or "")
    return res


def main():
    if len(sys.argv) < 3:
        print(__doc__); return 2
    src, tr = Path(sys.argv[1]), Path(sys.argv[2])
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("ОБЩИЕ_ЕГЭ_с_транскриптами.xlsx")
    import pandas as pd
    sys.path.insert(0, str(ROOT))
    from school_match import find_schools, summarize, author_school
    trans = load_transcripts(tr)
    screen = load_screen(tr)
    rows_all = list(csv.DictReader(open(src, encoding="utf-8-sig")))
    rows = [r for r in rows_all if SINCE <= (r.get("create_time") or "")[:10] <= UNTIL]
    print(f"постов в CSV: {len(rows_all)}, в окне {SINCE}..{UNTIL}: {len(rows)}, транскриптов с сервера: {len(trans)}, текст с картинок: {len(screen)}")
    per_school = Counter(); cmp_n = promo_n = 0
    for r in rows:
        t, st = trans.get(str(r["post_id"]), ("", "нет"))
        if not t and r.get("transcript_whisper"):
            t, st = r["transcript_whisper"], "речь (старый)"
        r["Транскрипт (Whisper)"] = t
        r["Whisper статус"] = st if r.get("post_type") != "photo" else "карусель"
        sc, ss, sp = screen.get(str(r["post_id"]), ("", "", ""))
        r["Текст на экране / слайдах"] = sc; r["Школы на экране"] = ss; r["Промо на экране"] = sp
        blob = " ".join(str(r.get(k) or "") for k in TEXT_FIELDS).lower() + " " + t.lower() + " " + " ".join((sc, ss, sp)).lower()
        flds = {k: r.get(k) for k in TEXT_FIELDS if r.get(k)}
        flds.update({"transcript_whisper": t, "screen_text": sc, "screen_schools": ss,
                     "author_username": r.get("author_username"), "author_bio": r.get("author_bio") or r.get("author_signature")})
        names, forms, weak = summarize(find_schools(flds))
        found = [x for x in names.split("; ") if x]
        r["Школы (упоминания)"] = names
        r["Школы: как написано [поле, тип]"] = forms
        r["Школы только по искажению/контексту (проверить)"] = weak
        r["Автор — известный препод/амбассадор"] = author_school(r.get("author_username") or "")
        r["Школ упомянуто"] = len(found)
        r["Промокод"] = "да" if PROMO_RE.search(blob) else ""
        r["Сравнение школ"] = "да" if (len(found) >= 2 or (found and COMPARE_RE.search(blob))) else ""
        for n in found:
            per_school[n] += 1
        cmp_n += r["Сравнение школ"] == "да"; promo_n += r["Промокод"] == "да"
    df = pd.DataFrame(rows)
    # старые пустые колонки из CSV убираем, новые текстовые ставим сразу после описания — чтобы их было видно
    df = df.drop(columns=[c for c in ("transcript_whisper", "transcript_source", "caption", "url") if c in df.columns])
    front = ["post_id", "canonical_url", "author_username", "create_time", "description", "Транскрипт (Whisper)", "Whisper статус",
             "Текст на экране / слайдах", "subtitle_text", "slides_text", "Школы (упоминания)", "Школ упомянуто",
             "Школы: как написано [поле, тип]", "Школы только по искажению/контексту (проверить)", "Автор — известный препод/амбассадор",
             "Сравнение школ", "Промокод", "Школы на экране", "Промо на экране"]
    front = [c for c in front if c in df.columns]
    df = df[front + [c for c in df.columns if c not in front]]
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda x: x[:XL_MAX] if isinstance(x, str) and len(x) > XL_MAX else x)
    rel = df[df["Школ упомянуто"] > 0]
    st = Counter(df["Whisper статус"])
    summary = pd.DataFrame(
        [{"Школа": n, "Постов с упоминанием": c} for n, c in per_school.most_common()]
        + [{"Школа": "— всего постов", "Постов с упоминанием": len(df)},
           {"Школа": "— упоминают хотя бы одну школу", "Постов с упоминанием": len(rel)},
           {"Школа": "— со сравнением", "Постов с упоминанием": cmp_n},
           {"Школа": "— с промокодом", "Постов с упоминанием": promo_n}]
        + [{"Школа": f"— Whisper: {k}", "Постов с упоминанием": v} for k, v in st.most_common()])
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Все посты", index=False)
        rel.to_excel(w, sheet_name="Про школы", index=False)
        summary.to_excel(w, sheet_name="Сводка", index=False)
    print(f"{out}: всего {len(df)}, про школы {len(rel)}, сравнение {cmp_n}, промокод {promo_n} | Whisper: {dict(st)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
