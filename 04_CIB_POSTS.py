"""
STAGE 2 / ШАГ 04 — ПОИСК СКООРДИНИРОВАННЫХ ПОСТОВ (только посты, без комментариев, без платных моделей)

Что ищет:
  1. ШАБЛОНЫ: почти одинаковые тексты у РАЗНЫХ авторов (описание, речь в видео, субтитры, текст на экране,
     текст каруселей). Похожесть — доля общих 5-словных кусков (Жаккар), поиск через MinHash.
     Группа интересна, если в ней 3+ разных автора.
  2. ОБЩИЕ «ХВОСТЫ»: один и тот же промокод, реферальная ссылка, телеграм-канал или @упоминание
     у 3+ разных авторов.
  3. ПОРТРЕТ АВТОРОВ: сколько у автора постов, про сколько школ, доля одной школы, подписчики,
     в скольких шаблонах и общих хвостах он участвует.

Вход:  ANALYSIS_STAGE1_V4/01_posts_master_features_v4.parquet (или .csv) — результат 02
Выход: ANALYSIS_STAGE2_CIB/
   01_template_clusters.csv      — группы-шаблоны (одна строка = группа)
   02_template_cluster_posts.csv — посты в группах
   03_shared_tokens.csv          — общие промокоды / ссылки / тг / @ (одна строка = хвост)
   04_shared_token_posts.csv     — посты с этими хвостами
   05_author_flags.csv           — портрет авторов
   06_CIB_REVIEW.xlsx            — главное для ручной проверки (топ групп и хвостов со ссылками)
   07_CIB_SUMMARY.json

Это кандидаты, а не доказательства: каждую группу из 06 надо открыть глазами.
Запуск: python 04_CIB_POSTS.py        (нужны только pandas, numpy, openpyxl)
"""
from __future__ import annotations

import json
import re
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
MASTER_PATH = HERE / "ANALYSIS_STAGE1_V4" / "01_posts_master_features_v4.parquet"
OUT_DIR = HERE / "ANALYSIS_STAGE2_CIB"

# ---------------- настройки ----------------
FIELDS = ["description", "transcript", "subtitles", "screen_text", "slides_text"]
MIN_WORDS = {"description": 8, "transcript": 20, "subtitles": 15, "screen_text": 10, "slides_text": 15}
MAX_WORDS = 400            # длинные тексты режем: для шаблона хватает начала
SHINGLE = 5                # куски по 5 слов
N_PERM = 64                # MinHash
BANDS, ROWS = 16, 4        # LSH: ловит пары примерно от 50% похожести
JACCARD_MIN = 0.5          # пара «похожа», если общих кусков >= 50 %
MAX_BUCKET = 400           # корзина больше — это общий шаблон всего TikTok (не берём в пары, но считаем)
MIN_AUTHORS = 3            # группа/хвост интересны от 3 разных авторов
BURST_DAYS = 7

STOP_TOKENS = {"егэ", "огэ", "fyp", "foryou", "рек", "рекомендации", "реки", "school", "study", "tiktok", "тикток"}
SELF_SOUNDS = re.compile(r"оригинальн|original|оригинал", re.I)


# ---------------- общие функции ----------------
def clean(x) -> str:
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    s = str(x).strip()
    return "" if s.lower() in {"nan", "none", "null"} else s


def split_multi(x):
    return [i.strip() for i in clean(x).split(";") if i.strip()]


URL_RX = re.compile(r"https?://\S+|www\.\S+|\b[\w-]+\.(?:ru|com|me|io|org|net|рф)/\S*", re.I)


