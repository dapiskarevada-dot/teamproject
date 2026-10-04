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


def download(url, folder, cookies):
    import yt_dlp
    folder.mkdir(parents=True, exist_ok=True)
    opts = {"format": "best[ext=mp4]/best", "outtmpl": str(folder / "video.%(ext)s"), "quiet": True, "no_warnings": True,
            "noplaylist": True, "retries": 3, "http_headers": {"Referer": "https://www.tiktok.com/"}}
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        return Path(ydl.prepare_filename(ydl.extract_info(url, download=True)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("links"); ap.add_argument("--workers", type=int, default=8); ap.add_argument("--cookies", default="")
    ap.add_argument("--model", default="large-v3"); ap.add_argument("--frames", type=int, default=5)
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True); (OUT / "frames").mkdir(exist_ok=True)
    done = set()
    jl = OUT / "transcripts.jsonl"
    if jl.exists():
        for line in jl.read_text(encoding="utf-8").splitlines():
            try: done.add(json.loads(line)["post_id"])
            except Exception: pass
    rows = [r for r in csv.DictReader(open(a.links, encoding="utf-8")) if r["post_id"] not in done]
    print(f"К обработке {len(rows)} (уже готово {len(done)})", flush=True)
    from faster_whisper import WhisperModel
    model = WhisperModel(a.model, device="cuda", compute_type="float16")
    q = queue.Queue(maxsize=a.workers * 2); stop = object()

    def worker(items):
        for r in items:
            try:
                v = download(r["url"], TMP / r["post_id"], a.cookies)
                q.put((r["post_id"], v))
            except Exception as exc:
                q.put((r["post_id"], None, str(exc)[:200]))

    chunks = [rows[i::a.workers] for i in range(a.workers)]
    ths = [threading.Thread(target=worker, args=(c,), daemon=True) for c in chunks if c]
    for t in ths: t.start()
    threading.Thread(target=lambda: ([t.join() for t in ths], q.put(stop)), daemon=True).start()

    fout = jl.open("a", encoding="utf-8"); n = 0; t0 = time.monotonic()
    while True:
        item = q.get()
        if item is stop: break
        pid, v = item[0], item[1]
        rec = {"post_id": pid, "model": a.model}
        if v is None:
            rec.update({"text": "", "error": "download: " + item[2]})
        else:
            try:
                if a.frames: frames(v, OUT / "frames" / pid, a.frames)
                audio = load_audio(v)
                if audio.size == 0:
                    rec.update({"text": "", "segments": [], "note": "нет аудио"})
                else:
                    segs, info = model.transcribe(audio, language="ru", vad_filter=True, beam_size=5, initial_prompt=PROMPT, condition_on_previous_text=False)
                    out, parts = [], []
                    for s in segs:
                        t = s.text.strip(); low = t.lower()
                        if not t or any(h in low for h in HALLU) or s.no_speech_prob > 0.75 or s.avg_logprob < -1.2 or s.compression_ratio > 2.4:
                            continue
                        out.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": t}); parts.append(t)
                    rec.update({"text": " ".join(parts)[:32000], "segments": out, "duration": round(info.duration, 1), "language": info.language})
            except Exception as exc:
                rec.update({"text": "", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            finally:
                shutil.rmtree(v.parent, ignore_errors=True)
        fout.write(json.dumps(rec, ensure_ascii=False) + "\n"); fout.flush(); n += 1
        if n % 10 == 0:
            print(f"[{n}/{len(rows)}] {n / ((time.monotonic() - t0) / 60):.1f}/мин | {rec.get('text', '')[:70]}", flush=True)
    print("Готово:", n, "-> out/transcripts.jsonl и out/frames/", flush=True)


if __name__ == "__main__":
    sys.exit(main())
