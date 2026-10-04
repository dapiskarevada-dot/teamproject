#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""НА MAC: список роликов для сервера -> server/links.csv (post_id,url). Берутся все видео-посты с упоминанием школы
(по всем школам из плана), у которых ещё нет транскрипта. С --sample — только выборки школ."""
import argparse, csv, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os; os.chdir(Path(__file__).resolve().parent.parent)
from transcribe_links import load_targets, has_transcript

ap = argparse.ArgumentParser(); ap.add_argument("--sample", action="store_true"); a = ap.parse_args()
targets = load_targets(not a.sample, None)
todo = [(i, u) for i, u in targets if not has_transcript(i)]
out = Path("server") / "links.csv"
with out.open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["post_id", "url"]); w.writerows(todo)
print(f"Всего видео {len(targets)}, без транскрипта {len(todo)} -> {out}")