def norm_for_template(t: str) -> list[str]:
    t = clean(t).lower().replace("ё", "е")
    t = re.sub(r"\[\d+\]", " ", t)               # [1] [2] номера слайдов
    t = URL_RX.sub(" ", t)
    t = re.sub(r"[@#][\w.]+", " ", t)            # хэштеги и @ — не шаблон текста
    t = re.sub(r"\d+", "0", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return t.split()[:MAX_WORDS]


def shingles(words: list[str]) -> np.ndarray:
    if len(words) < SHINGLE:
        return np.array([], dtype=np.int64)
    sh = {zlib.crc32(" ".join(words[i:i + SHINGLE]).encode()) for i in range(len(words) - SHINGLE + 1)}
    return np.fromiter(sh, dtype=np.int64, count=len(sh))


_rng = np.random.default_rng(42)
_A = _rng.integers(1, 2 ** 30, N_PERM, dtype=np.int64)
_B = _rng.integers(0, 2 ** 30, N_PERM, dtype=np.int64)
_P = np.int64(4294967311)


def minhash(sh: np.ndarray) -> np.ndarray:
    return ((np.outer(_A, sh) + _B[:, None]) % _P).min(axis=1)


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def max_in_window(dates: pd.Series, days: int) -> int:
    d = dates.dropna().sort_values().values
    if len(d) == 0:
        return 0
    best, j = 1, 0
    w = np.timedelta64(days, "D")
    for i in range(len(d)):
        while d[i] - d[j] > w:
            j += 1
        best = max(best, i - j + 1)
    return best


def load_master() -> pd.DataFrame:
    if MASTER_PATH.exists():
        m = pd.read_parquet(MASTER_PATH)
    elif MASTER_PATH.with_suffix(".csv").exists():
        m = pd.read_csv(MASTER_PATH.with_suffix(".csv"), dtype=str, keep_default_na=False)
    else:
        raise FileNotFoundError(f"Нет {MASTER_PATH}: сначала запустите 02_STAGE1_V4_BRANDS.py")
    need = ["post_id_master", "author_key_final", "create_time_parsed", "schools_canonical"]
    miss = [c for c in need if c not in m.columns]
    if miss:
        raise RuntimeError(f"В master нет колонок {miss}: нужен свежий 02")
    m["create_time_parsed"] = pd.to_datetime(m["create_time_parsed"], errors="coerce")
    for c in ("views", "author_followers", "likes"):
        if c in m.columns:
            m[c] = pd.to_numeric(m[c], errors="coerce")
    for c in ("has_ad_disclosure", "has_promo_code", "has_negative", "has_ambassador"):
        if c in m.columns:
            m[c] = m[c].astype(str).str.lower().isin(["true", "1"])
        else:
            m[c] = False
    for c in ["author_username", "post_url", "music_title", "author_affiliation_hint", "author_created",
              "schools_unsure", "school_fields"] + FIELDS:
        if c not in m.columns:
            m[c] = ""
        m[c] = m[c].map(clean)
    m["author_key_final"] = m["author_key_final"].map(clean)
    m = m[m["author_key_final"].ne("")].copy()
    return m.reset_index(drop=True)


# ============================================================
# 1. ШАБЛОНЫ
# ============================================================
def find_templates(m: pd.DataFrame):
    docs = []          # (doc_id, row_idx, field)
    sigs, shs = [], []
    for field in FIELDS:
        for i, t in enumerate(m[field].values):
            if not t:
                continue
            w = norm_for_template(t)
            if len(w) < MIN_WORDS[field]:
                continue
            sh = shingles(w)
            if len(sh) == 0:
                continue
            docs.append((len(docs), i, field))
            shs.append(sh)
            sigs.append(minhash(sh))
    print(f"  текстов для сравнения: {len(docs):,}")
    if not docs:
        return pd.DataFrame(), pd.DataFrame()
    sigs = np.vstack(sigs)
    authors = m["author_key_final"].values

    # кандидаты через LSH (только тексты одного типа поля)
    cand, skipped_big = set(), 0
    for b in range(BANDS):
        buckets = defaultdict(list)
        part = sigs[:, b * ROWS:(b + 1) * ROWS]
        for d, row in enumerate(part):
            buckets[(docs[d][2], row.tobytes())].append(d)
        for ids in buckets.values():
            if len(ids) < 2:
                continue
            if len({authors[docs[d][1]] for d in ids}) < 2:
                continue
            if len(ids) > MAX_BUCKET:
                skipped_big += 1
                ids = ids[:MAX_BUCKET]
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    cand.add((ids[x], ids[y]))
    print(f"  пар-кандидатов: {len(cand):,} (очень больших корзин урезано: {skipped_big})")

    dsu, sets = DSU(), {}
    kept = 0
    for a, b in cand:
        ra, rb = docs[a][1], docs[b][1]
        if ra == rb:
            continue
        sa = sets.setdefault(a, set(shs[a].tolist()))
        sb = sets.setdefault(b, set(shs[b].tolist()))
        j = len(sa & sb) / max(1, len(sa | sb))
        if j >= JACCARD_MIN:
            dsu.union(a, b)
            kept += 1
    print(f"  похожих пар: {kept:,}")

    groups = defaultdict(list)
    for d in dsu.p:
        groups[dsu.find(d)].append(d)

    rows, members = [], []
    cid = 0
    for root, ids in groups.items():
        idx = sorted({docs[d][1] for d in ids})
        g = m.iloc[idx]
        n_auth = g["author_key_final"].nunique()
        if n_auth < MIN_AUTHORS:
            continue
        cid += 1
        field = Counter(docs[d][2] for d in ids).most_common(1)[0][0]
        schools = Counter(s for v in g["schools_canonical"] for s in split_multi(v))
        top_school, top_n = (schools.most_common(1)[0] if schools else ("", 0))
        music = Counter(x for x in g["music_title"] if x and not SELF_SOUNDS.search(x))
        song_share = (music.most_common(1)[0][1] / len(g)) if music else 0.0
        # школа названа в САМОМ шаблонном тексте? (а не только в описании рядом)
        in_tpl = g["school_fields"].map(lambda v: any(x.endswith("@" + field) for x in split_multi(v)))
        school_in_template = round(in_tpl.mean(), 2)
        # трендовый звук / песня: речь одинаковая, но школа в ней почти не звучит
        same_song = field in ("transcript", "subtitles") and (song_share >= 0.6 or school_in_template < 0.3)
        dates = g["create_time_parsed"]
        burst = max_in_window(dates, BURST_DAYS)
        aff = Counter(h for v in g["author_affiliation_hint"] for h in split_multi(v))
        example = m.iloc[idx[0]][field]
        rows.append({
            "cluster_id": f"T{cid:04d}",
            "field": field,
            "posts": len(g),
            "authors": n_auth,
            "top_school": top_school,
            "top_school_share": round(top_n / len(g), 2) if len(g) else 0,
            "schools": "; ".join(f"{s} ({n})" for s, n in schools.most_common(5)),
            "first_date": dates.min(),
            "last_date": dates.max(),
            "days_span": (dates.max() - dates.min()).days if dates.notna().any() else None,
            f"max_posts_in_{BURST_DAYS}d": burst,
            "burst_share": round(burst / len(g), 2),
            "ad_disclosure_share": round(g["has_ad_disclosure"].mean(), 2),
            "promo_share": round(g["has_promo_code"].mean(), 2),
            "negative_share": round(g["has_negative"].mean(), 2),
            "authors_with_school_in_profile": "; ".join(f"{s} ({n})" for s, n in aff.most_common(3)),
            "median_followers": g["author_followers"].median() if "author_followers" in g else None,
            "total_views": g["views"].sum() if "views" in g else None,
            "school_in_template_share": school_in_template,
            "same_song_lyrics": same_song,
            "example_text": example[:500],
            "example_url": m.iloc[idx[0]]["post_url"],
            "authors_list": "; ".join(sorted(g["author_username"].replace("", np.nan).dropna().unique())[:30]),
        })
        for i in idx:
            r = m.iloc[i]
            members.append({"cluster_id": f"T{cid:04d}", "post_id_master": r["post_id_master"],
                            "post_url": r["post_url"], "author_username": r["author_username"],
                            "author_key_final": r["author_key_final"], "create_time_parsed": r["create_time_parsed"],
                            "schools_canonical": r["schools_canonical"], "views": r.get("views"),
                            "has_ad_disclosure": r["has_ad_disclosure"], "text": r[field][:500]})
    clusters = pd.DataFrame(rows)
    if len(clusters):
        clusters["review_score"] = (clusters["authors"].clip(upper=30)
                                    + 5 * clusters["burst_share"]
                                    + 3 * clusters["top_school_share"]
                                    + 4 * clusters["school_in_template_share"]
                                    + 3 * clusters["promo_share"]
                                    + 2 * (1 - clusters["ad_disclosure_share"])
                                    - 20 * clusters["same_song_lyrics"].astype(int)
                                    - 10 * (clusters["top_school"] == "").astype(int))
        clusters = clusters.sort_values("review_score", ascending=False)
    return clusters, pd.DataFrame(members)


# ============================================================
# 2. ОБЩИЕ ХВОСТЫ
# ============================================================
TOKEN_RX = {
    # «код» только отдельным словом (не «кодификатор», «кодовое»); сам код — латиница/цифры
    "promo": re.compile(r"(?<![а-яa-z])(?:промокод\w*|промик\w*|по коду|код)(?![а-я])\s*[:\-—«\"']*\s*([a-z0-9_]{3,25})(?![а-я])", re.I),
    "ref_link": re.compile(r"((?:https?://)?(?:[\w-]+\.)+(?:ru|com|me|io|org|net|рф|link|page)/[\w\-./?=&%]{2,80})", re.I),
    "telegram": re.compile(r"(?:t\.me/|тгк?\s*[:\-—]?\s*@?|телеграм\w*\s*[:\-—]?\s*@?|tg\s*[:\-—]?\s*@?)([a-z][a-z0-9_]{3,31})", re.I),
    "mention": re.compile(r"(?<![\w.])@([a-z0-9_.]{3,30})", re.I),
}
PROMO_STOP = {"для", "на", "в", "и", "по", "от", "тут", "здесь", "профиле", "шапке", "био", "описании", "комментах",
              "комментариях", "скидку", "скидка", "скидкой", "курс", "курсы", "закрепе", "ниже", "выше", "это", "the"}


def norm_link(u: str) -> str:
    u = u.lower().rstrip(".,!?)»\"'")
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"[?&](?:utm_\w+|si|feature|is_from_webapp|sender_device)=[^&]*", "", u)
    return u.rstrip("/?&")


