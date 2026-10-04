#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Расшифровка речи в видео TikTok через faster-whisper (на основе scrapping2.py Маши).

Работает по папкам cases/tiktok_media/raw/posts/<post_id>/video.mp4 (их кладёт конвейер
collect_tiktok_search_threads.py или скачивает этот скрипт сам по ссылкам).
Результат кэшируется в <post_id>/transcript.whisper.json и повторно не считается.

    python transcribe_whisper.py                      # все скачанные видео без транскрипта
    python transcribe_whisper.py --model small        # быстрее, хуже русский
    python transcribe_whisper.py --urls-file urls.txt # сначала скачать через yt-dlp, потом расшифровать

Модели: large-v3-turbo (по умолчанию; точность ~large-v3, в разы быстрее), large-v3, medium, small.
Устройство: cpu (int8). На Mac с Apple Silicon это ~0.3–0.5 от длительности ролика.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_LANGUAGE = "ru"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE = "int8"
DEFAULT_ROOT = Path("cases") / "tiktok_media"

# Подсказка Whisper: слова и названия, которые часто звучат в роликах, — модель пишет их правильно.
# Школы подтягиваются из schools.txt (ocr_vlm.load_schools), остальное — общий словарь.
BASE_PROMPT_WORDS = (
    "ЕГЭ, ОГЭ, вебинар, наставник, курс, подготовка к экзамену, пробник, баллы, "
    "химия, математика, профильная математика, русский язык, обществознание, биология, "
    "информатика, физика, литература, история, онлайн-школа, репетитор."
)

_model = None

# Фразы, которые Whisper «слышит» на тишине и музыке (из титров обучающих данных).
HALLUCINATIONS = ("субтитры создавал", "субтитры сделал", "редактор субтитров", "продолжение следует", "dimatorzok",
                  "подписывайтесь на канал", "спасибо за просмотр", "спасибо за внимание", "субтитры подготовил",
                  "thanks for watching", "subtitles by", "amara.org")


def is_hallucination(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    if any(h in t for h in HALLUCINATIONS):
        return True
    words = t.split()
    return len(words) >= 6 and len(set(words)) <= 2      # «да да да да да да»


def build_prompt(schools=None):
    try:
        if schools is None:
            from ocr_vlm import load_schools
            schools = load_schools()
    except Exception:
        schools = []
    names = ", ".join(s.split(",")[0].strip() for s in schools)
    return (names + ", " if names else "") + BASE_PROMPT_WORDS


_engine = None


def pick_engine():
    """На Apple Silicon используем mlx-whisper (GPU, в ~5–10 раз быстрее), иначе faster-whisper (CPU)."""
    global _engine
    if _engine is None:
        _engine = "faster"
        if sys.platform == "darwin" and os.uname().machine == "arm64" and os.environ.get("WHISPER_ENGINE", "auto") != "faster":
            try:
                import mlx_whisper  # noqa: F401
                _engine = "mlx"
            except Exception:
                _engine = "faster"
        print(f"Whisper engine: {_engine}", flush=True)
    return _engine


MLX_REPOS = {"large-v3": "mlx-community/whisper-large-v3-mlx", "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
             "medium": "mlx-community/whisper-medium-mlx", "small": "mlx-community/whisper-small-mlx"}


class _Seg:
    def __init__(self, d):
        self.start, self.end, self.text = d.get("start", 0.0), d.get("end", 0.0), d.get("text", "")
        self.no_speech_prob = d.get("no_speech_prob", 0.0); self.avg_logprob = d.get("avg_logprob", 0.0)
        self.compression_ratio = d.get("compression_ratio", 0.0)


class _Info:
    def __init__(self, language, duration):
        self.language, self.duration = language, duration


def mlx_transcribe(audio, model_name, language, prompt):
    import mlx_whisper
    repo = MLX_REPOS.get(model_name, model_name)
    r = mlx_whisper.transcribe(audio, path_or_hf_repo=repo, language=language, initial_prompt=prompt,
                               condition_on_previous_text=False, fp16=True, verbose=False)
    segs = [_Seg(d) for d in r.get("segments", [])]
    return segs, _Info(r.get("language", language), float(len(audio)) / 16000.0)


def get_model(name=DEFAULT_MODEL, device=DEFAULT_DEVICE, compute=DEFAULT_COMPUTE):
    global _model
    if _model is None or _model[0] != (name, device, compute):
        from faster_whisper import WhisperModel
        print(f"Загружаю модель Whisper '{name}' ({device}/{compute}); первый раз скачивается из интернета...", flush=True)
        _model = ((name, device, compute), WhisperModel(name, device=device, compute_type=compute))
    return _model[1]


def load_audio(path, sr=16000):
    """Звук из видео: моно 16 кГц float32 — то, что ждёт Whisper (своя декодировка через PyAV)."""
    import av
    import numpy as np
    chunks = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            return np.zeros(0, dtype=np.float32)
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sr)
        for frame in container.decode(stream):
            frame.pts = None
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def transcribe_file(path, model_name=DEFAULT_MODEL, language=DEFAULT_LANGUAGE, prompt=None,
                    device=DEFAULT_DEVICE, compute=DEFAULT_COMPUTE, verbose=False):
    """Возвращает dict: text, segments[{start,end,text}], language, duration, seconds."""
    t0 = time.monotonic()
    audio = load_audio(path)
    if audio.size == 0:
        return {"text": "", "segments": [], "language": None, "duration": 0.0, "seconds": round(time.monotonic() - t0, 1), "note": "нет аудиодорожки"}
    engine = pick_engine()
    if engine == "mlx":
        segments, info = mlx_transcribe(audio, model_name, language, prompt or build_prompt())
    else:
        model = get_model(model_name, device, compute)
        segments, info = model.transcribe(
            audio, language=language, vad_filter=True, beam_size=5,
            initial_prompt=prompt or build_prompt(), condition_on_previous_text=False,
        )
    segs, parts, dropped = [], [], 0
    for s in segments:
        t = s.text.strip()
        # Фильтр галлюцинаций: типовые фразы на тишине/музыке и сегменты с низкой уверенностью.
        bad = (is_hallucination(t) or (getattr(s, "no_speech_prob", 0) or 0) > 0.75
               or (getattr(s, "avg_logprob", 0) or 0) < -1.2 or (getattr(s, "compression_ratio", 0) or 0) > 2.4)
        if verbose:
            print(f"    [{s.start:5.1f}–{s.end:5.1f} с]{' (отброшено)' if bad else ''} {t}", flush=True)
        if bad:
            dropped += 1; continue
        segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": t})
        parts.append(t)
    speech = round(sum(x["end"] - x["start"] for x in segs), 1)
    return {"text": " ".join(parts).strip()[:32000], "segments": segs, "language": getattr(info, "language", language),
            "duration": round(getattr(info, "duration", 0.0) or 0.0, 1), "seconds": round(time.monotonic() - t0, 1),
            "speech_seconds": speech, "dropped_segments": dropped}


