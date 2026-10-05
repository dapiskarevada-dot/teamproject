#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Поиск упоминаний онлайн-школ по словарю school_aliases.tsv (бренд / ASR-искажения / слова с контекстом / исключения).

    from school_match import find_schools, schools_list
    hits = find_schools({"description": "...", "transcript": "...", "screen": "..."})
    # -> [{"school": "Сотка", "form": "в сотке", "kind": "brand", "field": "transcript"}, ...]
    names = sorted({h["school"] for h in hits if h["kind"] != "author"})

Командная строка — проверить текст:  python school_match.py "учусь в сотке и в омскуле"
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
DICT = HERE / "school_aliases.tsv"
B = r"(?<![a-zа-я0-9])"   # левая граница слова (#, @ и _ границей считаются: @rus_webium)

CONTEXT = re.compile(
    r"школ|курс|онлайн|online|препод|учител|куратор|промокод|промик|промо|занима|подготов|готовл|готовил|учусь|училась|учился|"
    r"вебинар|платформ|репетитор|тариф|подписк|мастер[- ]?групп|абонемент|перешл|перешел|ушл[аи] из|ушел из|наставник|"
    r"ученик|эфир|канал|тгк|телеграм|скидк|записыва|бесплатн\w* урок|разбор|домашк|дз\b|сотрудни|амбассадор")
WINDOW = 60


def norm(s) -> str:
    return str(s or "").lower().replace("ё", "е")


@lru_cache(maxsize=1)
def load(path: str = str(DICT)):
    rules, rejects = [], []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        school, pat, kind = parts[0].strip(), parts[1].strip(), parts[2].strip()
        rx = re.compile(pat if pat.startswith(("#", "^", "(?<")) else B + "(?:" + pat + ")")
        if kind == "reject":
            rejects.append(rx)
        else:
            rules.append((school, kind, rx))
    return rules, rejects


def schools_list():
    rules, _ = load()
    return list(dict.fromkeys(s for s, k, _ in rules if s != "-"))


def find_schools(fields: dict, keep_all=False):
    """fields: {имя_поля: текст}. Возвращает по одному попаданию на (школа, поле) — первое найденное."""
    rules, rejects = load()
    out, seen = [], set()
    for fname, text in fields.items():
        t = norm(text)
        if not t.strip():
            continue
        for rx in rejects:
            t = rx.sub(lambda m: " " * len(m.group(0)), t)
        sure = [(mm.start(), mm.end()) for school, kind, rx in rules if kind in ("brand", "asr") for mm in rx.finditer(t)]
        for school, kind, rx in rules:
            if (school, fname) in seen and not keep_all:
                continue
            for m in rx.finditer(t):
                if kind in ("context", "list"):
                    win = t[max(0, m.start() - WINDOW): m.start()] + " " + t[m.end(): m.end() + WINDOW]
                    near_school = any(abs(s0 - m.start()) <= WINDOW and not (s0 <= m.start() < e0) for s0, e0 in sure)
                    if not (near_school or (kind == "context" and CONTEXT.search(win))):
                        continue
                out.append({"school": school, "form": m.group(0).strip(), "kind": kind, "field": fname})
                seen.add((school, fname))
                break
    return out


def summarize(hits):
    """-> (школы через «; », формы через «; », только_слабые: школы, найденные лишь по ASR/контексту)"""
    hits = [h for h in hits if h["kind"] != "author"]
    by = {}
    for h in hits:
        by.setdefault(h["school"], []).append(h)
    names = list(by)
    forms = "; ".join(f"{s}: " + ", ".join(sorted({f'{h["form"]} [{h["field"]}{"" if h["kind"] == "brand" else ", " + h["kind"]}]' for h in hs}))
                      for s, hs in by.items())
    weak = [s for s, hs in by.items() if all(h["kind"] != "brand" for h in hs)]
    return "; ".join(names), forms, "; ".join(weak)


def author_school(username: str):
    rules, _ = load()
    u = norm(username)
    return "; ".join(dict.fromkeys(s for s, k, rx in rules if k == "author" and rx.search(u)))


if __name__ == "__main__":
    for h in find_schools({"text": " ".join(sys.argv[1:])}):
        print(h)


def canon(name: str) -> str:
    """Любое написание из планов/schools.txt («Maximum Education», «Турбо», «99 баллов») -> имя из словаря."""
    n = norm(name).split(",")[0].strip()
    for s in schools_list():
        if norm(s) == n or norm(s).startswith(n) or n.startswith(norm(s)):
            return s
    hits = [h for h in find_schools({"x": name}) if h["kind"] != "author"]
    return hits[0]["school"] if hits else name.split(",")[0].strip()


POST_FIELDS = ("description", "hashtags", "subtitle_text", "transcript_whisper", "slides_text", "slides_schools",
               "screen_text", "screen_schools", "author_username", "author_nickname", "author_bio")


def post_hits(f: dict, fields=POST_FIELDS):
    return find_schools({k: f.get(k) for k in fields if f.get(k)})