def find_tokens(m: pd.DataFrame):
    hits = []
    texts = (m["description"] + " \n " + m["screen_text"] + " \n " + m["slides_text"] + " \n " + m["transcript"]).str.lower()
    for i, t in enumerate(texts.values):
        if not t.strip():
            continue
        own = m.at[i, "author_username"].lower().lstrip("@")
        found = set()
        for typ, rx in TOKEN_RX.items():
            for mt in rx.finditer(t):
                tok = mt.group(1)
                if typ == "ref_link":
                    tok = norm_link(tok)
                    if tok.startswith(("tiktok.com", "vm.tiktok", "vt.tiktok")) or "/" not in tok:
                        continue
                    if tok.startswith("t.me/"):
                        typ_, tok = "telegram", tok[5:].split("/")[0].split("?")[0]
                    else:
                        typ_ = typ
                else:
                    typ_ = typ
                    tok = tok.strip("._").lower()
                if not tok or tok in STOP_TOKENS or tok == own or tok in {"https", "http", "www"} \
                        or not re.search(r"[a-zа-я]", tok):
                    continue
                if typ_ == "promo" and (tok in PROMO_STOP or (tok.isalpha() and len(tok) < 4) or tok.isdigit() and len(tok) < 3):
                    continue
                found.add((typ_, tok))
        for typ_, tok in found:
            hits.append((i, typ_, tok))
    if not hits:
        return pd.DataFrame(), pd.DataFrame()
    h = pd.DataFrame(hits, columns=["row", "type", "token"])
    h = h.join(m[["post_id_master", "post_url", "author_username", "author_key_final", "create_time_parsed",
                  "schools_canonical", "has_ad_disclosure", "author_affiliation_hint"]], on="row")
    # @упоминание — это и «тг», и TikTok-ник; один токен = одна строка (тип с наибольшим числом авторов)
    agg = []
    for (typ, tok), g in h.groupby(["type", "token"]):
        n_auth = g["author_key_final"].nunique()
        if n_auth < MIN_AUTHORS:
            continue
        schools = Counter(s for v in g["schools_canonical"] for s in split_multi(v))
        top_school, top_n = (schools.most_common(1)[0] if schools else ("", 0))
        dates = g["create_time_parsed"]
        burst = max_in_window(dates, BURST_DAYS)
        agg.append({
            "token_type": typ, "token": tok, "authors": n_auth, "posts": g["post_id_master"].nunique(),
            "top_school": top_school, "top_school_share": round(top_n / len(g), 2),
            "schools": "; ".join(f"{s} ({n})" for s, n in schools.most_common(5)),
            "first_date": dates.min(), "last_date": dates.max(),
            f"max_posts_in_{BURST_DAYS}d": burst, "burst_share": round(burst / len(g), 2),
            "ad_disclosure_share": round(g["has_ad_disclosure"].mean(), 2),
            "authors_list": "; ".join(sorted(g["author_username"].replace("", np.nan).dropna().unique())[:30]),
            "example_url": g["post_url"].iloc[0],
        })
    tokens = pd.DataFrame(agg)
    if len(tokens):
        # @-упоминание популярного аккаунта (препод, школа) — обычное дело; промокод/реф. ссылка у многих — сильнее
        weight = tokens["token_type"].map({"promo": 3, "ref_link": 3, "telegram": 2, "mention": 1})
        tokens["review_score"] = (weight * tokens["authors"].clip(upper=30) / 3
                                  + 5 * tokens["burst_share"] + 2 * (1 - tokens["ad_disclosure_share"]))
        tokens = tokens.sort_values("review_score", ascending=False)
        keep = set(zip(tokens["token_type"], tokens["token"]))
        h = h[[k in keep for k in zip(h["type"], h["token"])]]
    return tokens, h.drop(columns=["row"])


