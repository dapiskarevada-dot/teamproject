#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
ЕДИНАЯ ТОЧКА ВХОДА: сбор TikTok по онлайн-школе целиком, одной командой.

    python pipeline.py                 # настройки из шапки файла
    python pipeline.py --school Фоксфорд --queries queries_foxford.txt --hashtags hashtags_foxford.txt
    python pipeline.py --test          # маленький прогон: 5 результатов на источник, 10 комментариев

Шаги (всё внутри collect_tiktok_search_threads.py и модулей рядом):
  1. Поиск по запросам (queries.txt) + ленты хэштегов (hashtags.txt) -> уникальные посты.
  2. Таблица постов: автор, дата создания аккаунта, статистика, хэштеги, по каким запросам найден.
  3. Карусели (фото-посты): слайды -> расшифровка нейросетью через API -> колонки «Текст со слайдов»,
     «Школы на слайдах», «Контекст слайдов», «Сравнение школ на слайдах».
  4. Видео: субтитры TikTok + скачивание видео + Whisper -> «Транскрипт (Whisper)», «Источник транскрипта».
  5. Комментарии верхнего уровня + реплаи -> таблица комментариев.
  6. Итоговая книга ИТОГ_<школа>.xlsx: Посты (дедуп по всем прогонам), Комментарии и реплаи, Сводка.

Для другой школы: --school, свои queries/hashtags файлы (или одноимённые в папке school/<имя>/).
Ключ API для слайдов: openrouter_key.txt. Whisper: faster-whisper (1c_whisper_setup.command).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

# ============================ НАСТРОЙКИ ПО УМОЛЧАНИЮ ============================
SCHOOL = "Умскул"                 # для имени итоговой книги и колонки «Упомянута школа»
QUERIES_FILE = "queries.txt"      # поисковые запросы, по одному в строке
HASHTAGS_FILE = "hashtags.txt"    # хэштеги (без #), по одному в строке
SEARCH_COUNT = 200                # результатов на запрос/хэштег (потолок TikTok ~200)
COMMENTS = 200                    # комментариев верхнего уровня на пост (+ все реплаи к ним)
WHISPER = "missing"               # missing | all | off  (off = расшифровать позже 11_whisper.command)
WHISPER_MODEL = "large-v3-turbo"
OCR_MODEL = "google/gemini-2.5-flash"
FETCH_AUTHOR = True               # дозапрашивать профиль автора, если в выдаче нет статистики
CASE_DIR = Path("cases") / "tiktok_search_threads"
# ===============================================================================


