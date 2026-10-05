#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""НА СЕРВЕРЕ С GPU: links.csv -> yt-dlp (N параллельно) -> 5 кадров -> Whisper large-v3 (CUDA, float16, beam 5)
-> out/transcripts.jsonl (дописывается построчно, можно прерывать) + out/frames/<post_id>/*.jpg. Видео удаляются.

    python server_transcribe.py links.csv --workers 8
    python server_transcribe.py links.csv --cookies cookies.txt     # если TikTok отдаёт 403 без куки
"""
import argparse, csv, json, os, queue, shutil, sys, threading, time
from pathlib import Path

OUT = Path("out"); TMP = Path("tmp_dl")
HALLU = ("субтитры создавал", "субтитры сделал", "редактор субтитров", "продолжение следует", "dimatorzok",
         "подписывайтесь на канал", "спасибо за просмотр", "спасибо за внимание", "thanks for watching", "subtitles by")
PROMPT = ("Умскул, Umschool, Фоксфорд, Foxford, Сотка, Тетрика, Skysmart, Вебиум, Турбо ЕГЭ, Maximum Education, Точка Знаний, "
          "Лектариум, 99 баллов, ЕГЭленд, 100балльный репетитор, ЕГЭ, ОГЭ, вебинар, наставник, куратор, курс, пробник, баллы, "
          "профильная математика, русский язык, обществознание, биология, химия, информатика, физика, история, онлайн-школа, репетитор.")


def load_audio(path, sr=16000):
    import av, numpy as np
    chunks = []
    with av.open(str(path)) as c:
        if not c.streams.audio:
            return np.zeros(0, dtype=np.float32)
        st = c.streams.audio[0]; rs = av.AudioResampler(format="s16", layout="mono", rate=sr)
        for fr in c.decode(st):
            fr.pts = None
            for o in rs.resample(fr):
                chunks.append(o.to_ndarray().reshape(-1))
        for o in rs.resample(None):
            chunks.append(o.to_ndarray().reshape(-1))
    return (np.concatenate(chunks).astype(np.float32) / 32768.0) if chunks else np.zeros(0, dtype=np.float32)


def frames(video, out_dir, n=5):
    try:
        import av
        out_dir.mkdir(parents=True, exist_ok=True)
        with av.open(str(video)) as c:
            st = c.streams.video[0]
            dur = float(c.duration / av.time_base) if c.duration else 15.0
            for i in range(n):
                t = dur * (i + 0.5) / n
                c.seek(int(t / st.time_base), stream=st, backward=True, any_frame=False)
                img = next(c.decode(st)).to_image(); img.thumbnail((720, 720))
                img.save(out_dir / f"frame_{i:02d}_{int(t):03d}s.jpg", "JPEG", quality=85)
    except Exception as exc:
        print(f"  кадры {out_dir.name}: {exc}", flush=True)


IMPERSONATE = ""


def _ydl(url, folder, cookies, extractor_args=None):
    import yt_dlp
    opts = {"format": "best[ext=mp4]/best", "outtmpl": str(folder / "video.%(ext)s"), "quiet": True, "no_warnings": True,
            "noplaylist": True, "retries": 2, "http_headers": {"Referer": "https://www.tiktok.com/"}}
    if IMPERSONATE:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget.from_str(IMPERSONATE)
    if cookies:
        opts["cookiefile"] = cookies
    if extractor_args:
        opts["extractor_args"] = extractor_args
    with yt_dlp.YoutubeDL(opts) as ydl:
        return Path(ydl.prepare_filename(ydl.extract_info(url, download=True)))


_tikwm_lock = threading.Lock(); _tikwm_last = [0.0]


def _tikwm(url, folder):
    """Запасной путь: сторонний сервис tikwm.com отдаёт прямую ссылку на mp4 (сам ходит в TikTok). ~1 запрос/с."""
    import urllib.request, urllib.parse
    with _tikwm_lock:                       # не чаще 1 запроса в секунду на весь процесс
        wait = 1.1 - (time.time() - _tikwm_last[0])
        if wait > 0: time.sleep(wait)
        _tikwm_last[0] = time.time()
    req = urllib.request.Request("https://www.tikwm.com/api/?" + urllib.parse.urlencode({"url": url, "hd": 1}),
                                 headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
    if d.get("code") != 0 or not d.get("data"):
        raise RuntimeError(f"tikwm: {d.get('msg') or d}")
    play = d["data"].get("hdplay") or d["data"].get("play")
    if not play:
        raise RuntimeError("tikwm: нет ссылки на видео (возможно, карусель)")
    if play.startswith("/"):
        play = "https://www.tikwm.com" + play
    out = folder / "video.mp4"
    req = urllib.request.Request(play, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(out, "wb") as f:
        shutil.copyfileobj(r, f)
    if out.stat().st_size < 10_000:
        raise RuntimeError("tikwm: файл пустой")
    return out


DL_MODE = "auto"
API_ARGS = {"tiktok": {"api_hostname": ["api16-normal-c-useast1a.tiktokv.com"], "app_info": ["7355728856979392262"]}}


def download(url, folder, cookies):
    """Цепочка: yt-dlp (сайт) -> yt-dlp (мобильный API TikTok) -> tikwm. Режим --dl выбирает один путь."""
    folder.mkdir(parents=True, exist_ok=True)
    steps = {"web": lambda: _ydl(url, folder, cookies), "api": lambda: _ydl(url, folder, cookies, API_ARGS), "tikwm": lambda: _tikwm(url, folder)}
    order = ["web", "api", "tikwm"] if DL_MODE == "auto" else [DL_MODE]
    errs = []
    for name in order:
        try:
            return steps[name]()
        except Exception as exc:
            errs.append(f"{name}: {str(exc)[:120]}")
    raise RuntimeError(" | ".join(errs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("links"); ap.add_argument("--workers", type=int, default=8); ap.add_argument("--cookies", default="")
    ap.add_argument("--model", default="large-v3"); ap.add_argument("--frames", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0, help="проверка: взять только первые N ссылок")
    ap.add_argument("--impersonate", default="", help="маскировка TLS под браузер (chrome, safari); нужен pip install curl_cffi")
    ap.add_argument("--dl", default="auto", choices=["auto", "web", "api", "tikwm"], help="путь скачивания: auto = сайт -> мобильный API -> tikwm")
    ap.add_argument("--dl-test", default="", help="быстрая проверка: скачать одну ссылку и выйти (без Whisper)")
    ap.add_argument("--gpu-threads", type=int, default=2, help="потоков Whisper: пока один считает VAD на CPU, другой занимает GPU")
    ap.add_argument("--batch", type=int, default=16, help="батч Whisper (BatchedInferencePipeline); 1 = обычный режим")
    ap.add_argument("--sleep", type=float, default=0.0, help="пауза между скачиваниями в каждом потоке, сек (если TikTok начнёт отказывать)")
    a = ap.parse_args()
    global IMPERSONATE, DL_MODE; IMPERSONATE = a.impersonate; DL_MODE = a.dl
    if a.dl_test:
        for name in (["web", "api", "tikwm"] if a.dl == "auto" else [a.dl]):
            t0 = time.time(); DL_MODE = name
            try:
                v = download(a.dl_test, TMP / "dl_test", a.cookies)
                print(f"{name:6} OK  {v.stat().st_size // 1024} КБ за {time.time() - t0:.1f} с"); v.unlink()
            except Exception as exc:
                print(f"{name:6} ОШИБКА {str(exc)[:200]}")
        return 0
    OUT.mkdir(exist_ok=True); (OUT / "frames").mkdir(exist_ok=True)
    done = set()
    jl = OUT / "transcripts.jsonl"
    if jl.exists():
        for line in jl.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if not str(r.get("error", "")).startswith("download:"):     # не скачавшиеся — пробуем снова
                    done.add(r["post_id"])
            except Exception: pass
    rows = [r for r in csv.DictReader(open(a.links, encoding="utf-8")) if r["post_id"] not in done]
    if a.limit:
        rows = rows[: a.limit]
    print(f"К обработке {len(rows)} (уже готово {len(done)})", flush=True)
    from faster_whisper import WhisperModel
    model = WhisperModel(a.model, device="cuda", compute_type="float16")
    batched = None
    if a.batch > 1:
        try:
            from faster_whisper import BatchedInferencePipeline
            batched = BatchedInferencePipeline(model=model)
            print(f"Whisper: батчевый режим, batch={a.batch} (та же модель large-v3, beam 5, в 2–4 раза быстрее)", flush=True)
        except Exception as exc:
            print("Whisper: батчевый режим недоступен, обычный:", exc, flush=True)
    def run_whisper(audio):
        kw = dict(language="ru", vad_filter=True, beam_size=5, initial_prompt=PROMPT, condition_on_previous_text=False)
        if batched is not None:
            try:
                return batched.transcribe(audio, batch_size=a.batch, **kw)
            except TypeError:
                return batched.transcribe(audio, batch_size=a.batch, language="ru", beam_size=5, initial_prompt=PROMPT)
        return model.transcribe(audio, **kw)
    q = queue.Queue(maxsize=a.workers * 2); stop = object()

    def worker(items):
        """Скачивание + кадры + декодирование звука — всё на CPU в потоках загрузки, GPU-потокам остаётся только Whisper."""
        for r in items:
            pid = r["post_id"]
            try:
                v = download(r["url"], TMP / pid, a.cookies)
            except Exception as exc:
                q.put((pid, None, None, "download: " + str(exc)[:400])); continue
            try:
                if a.frames: frames(v, OUT / "frames" / pid, a.frames)
                audio = load_audio(v)
                q.put((pid, v, audio, None))
            except Exception as exc:
                q.put((pid, v, None, f"{type(exc).__name__}: {str(exc)[:200]}"))
            if a.sleep:
                time.sleep(a.sleep)

    chunks = [rows[i::a.workers] for i in range(a.workers)]
    ths = [threading.Thread(target=worker, args=(c,), daemon=True) for c in chunks if c]
    for t in ths: t.start()
    threading.Thread(target=lambda: ([t.join() for t in ths], [q.put(stop) for _ in range(a.gpu_threads)]), daemon=True).start()

    fout = jl.open("a", encoding="utf-8"); state = {"n": 0}; t0 = time.monotonic(); lock = threading.Lock()

    def consumer():
        while True:
            item = q.get()
            if item is stop: break
            pid, v, audio, err = item
            rec = {"post_id": pid, "model": a.model}
            if err:
                rec.update({"text": "", "error": err})
            elif audio.size == 0:
                rec.update({"text": "", "segments": [], "note": "нет аудио"})
            else:
                try:
                    segs, info = run_whisper(audio)
                    out, parts = [], []
                    for sg in segs:
                        t = sg.text.strip(); low = t.lower()
                        if not t or any(h in low for h in HALLU) or sg.no_speech_prob > 0.75 or sg.avg_logprob < -1.2 or sg.compression_ratio > 2.4:
                            continue
                        out.append({"start": round(sg.start, 2), "end": round(sg.end, 2), "text": t}); parts.append(t)
                    rec.update({"text": " ".join(parts)[:32000], "segments": out, "duration": round(info.duration, 1), "language": info.language})
                except Exception as exc:
                    rec.update({"text": "", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            if v is not None:
                shutil.rmtree(v.parent, ignore_errors=True)
            with lock:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n"); fout.flush(); state["n"] += 1; n = state["n"]
            if n % 10 == 0:
                print(f"[{n}/{len(rows)}] {n / ((time.monotonic() - t0) / 60):.1f}/мин | {rec.get('text', '')[:70]}", flush=True)

    cons = [threading.Thread(target=consumer, daemon=True) for _ in range(a.gpu_threads)]
    for t in cons: t.start()
    for t in cons: t.join()
    n = state["n"]
    fails = sum(1 for line in jl.read_text(encoding="utf-8").splitlines() if '"error": "download:' in line)
    print(f"Готово: {n} -> out/transcripts.jsonl и out/frames/ | не скачалось всего: {fails}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
