#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Стенд для сравнения распознавания текста на картинках TikTok-каруселей.

Прогоняет одни и те же картинки через несколько движков и пишет таблицу
ocr_benchmark.xlsx (лист «Рядом»: одна строка = картинка, колонки = движки;
лист «Подробно»: движок, время, текст) + ocr_benchmark.json.

Движки (включаются флагами; недоступные пропускаются с пометкой):
  --paddle      PaddleOCR (кириллица)            pip install paddlepaddle paddleocr
  --easyocr     EasyOCR (ru+en)                   pip install easyocr
  --tesseract   Tesseract CLI (rus+eng)           brew install tesseract tesseract-lang
  --ollama MODEL   локальная VLM через Ollama     brew install ollama; ollama pull qwen2.5vl:7b
  --openrouter MODEL  VLM через OpenRouter        ключ в OPENROUTER_API_KEY или openrouter_key.txt
  --all         всё, что получится импортировать

Примеры:
  python ocr_benchmark.py cases/tiktok_media/raw/posts --recursive --all
  python ocr_benchmark.py cases/tiktok_media/raw/posts --recursive --easyocr --ollama qwen2.5vl:7b
  python ocr_benchmark.py путь/к/картинкам --openrouter google/gemini-2.5-flash --limit 10
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

VLM_PROMPT = (
    "Это слайд из TikTok-карусели. 1) Перепиши ВЕСЬ текст со слайда дословно, "
    "в порядке чтения, включая подписи, стикеры и мелкий текст. "
    "2) Затем с новой строки напиши: ШКОЛА: <названия онлайн-школ подготовки к ЕГЭ/ОГЭ, "
    "упомянутые на слайде (Умскул, ЕГЭленд, Фоксфорд, Сотка, Вебиум, Турбо и др.), "
    "или «нет»>. 3) С новой строки: КОНТЕКСТ: <одной фразой — реклама, отзыв, сравнение школ, "
    "мем/юмор, учебный контент, другое>. Отвечай на русском, без пояснений."
)


def find_images(root: Path, recursive: bool, limit: int | None):
    it = root.rglob("*") if recursive else root.glob("*")
    files = sorted(p for p in it if p.suffix.lower() in IMAGE_EXT and p.is_file())
    return files[:limit] if limit else files


# ---------------- движки ----------------

class Engine:
    name = "base"

    def __init__(self):
        self.error = None

    def run(self, path: Path) -> str:
        raise NotImplementedError


class Paddle(Engine):
    name = "paddleocr"

    def __init__(self):
        super().__init__()
        try:
            from paddleocr import PaddleOCR
            try:
                self.ocr = PaddleOCR(lang="ru", use_textline_orientation=True)
            except TypeError:
                self.ocr = PaddleOCR(lang="ru", use_angle_cls=True)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def run(self, path):
        lines = []
        if hasattr(self.ocr, "predict"):
            for res in self.ocr.predict(str(path)):
                d = res.json if hasattr(res, "json") else res
                d = d.get("res", d) if isinstance(d, dict) else d
                lines += list(d.get("rec_texts") or [])
        else:
            for page in self.ocr.ocr(str(path), cls=True) or []:
                for item in page or []:
                    lines.append(item[1][0])
        return "\n".join(lines)


class Easy(Engine):
    name = "easyocr"

    def __init__(self):
        super().__init__()
        try:
            import easyocr
            self.reader = easyocr.Reader(["ru", "en"], gpu=False, verbose=False)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def run(self, path):
        return "\n".join(t for _, t, _ in self.reader.readtext(str(path), paragraph=False))


class Tess(Engine):
    name = "tesseract"

    def __init__(self):
        super().__init__()
        if not shutil.which("tesseract"):
            self.error = "tesseract не установлен (brew install tesseract tesseract-lang)"

    def run(self, path):
        r = subprocess.run(["tesseract", str(path), "-", "-l", "rus+eng", "--psm", "6"],
                           capture_output=True, text=True, timeout=120)
        return r.stdout.strip()


class Ollama(Engine):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.name = f"ollama:{model}"
        try:
            import requests
            r = requests.get("http://localhost:11434/api/tags", timeout=5)
            names = [m["name"] for m in r.json().get("models", [])]
            if not any(n.split(":")[0] == model.split(":")[0] for n in names):
                self.error = f"модель {model} не скачана: ollama pull {model}"
        except Exception as exc:
            self.error = f"Ollama не запущен (brew install ollama; ollama serve): {type(exc).__name__}"

    def run(self, path):
        import requests
        b64 = base64.b64encode(path.read_bytes()).decode()
        r = requests.post("http://localhost:11434/api/generate", json={
            "model": self.model, "prompt": VLM_PROMPT, "images": [b64], "stream": False,
            "options": {"temperature": 0},
        }, timeout=600)
        r.raise_for_status()
        return r.json().get("response", "").strip()


