#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
РАЗМЕТКА ПОСТОВ ЧЕРЕЗ GEMINI (ось «что говорится»): формат поста, подача каждой упомянутой школы,
сравнение с принижением, переманивание, реклама/промокод, аффилиация автора, тезис негатива.

Вход: label_input.jsonl.gz (посты с упоминанием школы в тексте: описание, хэштеги, речь, текст с экрана).
Выход: out/labels.jsonl (кэш, можно перезапускать) и РАЗМЕТКА.xlsx (листы Посты / Школа×пост / Сводка / Негатив).
Только текст, без картинок. Ключ — /teamproject/openrouter_key.txt.

    python server_label.py --limit 50          # проба
    python server_label.py                     # всё
    python server_label.py --xlsx-only         # только пересобрать таблицу из out/labels.jsonl
"""
from __future__ import annotations

import argparse, gzip, json, re, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
OUT = HERE / "out"
MODEL = "google/gemini-2.5-flash"
BASE = "https://openrouter.ai/api/v1"

SCHOOLS = ["Умскул", "ЕГЭленд", "Фоксфорд", "Сотка", "Вебиум", "Турбо ЕГЭ", "Skysmart", "MAXIMUM Education", "Лектариум",
           "99 Баллов", "100балльный репетитор", "Тетрика", "Точка Знаний", "Школково", "Lomonosov School", "Insperia",
           "NeoFamily", "Учи.ру"]

PROMPT = """Ты помогаешь исследователям размечать посты TikTok об онлайн-школах подготовки к ЕГЭ/ОГЭ.
Пост может быть рекламой, отзывом, мемом, учебным роликом и т.д. Названия школ могут быть искажены распознаванием речи
(«Омскул» = Умскул, «Егэлэнд» = ЕГЭленд, «сотку» = Сотка). Слова «максимум», «турбо», «сотка» бывают обычными словами —
считай их школой, только если по контексту это онлайн-школа.
Справочник школ (используй эти названия): {schools}. Другие онлайн-школы называй как в тексте.

Ответь СТРОГО JSON без пояснений:
{{
 "relevant": true/false,              // пост действительно про онлайн-школы подготовки (а не случайное совпадение слова)
 "format": "отзыв|сравнение|реклама/промокод|учебный контент|мем/юмор|событие (выпускной, эфир, результаты)|личный опыт/влог|жалоба/разоблачение|слив/перепродажа курсов|другое",
 "ad": true/false,                     // явное продвижение: промокод, реф.ссылка, «переходи в тг», «записывайся», скидка
 "promo_codes": ["..."],
 "author_affiliation": "официальный аккаунт школы|преподаватель школы|ученик/амбассадор школы|не аффилирован|неясно",
 "author_school": "школа, с которой автор явно связан, или пустая строка",
 "schools": [ {{"school": "...", "stance": "позитив|нейтрально|негатив", "thesis": "что говорится об этой школе, до 15 слов"}} ],
 "comparison": true/false,             // в посте сравниваются школы
 "belittling": true/false,             // одну школу хвалят за счёт другой / принижают конкурента
 "poaching": true/false,               // призыв уйти из одной школы / перейти в другую
 "negative_claim": "главный негативный тезис о какой-либо школе (до 20 слов) или пустая строка",
 "summary": "о чём пост, одна фраза до 20 слов"
}}

