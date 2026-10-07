#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА СЕРВЕРЕ: проверка, что по постам с Маков всё собралось корректно (только чтение, ничего не меняет).

    cd /teamproject/server && python check_mac.py            # сводка
    python check_mac.py --examples                           # + примеры расшифровок, комментариев и текста с экрана

По каждому файлу с Мака: посты, видео, расшифровки (речь / без речи / не скачалось / ещё нет),
комментарии (сколько постов собрано, сколько комментариев против числа в TikTok, явные недоборы),
текст с экрана для роликов без речи.
"""
from __future__ import annotations

import argparse, csv, json, random, sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEW = HERE / "new"
csv.field_size_limit(10 ** 9)
LABEL = {"new_posts_mac.csv": "Диана", "new_posts_mac_masha.csv": "Маша", "new_posts_mac_zhenya.csv": "Женя",
         "new_posts_mac_100b.csv": "100балльный"}


def jl(path):
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            pid = str(r.get("post_id"))
            if not r.get("error") or pid not in out:      # успешная запись важнее ошибки
                out[pid] = r
    return out


def num(x):
    try:
        return int(float(x or 0))
    except ValueError:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", action="store_true")
    a = ap.parse_args()
    sys.path.insert(0, str(HERE))
    from server_screen_ocr import no_speech

    mp = NEW / "mac_posts.csv"
    if not mp.exists():
        sys.exit("Нет new/mac_posts.csv — прогон --mac-only ещё не запускался")
    posts = {r["post_id"]: r for r in csv.DictReader(open(mp, encoding="utf-8"))}
    src = {}
    for f in sorted(HERE.glob("new_posts_mac*.csv")):                  # тот же порядок, что в new_schools.py
        for r in csv.DictReader(open(f, encoding="utf-8")):
            pid = (r.get("post_id") or "").strip()
            if pid in posts and pid not in src:
                src[pid] = LABEL.get(f.name, f.name)
    tr = jl(NEW / "out" / "transcripts.jsonl")
    scr = jl(NEW / "out" / "screen_text.jsonl")
    com = jl(NEW / "comments_out" / "comments_done.jsonl")
    cdir = NEW / "comments_out" / "comments"

    S = defaultdict(Counter)
    under = []
    for pid, r in posts.items():
        s = S[src.get(pid, "?")]
        s["постов"] += 1
        if r.get("post_type") == "video":
            s["видео"] += 1
            t = tr.get(pid)
            if not t:
                s["расшифровки ещё нет"] += 1
            elif t.get("error"):
                s["видео не скачалось"] += 1
            elif no_speech(t.get("text"), 25):
                s["без речи"] += 1
                sc = scr.get(pid)
                if sc and not sc.get("error"):
                    s["  экран прочитан"] += 1
                    s["  на экране есть текст"] += bool((sc.get("text") or "").strip())
            else:
                s["с речью"] += 1
        else:
            s["карусели"] += 1
        want = num(r.get("comment_count"))
        if want > 0:
            s["постов с комментариями в TikTok"] += 1
            s["комментариев в TikTok (по счётчику)"] += want
            c = com.get(pid)
            got = (c or {}).get("n")
            if got is None and (cdir / f"{pid}.jsonl").exists():
                got = sum(1 for _ in open(cdir / f"{pid}.jsonl", encoding="utf-8"))
            if got is None:
                s["комментарии ещё не собраны"] += 1
            else:
                s["постов с собранными комментариями"] += 1
                s["собрано комментариев и ответов"] += got
                if want >= 20 and got < want * 0.5:
                    s["недобор (<50% от счётчика, при ≥20)"] += 1
                    under.append((want, got, pid, src.get(pid, "?")))

    keys = ["постов", "видео", "карусели", "с речью", "без речи", "  экран прочитан", "  на экране есть текст",
            "видео не скачалось", "расшифровки ещё нет", "постов с комментариями в TikTok", "постов с собранными комментариями",
            "комментарии ещё не собраны", "комментариев в TikTok (по счётчику)", "собрано комментариев и ответов",
            "недобор (<50% от счётчика, при ≥20)"]
    cols = [c for c in ("Диана", "Маша", "Женя", "100балльный", "?") if c in S]
    tot = Counter()
    for c in cols:
        tot.update(S[c])
    w = 38
    print("\n" + "".ljust(w) + "".join(c.rjust(12) for c in cols) + "ВСЕГО".rjust(12))
    for k in keys:
        print(k.ljust(w) + "".join(f"{S[c][k]:>12,}".replace(",", " ") for c in cols) + f"{tot[k]:>12,}".replace(",", " "))
    if tot["комментариев в TikTok (по счётчику)"]:
        print(f"\nСобрано комментариев от счётчика TikTok: {tot['собрано комментариев и ответов'] / tot['комментариев в TikTok (по счётчику)']:.0%} "
              f"(норма 70–100%: TikTok считает и удалённые/скрытые)")

    print("\nЧто проверить:")
    ok = True
    if tot["расшифровки ещё нет"] or tot["комментарии ещё не собраны"]:
        ok = False
        print(f"  • ещё не обработано: расшифровки {tot['расшифровки ещё нет']}, комментарии {tot['комментарии ещё не собраны']} "
              f"— если прогон уже закончился, запустите bash run_new_schools.sh --mac-only ещё раз (продолжит с места)")
    if tot["видео"] and tot["видео не скачалось"] / tot["видео"] > 0.1:
        ok = False
        print(f"  • не скачалось {tot['видео не скачалось'] / tot['видео']:.0%} видео — много (обычно удалённые/закрытые ролики, норма до 5–10%)")
    if under:
        print(f"  • недобор комментариев у {len(under)} постов (самые крупные):")
        for want, got, pid, s in sorted(under, reverse=True)[:8]:
            print(f"      {pid} ({s}): в TikTok {want}, собрано {got}")
        if len(under) > 0.05 * max(1, tot["постов с собранными комментариями"]):
            ok = False
    if ok:
        print("  • всё в порядке")

    if a.examples:
        rnd = random.Random()
        print("\n--- 3 случайные расшифровки с речью:")
        sp = [p for p in posts if p in tr and not tr[p].get("error") and not no_speech(tr[p].get("text"), 25)]
        for p in rnd.sample(sp, min(3, len(sp))):
            print(f"  {p}: {tr[p]['text'][:160]}")
        print("--- 3 случайных текста с экрана:")
        st = [p for p in posts if (scr.get(p) or {}).get("text")]
        for p in rnd.sample(st, min(3, len(st))):
            print(f"  {p}: {scr[p]['text'][:160].replace(chr(10), ' / ')}")
        print("--- 3 случайных комментария:")
        files = [cdir / f"{p}.jsonl" for p in posts if (cdir / f"{p}.jsonl").exists()]
        for f in rnd.sample(files, min(3, len(files))):
            lines = f.read_text(encoding="utf-8").splitlines()
            c = json.loads(rnd.choice(lines)) if lines else {}
            print(f"  {f.stem}: {str(c.get('text') or c.get('Текст') or '')[:160]}")


if __name__ == "__main__":
    main()