class OpenRouter(Engine):
    def __init__(self, model, base_url=None):
        super().__init__()
        self.model = model
        self.name = f"api:{model}"
        self.base_url = base_url or "https://openrouter.ai/api/v1"
        key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not key and Path("openrouter_key.txt").exists():
            key = Path("openrouter_key.txt").read_text(encoding="utf-8").strip()
        self.key = key
        if not key:
            self.error = "нет ключа: переменная OPENROUTER_API_KEY или файл openrouter_key.txt"

    def run(self, path):
        import requests
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        b64 = base64.b64encode(path.read_bytes()).decode()
        r = requests.post(f"{self.base_url}/chat/completions",
                          headers={"Authorization": f"Bearer {self.key}"},
                          json={"model": self.model, "temperature": 0, "messages": [{
                              "role": "user", "content": [
                                  {"type": "text", "text": VLM_PROMPT},
                                  {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                              ]}]}, timeout=180)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


# ---------------- запуск ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--limit", type=int, help="взять только первые N картинок")
    ap.add_argument("--paddle", action="store_true")
    ap.add_argument("--easyocr", action="store_true")
    ap.add_argument("--tesseract", action="store_true")
    ap.add_argument("--ollama", metavar="MODEL", action="append", default=[])
    ap.add_argument("--openrouter", metavar="MODEL", action="append", default=[])
    ap.add_argument("--base-url", help="другой OpenAI-совместимый API вместо OpenRouter")
    ap.add_argument("--all", action="store_true", help="paddle+easyocr+tesseract+ollama qwen2.5vl:7b")
    ap.add_argument("--out", type=Path, default=Path("ocr_benchmark"))
    a = ap.parse_args()

    if a.all:
        a.paddle = a.easyocr = a.tesseract = True
        if not a.ollama:
            a.ollama = ["qwen2.5vl:7b"]

    images = find_images(a.root, a.recursive, a.limit)
    if not images:
        print("Картинки не найдены в", a.root); return 1
    print(f"Картинок: {len(images)}")

    engines = []
    if a.paddle: engines.append(Paddle())
    if a.easyocr: engines.append(Easy())
    if a.tesseract: engines.append(Tess())
    for m in a.ollama: engines.append(Ollama(m))
    for m in a.openrouter: engines.append(OpenRouter(m, a.base_url))
    if not engines:
        print("Не выбран ни один движок (см. --help)"); return 1

    for e in engines:
        print(f"{'OK  ' if not e.error else 'SKIP'} {e.name}" + (f" — {e.error}" if e.error else ""))
    engines = [e for e in engines if not e.error]
    if not engines:
        return 1

    a.out.mkdir(parents=True, exist_ok=True)
    detailed, side = [], []
    for i, img in enumerate(images, 1):
        row = {"image": str(img), "post_id": img.parent.parent.name if img.parent.name == "images" else ""}
        print(f"\n[{i}/{len(images)}] {img}")
        for e in engines:
            t0 = time.time()
            try:
                text = e.run(img)
                err = ""
            except Exception as exc:
                text, err = "", f"{type(exc).__name__}: {str(exc)[:200]}"
            dt = round(time.time() - t0, 2)
            detailed.append({"image": str(img), "post_id": row["post_id"], "engine": e.name,
                             "seconds": dt, "chars": len(text), "text": text, "error": err})
            row[e.name] = text if not err else f"[ошибка] {err}"
            row[f"{e.name} (сек)"] = dt
            preview = (text or err).replace("\n", " | ")[:90]
            print(f"   {e.name:<28} {dt:>6.1f}s  {preview}")
        side.append(row)

    (a.out / "ocr_benchmark.json").write_text(json.dumps(detailed, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import pandas as pd
        with pd.ExcelWriter(a.out / "ocr_benchmark.xlsx") as w:
            pd.DataFrame(side).to_excel(w, sheet_name="Рядом", index=False)
            pd.DataFrame(detailed).to_excel(w, sheet_name="Подробно", index=False)
            summ = (pd.DataFrame(detailed).groupby("engine")
                    .agg(картинок=("image", "count"), ошибок=("error", lambda s: (s != "").sum()),
                         сек_среднее=("seconds", "mean"), символов_среднее=("chars", "mean")).round(1))
            summ.to_excel(w, sheet_name="Сводка")
        print("\nXLSX:", a.out / "ocr_benchmark.xlsx")
    except Exception as exc:
        print("XLSX не записан:", exc)
    print("JSON:", a.out / "ocr_benchmark.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