def download_ytdlp(url, folder: Path):
    """Запасное скачивание видео через yt-dlp. Возвращает путь или None."""
    try:
        import yt_dlp
        folder.mkdir(parents=True, exist_ok=True)
        opts = {"format": "best", "outtmpl": str(folder / "video.%(ext)s"), "quiet": True, "no_warnings": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))
    except Exception as exc:
        print("  yt-dlp не смог скачать:", str(exc)[:200])
        return None


def find_video(post_dir: Path):
    for f in sorted(post_dir.glob("video.*")):
        if f.suffix.lower() in {".mp4", ".webm", ".mov", ".m4a", ".mp3"}:
            return f
    return None


def transcribe_dir(root: Path = DEFAULT_ROOT, post_ids=None, model_name=DEFAULT_MODEL, language=DEFAULT_LANGUAGE,
                   force=False, verbose=False, device=DEFAULT_DEVICE, compute=DEFAULT_COMPUTE):
    """
    Расшифровывает video.* в root/raw/posts/<id>/, кэш в transcript.whisper.json.
    Возвращает {post_id: {"text", "duration", "seconds", "model", "source", "error"}}.
    """
    posts_dir = root / "raw" / "posts"
    prompt = build_prompt()
    out = {}
    todo = []
    for d in sorted(posts_dir.glob("*")):
        if post_ids and d.name not in post_ids:
            continue
        v = find_video(d)
        cache = d / "transcript.whisper.json"
        if cache.exists() and cache.stat().st_size > 2 and not force:
            c = json.loads(cache.read_text(encoding="utf-8"))
            out[d.name] = {"text": c.get("text", ""), "duration": c.get("duration"), "seconds": c.get("seconds"),
                           "model": c.get("model"), "source": "whisper" if c.get("text") else "whisper: речи нет", "error": ""}
            continue
        if v:
            todo.append((d, v))
    print(f"Whisper: из кэша {len(out)}, к расшифровке {len(todo)}", flush=True)
    for i, (d, v) in enumerate(todo, 1):
        print(f"  [{i}/{len(todo)}] {d.name} ({v.name}, {v.stat().st_size // 1024} KB)", flush=True)
        try:
            r = transcribe_file(v, model_name, language, prompt, device, compute, verbose)
            r["model"] = model_name
            (d / "transcript.whisper.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            out[d.name] = {"text": r["text"], "duration": r["duration"], "seconds": r["seconds"], "model": model_name,
                           "source": "whisper" if r["text"] else "whisper: речи нет", "error": ""}
            print(f"      {r['seconds']}s | {r['text'][:90]}", flush=True)
        except Exception as exc:
            out[d.name] = {"text": "", "duration": None, "seconds": None, "model": model_name, "source": "", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            print("      ОШИБКА:", out[d.name]["error"], flush=True)
    return out


def write_table(result: dict, root: Path):
    rows = [{"ID поста": pid, "Длительность (с)": r.get("duration"), "Транскрипт (Whisper)": r.get("text"),
             "Источник": r.get("source"), "Модель": r.get("model"), "Секунд на расшифровку": r.get("seconds"), "Ошибка": r.get("error")}
            for pid, r in result.items()]
    root.mkdir(parents=True, exist_ok=True)
    with (root / "transcripts.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["ID поста"]); w.writeheader(); w.writerows(rows)
    try:
        import pandas as pd
        pd.DataFrame(rows).to_excel(root / "transcripts.xlsx", index=False)
    except Exception as exc:
        print("XLSX не записан:", exc)
    return root / "transcripts.xlsx"


def sample_ids(cases_root: Path = Path("cases")):
    """ID постов из выборок всех школ (cases/<школа>/sample_ids.txt); пусто = все видео."""
    ids = set()
    for f in cases_root.glob("*/sample_ids.txt"):
        ids |= {l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip()}
    return ids


def watch(root: Path, model_name: str, stop_file: Path, only_sample=True, interval=45):
    """Фоновый режим: подхватывать новые video.* по мере скачивания и расшифровывать; выйти, когда
    появился stop_file и очередь пуста."""
    print(f"Whisper watch: модель {model_name}, стоп-файл {stop_file}", flush=True)
    done_total = 0
    while True:
        ids = sample_ids() if only_sample else None
        posts_dir = root / "raw" / "posts"
        todo = []
        for d in sorted(posts_dir.glob("*")):
            if ids and d.name not in ids:
                continue
            c = d / "transcript.whisper.json"
            if find_video(d) and not (c.exists() and c.stat().st_size > 2):
                todo.append(d.name)
        if todo:
            print(f"Whisper watch: в очереди {len(todo)}", flush=True)
            transcribe_dir(root, post_ids=set(todo[:200]), model_name=model_name)
            done_total += len(todo[:200])
            continue
        if stop_file.exists():
            print(f"Whisper watch: очередь пуста, сбор завершён — выход (расшифровано за сеанс {done_total})", flush=True)
            return 0
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="фоновый режим: расшифровывать видео по мере появления")
    ap.add_argument("--stop-file", type=Path, default=Path("cases") / "whisper_stop.flag")
    ap.add_argument("--all-videos", action="store_true", help="в режиме watch: не ограничиваться выборками школ")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--language", default=DEFAULT_LANGUAGE)
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--compute", default=DEFAULT_COMPUTE)
    ap.add_argument("--post", action="append", default=[])
    ap.add_argument("--urls-file", type=Path, help="скачать видео по ссылкам через yt-dlp перед расшифровкой")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="печатать каждую фразу")
    a = ap.parse_args()

    if a.watch:
        return watch(a.root, a.model, a.stop_file, only_sample=not a.all_videos)

    if a.urls_file:
        import re
        for line in a.urls_file.read_text(encoding="utf-8-sig").splitlines():
            url = line.strip()
            m = re.search(r"/(video|photo)/(\d+)", url)
            if not url or not m:
                continue
            d = a.root / "raw" / "posts" / m.group(2)
            if find_video(d):
                continue
            print("Скачиваю", url)
            download_ytdlp(url, d)

    res = transcribe_dir(a.root, set(a.post) or None, a.model, a.language, a.force, a.verbose, a.device, a.compute)
    if not res:
        print("Видео не найдено в", a.root / "raw" / "posts"); return 1
    t = write_table(res, a.root)
    ok = sum(1 for r in res.values() if r.get("text"))
    print(f"\nГотово: с речью {ok} из {len(res)}. Таблица: {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