# ============================================================
# 3. ПОРТРЕТ АВТОРОВ
# ============================================================
def author_flags(m, members, token_posts):
    ex = m[["author_key_final", "author_username", "post_id_master", "schools_canonical", "create_time_parsed",
            "author_followers", "author_created", "has_ad_disclosure", "has_promo_code"]].copy()
    rows = []
    in_cl = members.groupby("author_key_final")["cluster_id"].nunique() if len(members) else pd.Series(dtype=int)
    in_tok = token_posts.groupby("author_key_final")["token"].nunique() if len(token_posts) else pd.Series(dtype=int)
    for a, g in ex.groupby("author_key_final"):
        sch = Counter(s for v in g["schools_canonical"] for s in split_multi(v))
        with_school = int(g["schools_canonical"].ne("").sum())
        top, top_n = (sch.most_common(1)[0] if sch else ("", 0))
        rows.append({
            "author_key_final": a,
            "author_username": next((u for u in g["author_username"] if u), ""),
            "posts": len(g), "posts_with_school": with_school,
            "schools_count": len(sch), "top_school": top,
            "top_school_share": round(top_n / with_school, 2) if with_school else 0,
            "single_school_focus": with_school >= 3 and len(sch) == 1,
            "followers": g["author_followers"].max(),
            "author_created": next((c for c in g["author_created"] if c), ""),
            "first_post": g["create_time_parsed"].min(), "last_post": g["create_time_parsed"].max(),
            "ad_disclosure_posts": int(g["has_ad_disclosure"].sum()), "promo_posts": int(g["has_promo_code"].sum()),
            "template_clusters": int(in_cl.get(a, 0)), "shared_tokens": int(in_tok.get(a, 0)),
        })
    af = pd.DataFrame(rows)
    af["in_coordination_signals"] = af["template_clusters"].gt(0) | af["shared_tokens"].gt(0)
    return af.sort_values(["template_clusters", "shared_tokens", "posts_with_school"], ascending=False)


