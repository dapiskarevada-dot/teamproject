#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА MAC: одна таблица всех комментариев (сервер + собранные раньше на Маке) с данными поста.

    python server/merge_comments.py

Берёт:
  server/comments_server/КОММЕНТАРИИ_сервер.csv   (из comments_and_labels.tgz)
  server/comments_server/out/labels.jsonl          (разметка Gemini, если есть)
  server/comments_server/comment_list.csv          (группа поста A/B/C/D и почему)
  server/label_input.jsonl.gz                      (пост: автор, дата, просмотры, школы в посте)
  cases/*/search/comments_all.csv                  (комментарии, собранные на Маке)
Пишет рядом с репозиторием:
  КОММЕНТАРИИ_ВСЕ.csv  — все строки, без дублей по ID комментария
  КОММЕНТАРИИ_ВСЕ.xlsx — то же (если строк меньше миллиона) + лист «Посты» (сводка по постам) + «Школы в комментариях»
"""
from __future__ import annotations

import csv, glob, gzip, json, sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
csv.field_size_limit(10 ** 9)

COLS = ["ID поста", "Ссылка на пост", "Уровень", "ID комментария", "ID родительского", "Автор (ник)", "Автор (имя)",
        "ID автора", "Время (UTC)", "Текст", "Лайки", "Ответов (по данным TikTok)", "Автор поста лайкнул"]
MAC_MAP = {"Время комментария (UTC)": "Время (UTC)", "ID родительского комментария": "ID родительского",
           "Лайк автора поста": "Автор поста лайкнул"}


def main():
    import pandas as pd
    from school_match import find_schools
    cs = HERE / "comments_server"
    parts = []
    srv = cs / "КОММЕНТАРИИ_сервер.csv"
    if srv.exists():
        d = pd.read_csv(srv, dtype=str, keep_default_na=False)
        d["Откуда"] = "сервер"
        parts.append(d)
        print(f"сервер: {len(d)} строк")
    for f in sorted(glob.glob(str(ROOT / "cases" / "*" / "search" / "comments_all.csv"))):
        d = pd.read_csv(f, dtype=str, keep_default_na=False).rename(columns=MAC_MAP)
        d["Откуда"] = "Мак (" + Path(f).parts[-3] + ")"
        parts.append(d)
        print(f"Мак {Path(f).parts[-3]}: {len(d)} строк")
    df = pd.concat(parts, ignore_index=True)
    keep = COLS + ["Откуда"] + [c for c in ("Ответов собрано", "Закреплён автором", "Язык", "Ссылка на комментарий") if c in df.columns]
    df = df[[c for c in keep if c in df.columns]].fillna("")
    n0 = len(df)
    df = df[df["ID комментария"] != ""].drop_duplicates(subset=["ID комментария"], keep="first")
    print(f"всего {n0}, без дублей {len(df)}")

    # данные поста
    posts = {}
    li = HERE / "label_input.jsonl.gz"
    if li.exists():
        for l in gzip.open(li, "rt", encoding="utf-8"):
            p = json.loads(l)
            posts[p["post_id"]] = p
    tiers = {}
    cl = cs / "comment_list.csv"
    if cl.exists():
        for r in csv.DictReader(open(cl, encoding="utf-8")):
            tiers[r["post_id"]] = (r.get("приоритет", ""), r.get("почему", ""))
    labels = {}
    lf = cs / "out" / "labels.jsonl"
    if lf.exists():
        for l in lf.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(l)
                if not r.get("error"):
                    labels[r["post_id"]] = r
            except Exception:
                pass
    pid = df["ID поста"]
    g = lambda f: pid.map(lambda x: f(x) or "")
    df["Автор поста"] = g(lambda x: (posts.get(x) or {}).get("author"))
    df["Дата поста"] = g(lambda x: (posts.get(x) or {}).get("date"))
    df["Просмотры поста"] = g(lambda x: (posts.get(x) or {}).get("views"))
    df["Датасет"] = g(lambda x: (posts.get(x) or {}).get("src"))
    df["Школы в посте"] = g(lambda x: (posts.get(x) or {}).get("found"))
    df["Группа поста"] = g(lambda x: tiers.get(x, ("", ""))[0])
    df["Почему в группе"] = g(lambda x: tiers.get(x, ("", ""))[1])
    df["Gemini: формат поста"] = g(lambda x: (labels.get(x) or {}).get("format"))
    df["Gemini: подача школ"] = g(lambda x: "; ".join(f"{s.get('school')}: {s.get('stance')}" for s in (labels.get(x) or {}).get("schools") or []))
    df["Gemini: школа автора"] = g(lambda x: (labels.get(x) or {}).get("author_school"))
    df["Gemini: негативный тезис"] = g(lambda x: (labels.get(x) or {}).get("negative_claim"))
    df["Gemini: о чём пост"] = g(lambda x: (labels.get(x) or {}).get("summary"))
    # школы, упомянутые в самом комментарии
    print("ищу школы в тексте комментариев…", flush=True)
    df["Школы в комментарии"] = df["Текст"].map(
        lambda t: "; ".join(dict.fromkeys(h["school"] for h in find_schools({"t": t}) if h["kind"] != "author")) if t else "")
    df["В комментарии другая школа, чем в посте"] = [
        "да" if c and any(s not in (p or "").split("; ") for s in c.split("; ")) else ""
        for c, p in zip(df["Школы в комментарии"], df["Школы в посте"])]

    front = ["ID поста", "Ссылка на пост", "Автор поста", "Дата поста", "Просмотры поста", "Датасет", "Школы в посте",
             "Группа поста", "Уровень", "Текст", "Школы в комментарии", "В комментарии другая школа, чем в посте",
             "Лайки", "Время (UTC)", "Автор (ник)", "Автор (имя)", "ID автора", "ID комментария", "ID родительского"]
    df = df[[c for c in front if c in df.columns] + [c for c in df.columns if c not in front]]
    out_csv = ROOT / "КОММЕНТАРИИ_ВСЕ.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"{out_csv.name}: {len(df)} строк, постов {df['ID поста'].nunique()}", flush=True)

    # сводки
    df["_n"] = 1
    posts_sum = (df.groupby("ID поста").agg(**{
        "комментариев и ответов": ("_n", "sum"),
        "комментариев с другой школой": ("В комментарии другая школа, чем в посте", lambda s: (s == "да").sum()),
        "уникальных авторов": ("ID автора", "nunique")}).reset_index())
    meta = df.drop_duplicates("ID поста").set_index("ID поста")[
        ["Ссылка на пост", "Автор поста", "Дата поста", "Просмотры поста", "Школы в посте", "Группа поста", "Почему в группе",
         "Gemini: подача школ", "Gemini: о чём пост"]]
    posts_sum = posts_sum.join(meta, on="ID поста").sort_values("комментариев и ответов", ascending=False)
    sc = Counter()
    for c in df["Школы в комментарии"]:
        for s in filter(None, c.split("; ")):
            sc[s] += 1
    schools = pd.DataFrame(sc.most_common(), columns=["школа", "комментариев с упоминанием"])
    df = df.drop(columns="_n")
    if len(df) < 1_000_000:
        from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE as ILLEGAL
        clean = lambda x: ILLEGAL.sub("", x)[:32000] if isinstance(x, str) else x
        out_x = ROOT / "КОММЕНТАРИИ_ВСЕ.xlsx"
        print("пишу xlsx (несколько минут)…", flush=True)
        try:
            import xlsxwriter  # noqa — быстрее openpyxl на больших таблицах
            eng, kw = "xlsxwriter", {"engine_kwargs": {"options": {"strings_to_urls": False, "constant_memory": False}}}
        except ImportError:
            eng, kw = "openpyxl", {}
        with pd.ExcelWriter(out_x, engine=eng, **kw) as w:
            posts_sum.map(clean).to_excel(w, sheet_name="Посты", index=False)
            schools.to_excel(w, sheet_name="Школы в комментариях", index=False)
            df.map(clean).to_excel(w, sheet_name="Комментарии", index=False)
        print(f"{out_x.name}: листы Посты / Школы в комментариях / Комментарии", flush=True)
    print(schools.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
