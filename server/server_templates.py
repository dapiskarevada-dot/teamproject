#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ШАБЛОНЫ КАРТИНОК (мемы, айсберги, тир-листы, «скрины чатов» и т.п.) — кластеризация снизу вверх.

Для каждого поста берёт его картинки (кадры видео out/frames/<id>/, слайды out/slides/<id>/; на Mac —
cases/tiktok_media/raw/posts/<id>/), выкидывает почти одинаковые внутри поста (dHash), считает визуальный
отпечаток CLIP (на GPU), группирует похожие картинки (PCA + HDBSCAN) и рисует «контактные листы»
по крупнейшим кластерам, чтобы просмотреть их глазами без скачивания всех кадров.

    python server_templates.py                       # на поде: out/frames + out/slides
    python server_templates.py --root ../cases/tiktok_media/raw/posts   # на Mac: слайды каруселей

Результат в out/templates/ (≈200–300 МБ, скачать архивом templates.tgz):
    images.csv          — post_id, kind, файл, cluster (-1 = шум)
    clusters.csv        — cluster, картинок, постов, пример post_id
    embeddings.npy      — отпечатки (float16), порядок = images.csv
    sheets/cluster_XXXX.jpg — по 24 картинки из каждого из топ-кластеров
Дальше на Mac: clusters → join с таблицами по post_id (авторы, даты, школы, текст с экрана).
"""
from __future__ import annotations

import argparse, csv, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "templates"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    v = 0
    for r in range(size):
        row = px[r * (size + 1):(r + 1) * (size + 1)]
        for i in range(size):
            v = (v << 1) | (1 if row[i] > row[i + 1] else 0)
    return v


def collect(roots, max_per_post):
    """[(post_id, kind, path)] с дедупом почти одинаковых картинок внутри поста."""
    from PIL import Image
    items = []
    posts = {}
    for root, kind in roots:
        if not root.exists():
            continue
        for d in root.iterdir():
            if not d.is_dir():
                continue
            files = [p for p in sorted(d.rglob("*")) if p.suffix.lower() in IMG_EXT and "contact" not in p.name]
            if files:
                posts.setdefault(d.name, []).extend((kind, f) for f in files)
    print(f"Постов с картинками: {len(posts)}", flush=True)

    def pick(pid):
        kept, hashes = [], []
        for kind, f in posts[pid]:
            try:
                with Image.open(f) as im:
                    h = dhash(im)
            except Exception:
                continue
            if all(bin(h ^ k).count("1") > 10 for k in hashes):
                kept.append((pid, kind, f)); hashes.append(h)
            if len(kept) >= max_per_post:
                break
        return kept

    with ThreadPoolExecutor(16) as ex:
        for r in ex.map(pick, list(posts)):
            items.extend(r)
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=[], help="папка с подпапками <post_id>/ (можно несколько)")
    ap.add_argument("--max-per-post", type=int, default=3, help="не больше N разных картинок с поста")
    ap.add_argument("--model", default="ViT-B-32", help="open_clip модель")
    ap.add_argument("--pretrained", default="laion2b_s34b_b79k")
    ap.add_argument("--min-cluster", type=int, default=15, help="минимум картинок в шаблоне")
    ap.add_argument("--min-samples", type=int, default=5)
    ap.add_argument("--leaf", action="store_true", help="мелкие однородные кластеры (cluster_selection_method=leaf)")
    ap.add_argument("--out", default="", help="папка результата (по умолчанию out/templates)")
    ap.add_argument("--reuse", action="store_true", help="взять готовые embeddings.npy + images.csv из out/templates, без пересчёта")
    ap.add_argument("--sheets", type=int, default=80, help="контактных листов по крупнейшим кластерам")
    a = ap.parse_args()
    global OUT
    src_dir = OUT
    if a.out:
        OUT = HERE / "out" / a.out

    import numpy as np
    from PIL import Image
    roots = [(Path(r), "img") for r in a.root] or [(HERE / "out" / "frames", "video"), (HERE / "out" / "slides", "carousel")]
    OUT.mkdir(parents=True, exist_ok=True); (OUT / "sheets").mkdir(exist_ok=True)

    t0 = time.monotonic()
    if a.reuse:
        rows = list(csv.DictReader(open(src_dir / "images.csv", encoding="utf-8")))
        base = {"video": HERE / "out" / "frames", "carousel": HERE / "out" / "slides"}
        items = [(r["post_id"], r["kind"], base.get(r["kind"], Path(a.root[0]) if a.root else HERE) / r["post_id"] / r["file"]) for r in rows]
        emb = np.load(src_dir / "embeddings.npy")
        print(f"Взято готовое: {len(items)} картинок", flush=True)
    else:
        items = collect(roots, a.max_per_post)
        print(f"Картинок после дедупа: {len(items)} ({time.monotonic() - t0:.0f} с)", flush=True)

    if not a.reuse:
        import torch, open_clip
        dev = "cuda" if torch.cuda.is_available() else ("mps" if torch.backends.mps.is_available() else "cpu")
        model, _, prep = open_clip.create_model_and_transforms(a.model, pretrained=a.pretrained, device=dev)
        model.eval()
        if dev == "cuda":
            model.half()
        emb = np.zeros((len(items), model.visual.output_dim), dtype=np.float16)
        B = 256

        def load(it):
            try:
                with Image.open(it[2]) as im:
                    return prep(im.convert("RGB"))
            except Exception:
                return torch.zeros(3, 224, 224)

        t1 = time.monotonic()
        with ThreadPoolExecutor(16) as ex, torch.no_grad():
            for s in range(0, len(items), B):
                x = torch.stack(list(ex.map(load, items[s:s + B]))).to(dev)
                if dev == "cuda":
                    x = x.half()
                f = model.encode_image(x).float()
                f = f / f.norm(dim=-1, keepdim=True)
                emb[s:s + len(f)] = f.cpu().numpy().astype(np.float16)
                if (s // B) % 40 == 0:
                    print(f"  отпечатки {s + len(f)}/{len(items)} | {(s + len(f)) / max(1e-6, time.monotonic() - t1):.0f} картинок/с", flush=True)
        np.save(OUT / "embeddings.npy", emb)

    from sklearn.decomposition import PCA
    X = PCA(n_components=32, random_state=0).fit_transform(emb.astype(np.float32))
    try:
        import hdbscan
        lab = hdbscan.HDBSCAN(min_cluster_size=a.min_cluster, min_samples=a.min_samples, core_dist_n_jobs=-1,
                              cluster_selection_method="leaf" if a.leaf else "eom").fit_predict(X)
    except ImportError:
        from sklearn.cluster import HDBSCAN
        lab = HDBSCAN(min_cluster_size=a.min_cluster, min_samples=a.min_samples,
                      cluster_selection_method="leaf" if a.leaf else "eom").fit_predict(X)
    print(f"Кластеров: {lab.max() + 1}, в шуме {int((lab == -1).sum())} из {len(lab)} ({time.monotonic() - t0:.0f} с всего)", flush=True)

    with open(OUT / "images.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["post_id", "kind", "file", "cluster"])
        for (pid, kind, p), c in zip(items, lab):
            w.writerow([pid, kind, p.name, int(c)])
    from collections import defaultdict
    by = defaultdict(list)
    for i, c in enumerate(lab):
        if c >= 0:
            by[int(c)].append(i)
    order = sorted(by, key=lambda c: -len({items[i][0] for i in by[c]}))
    with open(OUT / "clusters.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["cluster", "картинок", "постов", "пример_post_id"])
        for c in order:
            w.writerow([c, len(by[c]), len({items[i][0] for i in by[c]}), items[by[c][0]][0]])

    # контактные листы: 24 картинки из разных постов, ближайшие к центру кластера
    T = 220
    for rank, c in enumerate(order[: a.sheets]):
        idx = by[c]
        cen = emb[idx].astype(np.float32).mean(0)
        idx = sorted(idx, key=lambda i: -float(emb[i].astype(np.float32) @ cen))
        seen, pick = set(), []
        for i in idx:
            if items[i][0] not in seen:
                seen.add(items[i][0]); pick.append(i)
            if len(pick) == 24:
                break
        sheet = Image.new("RGB", (6 * T, 4 * T + 40), (245, 245, 248))
        from PIL import ImageDraw
        ImageDraw.Draw(sheet).text((10, 12), f"cluster {c}: {len(by[c])} img, {len({items[i][0] for i in by[c]})} posts", fill=(20, 20, 20))
        for k, i in enumerate(pick):
            try:
                with Image.open(items[i][2]) as im:
                    im = im.convert("RGB"); im.thumbnail((T - 6, T - 6))
                    sheet.paste(im, ((k % 6) * T + 3, 40 + (k // 6) * T + 3))
            except Exception:
                pass
        sheet.save(OUT / "sheets" / f"{rank + 1:03d}_cluster_{c:04d}.jpg", quality=80)
    print(f"Готово: out/templates/ (images.csv, clusters.csv, embeddings.npy, sheets/ — {min(a.sheets, len(order))} листов)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
