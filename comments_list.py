#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА MAC/WINDOWS: комментарии и реплаи по готовому списку ссылок (server/comment_urls.txt), параллельно по аккаунтам.
Пропускает посты, по которым комментарии уже собраны (в любой школе или в этом списке). Повторный запуск продолжает.
Результат: cases/comments_priority/raw/posts/<id>/ и таблица КОММЕНТАРИИ_приоритет.xlsx.
    python comments_list.py server/comment_urls.txt --count 200
"""
import argparse, glob, os, re, shutil, subprocess, sys, time
from pathlib import Path
os.chdir(Path(__file__).resolve().parent)

ap = argparse.ArgumentParser()
ap.add_argument("urls_file", nargs="?", default="server/comment_urls.txt")
ap.add_argument("--count", type=int, default=200)
ap.add_argument("--parallel", type=int, default=0, help="0 = по числу аккаунтов")
a = ap.parse_args()
CASE = Path("cases") / "comments_priority"; CASE.mkdir(parents=True, exist_ok=True)

urls = [u.strip() for u in Path(a.urls_file).read_text(encoding="utf-8").splitlines() if u.strip()]
have = {Path(p).parent.name for p in glob.glob("cases/*/raw/posts/*/comments_*.jsonl")}
pid = lambda u: (re.search(r"/(?:video|photo)/(\d+)", u) or [None, ""])[1]
todo = [u for u in urls if pid(u) not in have]
print(f"В списке {len(urls)}, комментарии уже есть у {len(urls) - len(todo)}, собрать {len(todo)}", flush=True)

from pipeline import show_accounts, keep_awake
show_accounts(reset_locks=True)
acc = list(getattr(show_accounts, "names", []))
if not acc:
    sys.exit("Нет активных аккаунтов TikTok в пуле — 2_login.command")
n = min(a.parallel or len(acc), len(acc))
keep_awake()
procs = []
for k in range(n):
    part = todo[k::n]
    if not part: continue
    f = CASE / f"urls_{acc[k]}.txt"; f.write_text("\n".join(part) + "\n", encoding="utf-8")
    log = open(CASE / f"log_{acc[k]}.txt", "a", encoding="utf-8")
    cmd = [sys.executable, "-u", "collect_tiktok_threads.py", "--urls-file", str(f), "--count", str(a.count),
           "--case-dir", str(CASE), "--account", acc[k], "--skip-collected"]
    print(f"поток {k + 1}: @{acc[k]} — {len(part)} постов (лог {log.name})", flush=True)
    procs.append(subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT))
t0 = time.time()
while any(p.poll() is None for p in procs):
    time.sleep(120)
    done = len(glob.glob(str(CASE / "raw" / "posts" / "*" / "thread_*_summary.json")))
    print(f"[{time.strftime('%H:%M')}] готово постов: {done} из {len(todo)} | {done / max(1, (time.time() - t0) / 3600):.0f}/час", flush=True)
print("Потоки завершены, собираю таблицу...", flush=True)
subprocess.run([sys.executable, "export_comments_table.py", "--all", str(CASE)])
for x in sorted((CASE / "search").glob("comments_all*.xlsx")):
    shutil.copy(x, "КОММЕНТАРИИ_приоритет.xlsx"); print("→ КОММЕНТАРИИ_приоритет.xlsx")
