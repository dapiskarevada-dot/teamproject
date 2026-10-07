#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
НА СЕРВЕРЕ: текст с кадров для роликов без речи (музыка + надписи на экране).
Берёт out/transcripts.jsonl, выбирает ролики с пустым транскриптом, у которых есть кадры в out/frames/<id>/,
отправляет кадры в Gemini 2.5 Flash (OpenRouter) и пишет out/screen_text.jsonl. Можно запускать
параллельно с Whisper — это сетевые запросы, видеокарту не трогает. Повторный запуск продолжает с места.

Ключ: файл openrouter_key.txt в /teamproject/ (загрузить через Jupyter) или переменная OPENROUTER_API_KEY.

    python server_screen_ocr.py                 # ролики без речи + карусели
    python server_screen_ocr.py --all           # все ролики с кадрами
    python server_screen_ocr.py --limit 20      # проба

Для постов с Маков (new_schools.py --mac-only), только ролики без речи, без каруселей:
    python server_screen_ocr.py --out new/out --ids new/mac_posts.csv --carousels "" --dry-run   # сколько и почём
    python server_screen_ocr.py --out new/out --ids new/mac_posts.csv --carousels ""
«Без речи» = Whisper ничего не услышал, или распознал меньше --short символов, или выдал типичную
галлюцинацию на музыке («Продолжение следует…», «Субтитры сделал…»).
"""
from __future__ import annotations

import argparse, csv, json, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
OUT = HERE / "out"
# что Whisper large-v3 «слышит» в музыке без слов
HALLUC = re.compile(r"продолжение следует|субтитр|редактор субтитров|корректор|dimatorzok|спасибо за просмотр|подписывайтесь на канал|"
                    r"thanks for watching|thank you for watching|amara\.org|музыка|music|♪|апплодисменты|аплодисменты|смех", re.I)


def no_speech(text, short):
    t = (text or "").strip()
    if len(t) < short:
        return True
    rest = HALLUC.sub("", t)
    return len(re.sub(r"[\W\d_]+", "", rest)) < short


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="все ролики с кадрами, а не только без речи")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--carousels", default=str(HERE / "links_carousels.csv"), help="csv post_id,url каруселей; '' = не делать")
    ap.add_argument("--out", default=str(HERE / "out"), help="папка с transcripts.jsonl и frames/ (для Маков: new/out)")
    ap.add_argument("--ids", default="", help="только эти посты: csv с колонкой post_id (например new/mac_posts.csv)")
    ap.add_argument("--short", type=int, default=25, help="речь короче N букв считается «без речи»")
    ap.add_argument("--dry-run", action="store_true", help="только посчитать ролики и кадры, без запросов к Gemini")
    a = ap.parse_args()
    global OUT
    OUT = Path(a.out) if Path(a.out).is_absolute() else (HERE / a.out)
    only = None
    if a.ids:
        csv.field_size_limit(10 ** 9)
        f = Path(a.ids) if Path(a.ids).is_absolute() else HERE / a.ids
        only = {(r.get("post_id") or "").strip() for r in csv.DictReader(open(f, encoding="utf-8-sig"))}
    from ocr_vlm import ask_model, parse_json, load_key, load_schools, DEFAULT_MODEL, DEFAULT_BASE
    from video_frames_ocr import FRAME_PROMPT, dedupe_frames
    key = load_key()
    if not key and not a.dry_run:
        sys.exit("Нет ключа: положите openrouter_key.txt в /teamproject/ (через Jupyter) и запустите снова")
    prompt = FRAME_PROMPT.replace("{names}", ", ".join(load_schools()))

    last = {}
    for line in (OUT / "transcripts.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        pid = str(r.get("post_id"))
        if not r.get("error") or pid not in last:      # успешная запись важнее ошибки
            last[pid] = r
    targets = [pid for pid, r in last.items() if not r.get("error") and (only is None or pid in only)
               and (a.all or no_speech(r.get("text"), a.short))]
    res_path = OUT / "screen_text.jsonl"
    done = set()
    if res_path.exists():
        for line in res_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if not r.get("error"):
                    done.add(r["post_id"])
            except Exception:
                pass
    todo = [(p, "video") for p in targets if p not in done and (OUT / "frames" / p).is_dir()]
    car = {}
    if a.carousels and Path(a.carousels).exists():
        car = {r["post_id"]: r["url"] for r in csv.DictReader(open(a.carousels, encoding="utf-8")) if r["post_id"] not in done}
        todo = [(p, "carousel") for p in car] + todo
    if a.limit:
        todo = todo[: a.limit]
    if a.dry_run:
        nf = sum(len(dedupe_frames(sorted((OUT / "frames" / p).glob("*.jpg")))) for p, k in todo if k == "video")
        nospeech = sum(1 for p in targets)
        print(f"Роликов без речи: {nospeech}, из них с кадрами и ещё не сделано: {sum(1 for _, k in todo if k == 'video')} | "
              f"кадров после удаления похожих: {nf} | примерно ${nf * 0.0012:.0f}–{nf * 0.0025:.0f} на Gemini", flush=True)
        return
    print(f"Роликов без речи с кадрами: {sum(1 for _, k in todo if k == 'video')}, каруселей: {sum(1 for _, k in todo if k == 'carousel')}, "
          f"уже готово {len(done)} (модель {DEFAULT_MODEL})", flush=True)

    tik_lock = threading.Lock(); tik_last = [0.0]
    STOP = threading.Event()

    def slides(pid):
        """Слайды карусели через tikwm (не чаще 1 запроса в секунду)."""
        import urllib.request, urllib.parse
        with tik_lock:
            w = 1.1 - (time.time() - tik_last[0])
            if w > 0: time.sleep(w)
            tik_last[0] = time.time()
        req = urllib.request.Request("https://www.tikwm.com/api/?" + urllib.parse.urlencode({"url": car[pid]}), headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
        imgs = (d.get("data") or {}).get("images") or []
        folder = OUT / "slides" / pid; folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, u in enumerate(imgs[:20]):
            f = folder / f"slide_{i:02d}.jpg"
            if not f.exists():
                with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as r:
                    f.write_bytes(r.read())
            paths.append(f)
        return paths

    def one(item):
        pid, kind = item
        if kind == "carousel":
            try:
                frames = slides(pid)
            except Exception as exc:
                return {"post_id": pid, "kind": kind, "text": "", "error": f"slides: {str(exc)[:150]}"}
            if not frames:
                return {"post_id": pid, "kind": kind, "text": "", "error": "slides: нет картинок"}
        else:
            frames = dedupe_frames(sorted((OUT / "frames" / pid).glob("*.jpg")))
        texts, sch, promos, errs = [], [], [], 0
        for f in frames:
            if STOP.is_set():
                return None
            try:
                d = parse_json(ask_model(key, DEFAULT_BASE, DEFAULT_MODEL, f, prompt=prompt))
            except Exception as exc:
                if "402" in str(exc):
                    STOP.set(); return None
                errs += 1
                continue
            t = (d.get("text") or "").strip()
            if t and t not in texts:
                texts.append(t)
            for x in d.get("schools") or []:
                if x and x not in sch:
                    sch.append(x)
            p = (d.get("promo") or "").strip()
            if p and p not in promos:
                promos.append(p)
        rec = {"post_id": pid, "kind": kind, "model": DEFAULT_MODEL, "frames": len(frames), "text": "\n".join(texts)[:16000],
               "schools": sch, "promo": "; ".join(promos)}
        if frames and errs == len(frames):
            rec["error"] = "api"
        return rec

    lock = threading.Lock(); n = 0; t0 = time.monotonic()
    with open(res_path, "a", encoding="utf-8") as fout, ThreadPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(one, p) for p in todo]):
            rec = fut.result()
            if rec is None:
                continue
            with lock:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n"); fout.flush(); n += 1
                if n % 20 == 0 or n == len(todo):
                    rate = n / max(1e-6, (time.monotonic() - t0) / 60)
                    print(f"[{n}/{len(todo)}] {rate:.0f}/мин | {rec.get('kind')} | {rec['text'][:70].replace(chr(10), ' / ')}", flush=True)
    if STOP.is_set():
        print("!!! OpenRouter: 402 — закончились деньги. Пополните баланс и запустите ту же команду: продолжит с места.", flush=True)
    print(f"Готово: {n} -> {res_path}", flush=True)


if __name__ == "__main__":
    main()
