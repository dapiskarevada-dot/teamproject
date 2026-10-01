#!/usr/bin/env python
# -*- coding: utf-8 -*-

r"""
Run PaddleOCR locally on already-saved images.

This script NEVER opens TikTok and NEVER makes TikTok requests.

Examples:
    python .\paddle_ocr_folder.py .\cases\tiktok_media\raw\posts\POST_ID\images
    python .\paddle_ocr_folder.py .\cases\tiktok_media\raw\posts --recursive
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".avif", ".img"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def json_dump(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def gather_images(path: Path, recursive: bool):
    if path.is_file():
        return [path]
    globber = path.rglob("*") if recursive else path.glob("*")
    return sorted(p for p in globber if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def normalize_result(result):
    if result is None:
        return [], []
    if not isinstance(result, (list, tuple)):
        result = [result]

    texts, scores = [], []
    for item in result:
        data = None
        if isinstance(item, dict):
            data = item
        else:
            for attr in ("json", "to_dict"):
                try:
                    value = getattr(item, attr, None)
                    if callable(value):
                        value = value()
                    if isinstance(value, dict):
                        data = value
                        break
                except Exception:
                    pass

        if isinstance(data, dict):
            candidates = [data]
            if isinstance(data.get("res"), dict):
                candidates.append(data["res"])
            found = False
            for d in candidates:
                rt = d.get("rec_texts")
                rs = d.get("rec_scores")
                if isinstance(rt, list):
                    texts.extend([str(x) for x in rt])
                    if isinstance(rs, list):
                        scores.extend([float(x) if x is not None else None for x in rs[:len(rt)]])
                    else:
                        scores.extend([None] * len(rt))
                    found = True
                    break
            if found:
                continue

        try:
            nested = item
            if isinstance(nested, list) and len(nested) == 1 and isinstance(nested[0], list):
                nested = nested[0]
            if isinstance(nested, list):
                for row in nested:
                    if (
                        isinstance(row, (list, tuple))
                        and len(row) >= 2
                        and isinstance(row[1], (list, tuple))
                        and len(row[1]) >= 2
                    ):
                        texts.append(str(row[1][0]))
                        try:
                            scores.append(float(row[1][1]))
                        except Exception:
                            scores.append(None)
        except Exception:
            pass
    return texts, scores


def run_ocr(engine, image_path: Path):
    if hasattr(engine, "predict"):
        result = engine.predict(str(image_path))
    else:
        result = engine.ocr(str(image_path), cls=True)

    texts, scores = normalize_result(result)
    valid_scores = [s for s in scores if isinstance(s, (int, float))]
    mean_score = sum(valid_scores) / len(valid_scores) if valid_scores else None

    return {
        "file": str(image_path),
        "filename": image_path.name,
        "text": "\n".join(texts),
        "lines": texts,
        "scores": scores,
        "line_count": len(texts),
        "mean_score": mean_score,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="Image file or directory")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--lang", default="ru")
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()

    try:
        from paddleocr import PaddleOCR
    except ImportError:
        raise SystemExit("PaddleOCR is not installed in this environment.")

    images = gather_images(args.input, args.recursive)
    if not images:
        raise SystemExit("No images found.")

    out_dir = args.out_dir or (
        args.input.parent / "ocr_paddle" if args.input.is_file() else args.input / "ocr_paddle"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Images:", len(images))
    print("Language:", args.lang)
    print("TikTok requests: NONE")

    try:
        engine = PaddleOCR(
            lang=args.lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except TypeError:
        engine = PaddleOCR(lang=args.lang, use_angle_cls=True, show_log=False)

    rows = []
    for i, path in enumerate(images, 1):
        print(f"[{i}/{len(images)}] {path}")
        try:
            rec = run_ocr(engine, path)
            rec["status"] = "success"
            rec["error"] = None
        except Exception as exc:
            rec = {
                "file": str(path),
                "filename": path.name,
                "text": "",
                "lines": [],
                "scores": [],
                "line_count": 0,
                "mean_score": None,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        rows.append(rec)
        json_dump(out_dir / f"{path.stem}.ocr.json", {"created_at_utc": utc_now(), **rec})

    csv_path = out_dir / "ocr_results.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["file", "filename", "status", "line_count", "mean_score", "text", "error"],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k) for k in writer.fieldnames})

    json_dump(out_dir / "ocr_results.json", {
        "created_at_utc": utc_now(),
        "language": args.lang,
        "count": len(rows),
        "results": rows,
    })

    txt_path = out_dir / "ocr_results.txt"
    with txt_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write("=" * 80 + "\n")
            f.write(r["file"] + "\n")
            f.write("-" * 80 + "\n")
            f.write(r["text"] + "\n\n")

    print("\nCSV:", csv_path)
    print("JSON:", out_dir / "ocr_results.json")
    print("TXT:", txt_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
