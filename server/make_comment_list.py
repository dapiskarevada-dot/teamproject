#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА СЕРВЕРЕ, после server_label.py: список постов для сбора комментариев по приоритету.
  A — принижение / переманивание / негатив о школе / сравнение школ (все);
  B — реклама или промокод с упоминанием школы (по просмотрам);
  C — остальные посты про школы: топ по просмотрам на каждую школу;
  D — (с --all) все остальные посты про школы, включая ещё не размеченные.
    python make_comment_list.py --max 3000 --per-school 100
    python make_comment_list.py --all          # все посты, порядок A → B → C → D, + comment_plan.csv для server_comments.py
→ comment_urls.txt (по одной ссылке в строке, в порядке приоритета) и comment_list.csv (почему выбран).
"""
import argparse, csv, gzip, json, re
from pathlib import Path
HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--max", type=int, default=3000)
ap.add_argument("--per-school", type=int, default=100)
ap.add_argument("--all", action="store_true", help="все посты про школы (уровень D — остальные), без лимита --max")
a = ap.parse_args()
posts = {}
for l in gzip.open(HERE / "label_input.jsonl.gz", "rt", encoding="utf-8"):
    p = json.loads(l); posts[p["post_id"]] = p
labels = {}
lab_file = HERE / "out" / "labels.jsonl"
for l in (lab_file.read_text(encoding="utf-8").splitlines() if lab_file.exists() else []):
    try:
        r = json.loads(l)
        if not r.get("error"):
            labels[r["post_id"]] = r
    except Exception:
        pass

def views(pid):
    try: return float(posts[pid].get("views") or 0)
    except Exception: return 0.0

A, B, C, D = [], [], {}, []
for pid, l in labels.items():
    if pid not in posts or not l.get("relevant"):
        continue
    sch = l.get("schools") or []
    neg = [s.get("school") for s in sch if s.get("stance") == "негатив"]
    if l.get("belittling") or l.get("poaching") or neg or l.get("comparison"):
        why = ", ".join(x for x, f in (("принижение", l.get("belittling")), ("переманивание", l.get("poaching")),
                                       ("негатив: " + "/".join(map(str, neg)), neg), ("сравнение", l.get("comparison"))) if f)
        A.append((pid, "A", why))
    elif l.get("ad"):
        B.append((pid, "B", "реклама/промокод"))
    else:
        for s in sch:
            C.setdefault(s.get("school"), []).append((pid, "C", f"топ по просмотрам: {s.get('school')}"))
A.sort(key=lambda x: -views(x[0])); B.sort(key=lambda x: -views(x[0]))
Cl = []
for sch, lst in C.items():
    lst = sorted(lst, key=lambda x: -views(x[0]))
    Cl += lst[: a.per_school]
    D += [(p, "D", "остальные посты про школы") for p, _, _ in lst[a.per_school:]]
# Посты без разметки Gemini (кончился баланс и т.п.) — уровень по правилам из текста.
NEG = re.compile(r"развод|лохотрон|скам|обман|кидал|не покупайте|не советую|не идите|не берите|верните деньги|возврат денег|"
                 r"разочаров|ужасн|отстой|худш|хуже|кринж|позор|стрем|зря потрат|деньги на ветер|перешл[аи] из|ушл[аи] из|ушел из|"
                 r"перешел из|сравни|vs|против|рейтинг|тир[- ]?лист|топ онлайн|какую школу|какая школа лучше|слив", re.I)
PROMO = re.compile(r"промокод|промик|по коду|скидк|реф(ерал)?ьн|записывайся|ссылка в (шапке|био|профиле)", re.I)
n_rule = 0
for pid, p in posts.items():
    if pid in labels:
        continue
    n_rule += 1
    text = " ".join(str(p.get(k) or "") for k in ("desc", "hashtags", "speech", "screen")).lower()
    found = [x for x in (p.get("found") or "").split("; ") if x]
    m = NEG.search(text)
    if len(found) >= 2 or m:
        A.append((pid, "A", "по правилам: " + ("школ " + str(len(found)) if len(found) >= 2 else "") + (f" «{m.group(0)}»" if m else "")))
    elif PROMO.search(text):
        B.append((pid, "B", "по правилам: реклама/промокод"))
    else:
        for sch in found[:1] or ["?"]:
            C.setdefault(sch, []).append((pid, "C", f"по правилам, топ по просмотрам: {sch}"))
A.sort(key=lambda x: -views(x[0])); B.sort(key=lambda x: -views(x[0]))
Cl = []; D = []
for sch, lst in C.items():
    lst = sorted(lst, key=lambda x: -views(x[0]))
    Cl += lst[: a.per_school]
    D += [(p, "D", "остальные посты про школы") for p, _, _ in lst[a.per_school:]]
Cl.sort(key=lambda x: -views(x[0]))
D.sort(key=lambda x: -views(x[0]))
print(f"размечено Gemini: {len(labels)}, уровень по правилам (без разметки): {n_rule}")
Cl.sort(key=lambda x: -views(x[0]))
seen, out = set(), []
for pid, tier, why in A + B + Cl + (D if a.all else []):
    if pid in seen: continue
    seen.add(pid); out.append((pid, tier, why))
out = out if a.all else out[: a.max]
(HERE / "comment_urls.txt").write_text("\n".join(posts[p]["url"] for p, _, _ in out) + "\n", encoding="utf-8")
with open(HERE / "comment_list.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["post_id", "приоритет", "почему", "просмотры", "автор", "ссылка", "о чём"])
    for p, t, why in out:
        w.writerow([p, t, why, posts[p].get("views"), posts[p].get("author"), posts[p].get("url"), (labels.get(p) or {}).get("summary")])
with open(HERE / "comment_plan.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["post_id", "url", "tier"])
    for p, t, _ in out:
        w.writerow([p, posts[p]["url"], t])
from collections import Counter
print(f"A (конфликт/негатив/сравнение): {len(A)}, B (реклама): {len(B)}, C (топ по школам): {len(Cl)}, D: {len(D) if a.all else 0}"
      f" → в списке {len(out)} {dict(Counter(t for _, t, _ in out))}")
print("comment_urls.txt, comment_list.csv, comment_plan.csv")
