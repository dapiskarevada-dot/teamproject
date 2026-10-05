#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА СЕРВЕРЕ, после server_label.py: список постов для сбора комментариев по приоритету.
  A — принижение / переманивание / негатив о школе / сравнение школ (все);
  B — реклама или промокод с упоминанием школы (по просмотрам);
  C — остальные посты про школы: топ по просмотрам на каждую школу.
    python make_comment_list.py --max 3000 --per-school 100
→ comment_urls.txt (по одной ссылке в строке, в порядке приоритета) и comment_list.csv (почему выбран).
"""
import argparse, csv, gzip, json
from pathlib import Path
HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--max", type=int, default=3000)
ap.add_argument("--per-school", type=int, default=100)
a = ap.parse_args()
posts = {}
for l in gzip.open(HERE / "label_input.jsonl.gz", "rt", encoding="utf-8"):
    p = json.loads(l); posts[p["post_id"]] = p
labels = {}
for l in (HERE / "out" / "labels.jsonl").read_text(encoding="utf-8").splitlines():
    try:
        r = json.loads(l)
        if not r.get("error"):
            labels[r["post_id"]] = r
    except Exception:
        pass

def views(pid):
    try: return float(posts[pid].get("views") or 0)
    except Exception: return 0.0

A, B, C = [], [], {}
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
    Cl += sorted(lst, key=lambda x: -views(x[0]))[: a.per_school]
Cl.sort(key=lambda x: -views(x[0]))
seen, out = set(), []
for pid, tier, why in A + B + Cl:
    if pid in seen: continue
    seen.add(pid); out.append((pid, tier, why))
out = out[: a.max]
(HERE / "comment_urls.txt").write_text("\n".join(posts[p]["url"] for p, _, _ in out) + "\n", encoding="utf-8")
with open(HERE / "comment_list.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["post_id", "приоритет", "почему", "просмотры", "автор", "ссылка", "о чём"])
    for p, t, why in out:
        w.writerow([p, t, why, posts[p].get("views"), posts[p].get("author"), posts[p].get("url"), labels[p].get("summary")])
print(f"A (конфликт/негатив/сравнение): {len(A)}, B (реклама): {len(B)}, C (топ по школам): {len(Cl)} → в списке {len(out)}")
print("comment_urls.txt, comment_list.csv")
