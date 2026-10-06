#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА MAC: два датасета в CSV — посты + комментарии отдельно.

  ДАТАСЕТЫ_CSV/ШКОЛЫ_посты.csv          перепись по школам (ИТОГ_ВСЕ_ШКОЛЫ.xlsx) + новые школы, найденные на Маке
                                        (ИТОГ_ВСЕ_ШКОЛЫ_plan_new_schools.xlsx). Посты и расшифровки, найденные сервером, НЕ берутся.
  ДАТАСЕТЫ_CSV/ШКОЛЫ_комментарии.csv    все комментарии и ответы к этим постам
  ДАТАСЕТЫ_CSV/ОБЩИЕ_ЕГЭ_посты.csv      общая выборка ЕГЭ (ОБЩИЕ_ЕГЭ_с_транскриптами.xlsx, лист «Все посты»)
  ДАТАСЕТЫ_CSV/ОБЩИЕ_ЕГЭ_комментарии.csv

Комментарии: server/comments_server/КОММЕНТАРИИ_сервер.csv (первый сбор), server/out_all/КОММЕНТАРИИ.csv (досбор ко всем
постам, из all_comments_result.tgz), cases/*/search/comments_all.csv (собранные на Маке). Дубли убираются по ID комментария.
Школы в постах и в комментариях пересчитываются по словарю school_aliases.tsv.

    python server/build_csv_datasets.py
"""
from __future__ import annotations

import glob, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "ДАТАСЕТЫ_CSV"

COMMENT_COLS = ["ID поста", "Уровень", "ID комментария", "ID родительского", "Автор (ник)", "Автор (имя)", "ID автора",
                "Время (UTC)", "Текст", "Лайки", "Ответов (по данным TikTok)", "Автор поста лайкнул", "Откуда"]
RENAME = {"Время комментария (UTC)": "Время (UTC)", "ID родительского комментария": "ID родительского",
          "Лайк автора поста": "Автор поста лайкнул"}


def schools_cols(df, fields):
    """Пересчёт школ по словарю: какие школы, как написано, только по искажению/контексту."""
    from school_match import find_schools, summarize
    fields = [f for f in fields if f in df.columns]
    res = [summarize(find_schools({f: r[f] for f in fields if isinstance(r[f], str) and r[f]}))
           for _, r in df[fields].iterrows()]
    df["Школы упомянуты (словарь)"] = [r[0] for r in res]
    df["Школы: как написано [поле, тип]"] = [r[1] for r in res]
    df["Школы только по искажению/контексту (проверить)"] = [r[2] for r in res]
    return df


def load_posts():
    import pandas as pd
    parts = []
    for f, src in (("ИТОГ_ВСЕ_ШКОЛЫ.xlsx", "перепись (13 школ)"), ("ИТОГ_ВСЕ_ШКОЛЫ_plan_new_schools.xlsx", "новые школы (Мак)")):
        p = ROOT / f
        if p.exists():
            d = pd.read_excel(p, sheet_name="Посты", dtype=str).fillna("")
            d["Набор"] = src
            parts.append(d); print(f"{f}: {len(d)} строк", flush=True)
    sch = pd.concat(parts, ignore_index=True)
    sch = sch.drop(columns=[c for c in sch.columns if c.startswith("Упомянута школа (")])
    # один пост — одна строка; школы плана объединяем
    plan = sch.groupby("ID поста")["Школы (план)"].apply(lambda s: "; ".join(dict.fromkeys(x for v in s for x in str(v).split("; ") if x)))
    nabor = sch.groupby("ID поста")["Набор"].apply(lambda s: "; ".join(dict.fromkeys(s)))
    sch = sch.drop_duplicates("ID поста").set_index("ID поста")
    sch["Школы (план)"] = plan; sch["Набор"] = nabor
    sch = sch.reset_index()
    print("школы: пересчитываю упоминания по словарю…", flush=True)
    sch = schools_cols(sch, ["Описание поста", "Хэштеги", "Субтитры", "Транскрипт (Whisper)", "Текст со слайдов",
                             "Школы на слайдах", "Текст на экране (видео)", "Школы на экране (видео)"])
    gen = pd.read_excel(ROOT / "ОБЩИЕ_ЕГЭ_с_транскриптами.xlsx", sheet_name="Все посты", dtype=str).fillna("")
    print(f"ОБЩИЕ_ЕГЭ_с_транскриптами.xlsx: {len(gen)} строк", flush=True)
    gen = gen.drop_duplicates("post_id")
    print("общие: пересчитываю упоминания по словарю…", flush=True)
    gen = schools_cols(gen, ["description", "hashtags", "subtitle_text", "Транскрипт (Whisper)", "slides_text",
                             "slides_schools", "Текст на экране / слайдах", "Школы на экране"])
    return sch, gen


def load_comments():
    import pandas as pd
    parts = []
    srcs = [(HERE / "out_all" / "КОММЕНТАРИИ.csv", "сервер (досбор)"),
            (HERE / "comments_server" / "КОММЕНТАРИИ_сервер.csv", "сервер")]
    srcs += [(Path(f), "Мак") for f in sorted(glob.glob(str(ROOT / "cases" / "*" / "search" / "comments_all.csv")))]
    for f, src in srcs:
        if not f.exists():
            print(f"  нет {f.relative_to(ROOT)} — пропускаю", flush=True); continue
        d = pd.read_csv(f, dtype=str, keep_default_na=False).rename(columns=RENAME)
        d["Откуда"] = src
        parts.append(d[[c for c in COMMENT_COLS if c in d.columns]])
        print(f"  {f.relative_to(ROOT)}: {len(d)} строк", flush=True)
    c = pd.concat(parts, ignore_index=True).fillna("")
    c["Уровень"] = c["Уровень"].replace({"1": "комментарий", "2": "ответ"})
    n = len(c)
    c = c[c["ID комментария"] != ""].drop_duplicates("ID комментария", keep="first")   # серверный (полный) сбор приоритетнее
    print(f"комментариев всего {n}, без дублей {len(c)}", flush=True)
    return c


def main():
    import pandas as pd
    from school_match import find_schools
    OUT.mkdir(exist_ok=True)
    sch, gen = load_posts()
    com = load_comments()
    print("ищу школы в тексте комментариев…", flush=True)
    com["Школы в комментарии"] = com["Текст"].map(
        lambda t: "; ".join(dict.fromkeys(h["school"] for h in find_schools({"t": t}) if h["kind"] != "author")) if t else "")

    for name, posts, idcol, urlcol, scol in (("ШКОЛЫ", sch, "ID поста", "Ссылка на пост", "Школы упомянуты (словарь)"),
                                             ("ОБЩИЕ_ЕГЭ", gen, "post_id", "canonical_url", "Школы упомянуты (словарь)")):
        ids = set(posts[idcol])
        cm = com[com["ID поста"].isin(ids)].copy()
        meta = posts.set_index(idcol)
        cm.insert(1, "Ссылка на пост", cm["ID поста"].map(meta[urlcol]))
        cm.insert(2, "Школы в посте", cm["ID поста"].map(meta[scol]))
        cm["В комментарии другая школа, чем в посте"] = [
            "да" if c and any(s not in (p or "").split("; ") for s in c.split("; ")) else ""
            for c, p in zip(cm["Школы в комментарии"], cm["Школы в посте"])]
        cnt = cm.groupby("ID поста").size()
        posts = posts.copy()
        posts["Комментариев собрано (с ответами)"] = posts[idcol].map(cnt).fillna(0).astype(int)
        posts.to_csv(OUT / f"{name}_посты.csv", index=False, encoding="utf-8-sig")
        cm.to_csv(OUT / f"{name}_комментарии.csv", index=False, encoding="utf-8-sig")
        print(f"\n{name}: постов {len(posts)}, из них с комментариями {(posts['Комментариев собрано (с ответами)'] > 0).sum()} | "
              f"комментариев и ответов {len(cm)} (ответов {(cm['Уровень'] == 'ответ').sum()}) | "
              f"с другой школой в комментарии {(cm['В комментарии другая школа, чем в посте'] == 'да').sum()}", flush=True)
    print(f"\nГотово: папка {OUT.name}/ — ШКОЛЫ_посты.csv, ШКОЛЫ_комментарии.csv, ОБЩИЕ_ЕГЭ_посты.csv, ОБЩИЕ_ЕГЭ_комментарии.csv")


if __name__ == "__main__":
    main()