ПОСТ
Автор: @{author} ({nick}). Био: {bio}
Дата: {date}. Просмотры: {views}
Описание: {desc}
Хэштеги: {hashtags}
Речь в ролике: {speech}
Текст на экране / слайдах: {screen}
"""


def load_key():
    from ocr_vlm import load_key as lk
    return lk()


def ask(key, text, retries=4):
    import requests
    body = {"model": MODEL, "temperature": 0, "max_tokens": 2000, "response_format": {"type": "json_object"},
            "reasoning": {"enabled": False},
            "messages": [{"role": "user", "content": text}]}
    last = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{BASE}/chat/completions", json=body, timeout=120,
                              headers={"Authorization": f"Bearer {key}", "X-Title": "umschool-tiktok-labels"})
            if r.status_code in (402,):
                raise SystemExit("OpenRouter: недостаточно средств (402) — пополните баланс и перезапустите")
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"; time.sleep(4 * (attempt + 1)); continue
            r.raise_for_status()
            j = r.json()
            raw = j["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", raw, re.S)
            return json.loads(m.group(0) if m else raw), j.get("usage", {})
        except SystemExit:
            raise
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:150]}"; time.sleep(2 * (attempt + 1))
    raise RuntimeError(last)


def build_xlsx(posts, labels, path):
    import pandas as pd
    rows, long = [], []
    for p in posts:
        l = labels.get(p["post_id"])
        if not l or l.get("error"):
            continue
        rows.append({"post_id": p["post_id"], "датасет": p["src"], "ссылка": p["url"], "автор": p["author"], "дата": p["date"],
                     "просмотры": p["views"], "про школы": l.get("relevant"), "формат": l.get("format"), "реклама": l.get("ad"),
                     "промокоды": "; ".join(l.get("promo_codes") or []), "аффилиация автора": l.get("author_affiliation"),
                     "школа автора": l.get("author_school"),
                     "школы и подача": "; ".join(f"{s.get('school')}: {s.get('stance')}" for s in l.get("schools") or []),
                     "сравнение": l.get("comparison"), "принижение": l.get("belittling"), "переманивание": l.get("poaching"),
                     "негативный тезис": l.get("negative_claim"), "о чём": l.get("summary"),
                     "описание": p["desc"][:300], "речь": p["speech"][:500], "текст на экране": p["screen"][:300]})
        for s in l.get("schools") or []:
            long.append({"post_id": p["post_id"], "ссылка": p["url"], "автор": p["author"], "дата": p["date"], "просмотры": p["views"],
                         "школа": s.get("school"), "подача": s.get("stance"), "тезис": s.get("thesis"),
                         "аффилиация автора": l.get("author_affiliation"), "школа автора": l.get("author_school"),
                         "формат": l.get("format"), "сравнение": l.get("comparison"), "принижение": l.get("belittling"),
                         "переманивание": l.get("poaching"), "реклама": l.get("ad")})
    df, lg = pd.DataFrame(rows), pd.DataFrame(long)
    rel = df[df["про школы"] == True]
    lg = lg[lg.post_id.isin(set(rel.post_id))]
    summ = (lg.assign(n=1).pivot_table(index="школа", columns="подача", values="n", aggfunc="sum", fill_value=0)
            if len(lg) else pd.DataFrame())
    if len(summ):
        summ["всего"] = summ.sum(axis=1)
        own = lg[(lg["школа"] == lg["школа автора"])]
        other = lg[(lg["школа автора"].fillna("") != "") & (lg["школа"] != lg["школа автора"])]
        summ["негатив от аффилированных с ДРУГОЙ школой"] = other[other["подача"] == "негатив"].groupby("школа").size()
        summ["позитив от своих (амбассадоры/преподы)"] = own[own["подача"] == "позитив"].groupby("школа").size()
        summ["принижение (в постах, где школа есть)"] = lg[lg["принижение"] == True].groupby("школа").size()
        summ = summ.fillna(0).astype(int).sort_values("всего", ascending=False)
    neg = lg[lg["подача"] == "негатив"].sort_values(["школа", "дата"])
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        summ.to_excel(w, sheet_name="Сводка")
        lg.to_excel(w, sheet_name="Школа×пост", index=False)
        neg.to_excel(w, sheet_name="Негатив", index=False)
        rel.to_excel(w, sheet_name="Посты", index=False)
        df[df["про школы"] != True].to_excel(w, sheet_name="Не про школы", index=False)
    print(f"{path}: постов {len(df)}, про школы {len(rel)}, пар школа×пост {len(lg)}, негатив {len(neg)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--xlsx-only", action="store_true")
    a = ap.parse_args()
    posts = [json.loads(l) for l in gzip.open(HERE / "label_input.jsonl.gz", "rt", encoding="utf-8")]
    OUT.mkdir(exist_ok=True)
    cache = OUT / "labels.jsonl"
    labels = {}
    if cache.exists():
        for line in cache.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if not r.get("error"):
                    labels[r["post_id"]] = r
            except Exception:
                pass
    if not a.xlsx_only:
        key = load_key()
        if not key:
            sys.exit("Нет ключа: /teamproject/openrouter_key.txt")
        todo = [p for p in posts if p["post_id"] not in labels]
        if a.limit:
            todo = todo[: a.limit]
        print(f"Постов всего {len(posts)}, уже размечено {len(labels)}, в работе {len(todo)} (модель {MODEL})", flush=True)
        sch = ", ".join(SCHOOLS)
        tok = Counter(); lock = threading.Lock(); n = 0; t0 = time.monotonic()

        def one(p):
            text = PROMPT.format(schools=sch, **{k: (p.get(k) or "—") for k in
                                 ("author", "nick", "bio", "date", "views", "desc", "hashtags", "speech", "screen")})
            try:
                l, u = ask(key, text)
                l["post_id"] = p["post_id"]
                return l, u
            except SystemExit:
                raise
            except Exception as exc:
                return {"post_id": p["post_id"], "error": str(exc)[:200]}, {}

        with open(cache, "a", encoding="utf-8") as f, ThreadPoolExecutor(a.workers) as ex:
            for fut in as_completed([ex.submit(one, p) for p in todo]):
                l, u = fut.result()
                with lock:
                    f.write(json.dumps(l, ensure_ascii=False) + "\n"); f.flush(); n += 1
                    if not l.get("error"):
                        labels[l["post_id"]] = l
                    tok["in"] += u.get("prompt_tokens", 0); tok["out"] += u.get("completion_tokens", 0)
                    if n % 50 == 0 or n == len(todo):
                        cost = tok["in"] * 0.30e-6 + tok["out"] * 2.5e-6
                        print(f"[{n}/{len(todo)}] {n / max(1e-6, (time.monotonic() - t0) / 60):.0f}/мин | ~${cost:.2f} | "
                              f"{l.get('format', l.get('error', ''))} | {(l.get('summary') or '')[:60]}", flush=True)
    build_xlsx(posts, labels, HERE / "РАЗМЕТКА.xlsx")


if __name__ == "__main__":
    main()