# ============================================================
# MAIN
# ============================================================
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 70 + "\nSTAGE 2 / 04 — СКООРДИНИРОВАННЫЕ ПОСТЫ (шаблоны + общие хвосты)\n" + "=" * 70)
    m = load_master()
    print(f"Постов с автором: {len(m):,}, авторов: {m['author_key_final'].nunique():,}")

    print("\n1) Ищу шаблоны (почти одинаковые тексты у разных авторов)…")
    clusters, members = find_templates(m)
    print(f"  групп с {MIN_AUTHORS}+ авторами: {len(clusters):,}")

    print("\n2) Ищу общие промокоды / ссылки / тг / @…")
    tokens, token_posts = find_tokens(m)
    print(f"  хвостов у {MIN_AUTHORS}+ авторов: {len(tokens):,}")

    print("\n3) Портрет авторов…")
    af = author_flags(m, members, token_posts)

    def save(df, name):
        df.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    save(clusters, "01_template_clusters.csv")
    save(members, "02_template_cluster_posts.csv")
    save(tokens, "03_shared_tokens.csv")
    save(token_posts, "04_shared_token_posts.csv")
    save(af, "05_author_flags.csv")

    def xl(df):
        df = df.copy()
        for c in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[c]):
                df[c] = df[c].dt.strftime("%Y-%m-%d")
        return df

    guide = pd.DataFrame({"Как читать": [
        "Шаблоны — группы почти одинаковых текстов у РАЗНЫХ авторов. Чем выше review_score, тем подозрительнее.",
        "authors — сколько разных авторов; burst_share — какая доля постов вышла за 7 дней подряд (1.0 = все разом).",
        "ad_disclosure_share — доля постов с пометкой «реклама/сотрудничество». Шаблон без пометки — повод присмотреться.",
        "same_song_lyrics = TRUE — одинаковая речь, но школа в ней не звучит: трендовый звук/песня, а не шаблон. Эти группы",
        "   в лист не попали (они есть в 01_template_clusters.csv).",
        "school_in_template_share — доля постов, где школа названа в самом повторяющемся тексте (1.0 = шаблон про школу).",
        "Хвосты — один промокод / реф. ссылка / тг / @ у многих авторов. Промокод и реф. ссылка — сильнее, @ — слабее.",
        "Колонка «вердикт» — для вас: КАМПАНИЯ / ОФИЦИАЛЬНО (аккаунты самой школы) / СЛУЧАЙНО / НЕ ПОНЯТНО.",
        "Это кандидаты, не доказательства: открывайте example_url и посты из 02_/04_ файлов.",
    ]})
    top_cl = xl(clusters[~clusters["same_song_lyrics"]].head(150)) if len(clusters) else clusters
    top_tok = xl(tokens.head(150)) if len(tokens) else tokens
    for df in (top_cl, top_tok):
        if len(df):
            df.insert(0, "вердикт", "")
    with pd.ExcelWriter(OUT_DIR / "06_CIB_REVIEW.xlsx", engine="openpyxl") as w:
        guide.to_excel(w, sheet_name="Как читать", index=False)
        top_cl.to_excel(w, sheet_name="Шаблоны", index=False)
        top_tok.to_excel(w, sheet_name="Общие хвосты", index=False)
        xl(af[af["in_coordination_signals"]].head(2000)).to_excel(w, sheet_name="Авторы в сигналах", index=False)
        try:
            w.sheets["Как читать"].column_dimensions["A"].width = 120
            for sh in ("Шаблоны", "Общие хвосты"):
                ws = w.sheets[sh]
                ws.freeze_panes = "C2"
                ws.column_dimensions["A"].width = 14
        except Exception:
            pass

    summary = {
        "posts": len(m),
        "authors": int(m["author_key_final"].nunique()),
        "template_clusters": len(clusters),
        "template_clusters_not_song": int((~clusters["same_song_lyrics"]).sum()) if len(clusters) else 0,
        "posts_in_template_clusters": int(members["post_id_master"].nunique()) if len(members) else 0,
        "shared_tokens": len(tokens),
        "shared_tokens_by_type": tokens["token_type"].value_counts().to_dict() if len(tokens) else {},
        "authors_in_signals": int(af["in_coordination_signals"].sum()),
        "settings": {"jaccard_min": JACCARD_MIN, "min_authors": MIN_AUTHORS, "shingle_words": SHINGLE,
                     "burst_days": BURST_DAYS},
    }
    if len(clusters):
        summary["top_schools_in_clusters"] = clusters[~clusters["same_song_lyrics"]]["top_school"] \
            .replace("", "(без школы)").value_counts().head(15).to_dict()
    with (OUT_DIR / "07_CIB_SUMMARY.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    print("\n" + "=" * 70 + "\nDONE\n" + "=" * 70)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if len(clusters):
        show = ["cluster_id", "field", "posts", "authors", "top_school", "burst_share", "ad_disclosure_share", "example_text"]
        print("\nТОП-15 ШАБЛОНОВ:")
        t = clusters[~clusters["same_song_lyrics"]].head(15)[show].copy()
        t["example_text"] = t["example_text"].str[:70]
        print(t.to_string(index=False))
    if len(tokens):
        print("\nТОП-15 ОБЩИХ ХВОСТОВ:")
        print(tokens.head(15)[["token_type", "token", "authors", "posts", "top_school", "burst_share"]].to_string(index=False))
    print(f"\nГлавный файл для просмотра: {OUT_DIR / '06_CIB_REVIEW.xlsx'}")


if __name__ == "__main__":
    main()