def run_collect(a):
    cmd = [sys.executable, "-u", "collect_tiktok_search_threads.py",
           "--search-count", str(a.search_count), "--comments", str(a.comments),
           "--whisper", a.whisper, "--whisper-model", a.whisper_model, "--ocr-model", a.ocr_model,
           "--case-dir", str(a.case_dir)]
    if Path(a.queries).exists():
        cmd += ["--queries-file", a.queries]
    if Path(a.hashtags).exists():
        cmd += ["--hashtags-file", a.hashtags]
    if a.fetch_author:
        cmd.append("--fetch-author")
    if a.search_only:
        cmd.append("--search-only")
    if a.no_images:
        cmd.append("--no-images")
    if a.no_ocr:
        cmd.append("--no-ocr")
    if a.no_videos:
        cmd.append("--no-videos")
    print("RUN:", " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def build_final_workbook(a):
    """ИТОГ_<школа>.xlsx из всех прогонов в case_dir (дедуп постов по ID, свежие поля побеждают)."""
    import pandas as pd
    from tiktok_fields import COLUMNS
    from export_comments_table import export_all

    posts = {}
    for f in sorted(glob.glob(str(a.case_dir / "search" / "search_posts_*.json")), key=os.path.getmtime):
        for p in json.load(open(f, encoding="utf-8")).get("posts", []):
            pid = str(p["post_id"]); fld = dict(p.get("fields") or {})
            q = set(fld.get("matched_queries") or [])
            if pid in posts:
                old = posts[pid]; q |= set(old.get("matched_queries") or [])
                for k, v in old.items():           # не терять слайды/транскрипты из прошлых прогонов
                    if (k.startswith("slides_") or k.startswith("transcript_") or k == "subtitle_text") and not fld.get(k):
                        fld[k] = v
            fld["matched_queries"] = sorted(q); posts[pid] = fld
    rows = list(posts.values())
    if not rows:
        print("Нет данных для итоговой книги"); return None
    keys = [k for k in COLUMNS if any(r.get(k) not in (None, "", []) for r in rows)]
    dfp = pd.DataFrame([{COLUMNS[k]: ("; ".join(r[k]) if isinstance(r.get(k), list) else r.get(k)) for k in keys} for r in rows])

    # Упомянута ли целевая школа где-либо: описание, хэштеги, слайды, транскрипт
    aliases = [x.strip().lower() for x in a.school.split(",")] + [a.school.lower()]
    def mentions(r):
        blob = " ".join(str(r.get(k) or "") for k in ("description", "hashtags", "slides_text", "slides_schools", "transcript_whisper", "subtitle_text")).lower()
        return any(al and al in blob for al in aliases)
    mention_col = f"Упомянута школа ({a.school.split(',')[0]})"
    dfp[mention_col] = [mentions(r) for r in rows]

    _, _ = export_all(a.case_dir)
    cpath = a.case_dir / "search" / "comments_all.xlsx"
    dfc = pd.read_excel(cpath) if cpath.exists() else pd.DataFrame()
    if not dfc.empty:
        dfp["Комментарии собраны"] = dfp["ID поста"].astype(str).isin(set(dfc["ID поста"].astype(str)))

    def cnt(col, val=None):
        if col not in dfp: return 0
        if val is not None:
            return int((dfp[col] == val).sum())
        s = dfp[col]
        return int((s.notna() & (s.astype(str).str.strip() != "")).sum())
    summary = pd.DataFrame({"Показатель": [
        "Постов всего", "из них video", "из них photo (карусели)", "с текстом слайдов",
        "с транскриптом Whisper", "с субтитрами TikTok", f"упомянута {a.school.split(',')[0]}",
        "с собранными комментариями", "Комментариев верхнего уровня", "Реплаев"],
        "Значение": [len(dfp), cnt("Тип поста", "video"), cnt("Тип поста", "photo"), cnt("Текст со слайдов"),
                     cnt("Транскрипт (Whisper)"), cnt("Субтитры"), int(dfp[mention_col].sum()),
                     int(dfp["Комментарии собраны"].sum()) if "Комментарии собраны" in dfp else 0,
                     int((dfc["Уровень"] == 1).sum()) if not dfc.empty else 0, int((dfc["Уровень"] == 2).sum()) if not dfc.empty else 0]})
    out = Path(f"ИТОГ_{a.school.split(',')[0]}.xlsx")
    with pd.ExcelWriter(out) as w:
        dfp.to_excel(w, sheet_name="Посты", index=False)
        (dfc if not dfc.empty else pd.DataFrame({"ID поста": []})).to_excel(w, sheet_name="Комментарии и реплаи", index=False)
        summary.to_excel(w, sheet_name="Сводка", index=False)
    print("\nИТОГ:", out); print(summary.to_string(index=False))
    return out


def main():
    ap = argparse.ArgumentParser(description="Весь конвейер одной командой")
    ap.add_argument("--school", default=SCHOOL)
    ap.add_argument("--queries", default=QUERIES_FILE)
    ap.add_argument("--hashtags", default=HASHTAGS_FILE)
    ap.add_argument("--search-count", type=int, default=SEARCH_COUNT)
    ap.add_argument("--comments", type=int, default=COMMENTS)
    ap.add_argument("--whisper", choices=["missing", "all", "off"], default=WHISPER)
    ap.add_argument("--whisper-model", default=WHISPER_MODEL)
    ap.add_argument("--ocr-model", default=OCR_MODEL)
    ap.add_argument("--case-dir", type=Path, default=CASE_DIR)
    ap.add_argument("--fetch-author", action="store_true", default=FETCH_AUTHOR)
    ap.add_argument("--search-only", action="store_true", help="без комментариев")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--no-videos", action="store_true")
    ap.add_argument("--test", action="store_true", help="маленький прогон: 5 результатов, 10 комментариев")
    ap.add_argument("--final-only", action="store_true", help="только собрать ИТОГ из уже собранного")
    a = ap.parse_args()
    if a.test:
        a.search_count, a.comments = 5, 10

    rc = 0
    if not a.final_only:
        rc = run_collect(a)
        if rc != 0:
            print(f"\nСбор завершился с кодом {rc}; собираю ИТОГ из того, что есть")
    build_final_workbook(a)
    return rc


if __name__ == "__main__":
    sys.exit(main())
