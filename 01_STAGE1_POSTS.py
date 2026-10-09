"""
STAGE 1 / ШАГ 01 — НОРМАЛИЗАЦИЯ ПОСТОВ (только сборка корпуса, без поиска школ).

Что делает:
  1. Читает файлы постов по ЯВНОЙ карте колонок (SOURCES): никакого угадывания по подстроке.
     Если в файле нет ожидаемой колонки — останавливается с понятной ошибкой.
  2. Хранит текст поста ПО ПОЛЯМ: описание, хэштеги, субтитры, речь (Whisper), текст на экране, текст слайдов.
     В match_text идут только эти поля. НЕ идут: описание профиля автора, «Whisper статус»,
     пересказы и ярлыки модели («О чём слайды», «Контекст слайдов», «Школы на слайдах» и т.п.) —
     они сохраняются отдельными колонками model_*.
  3. Чистит речь от галлюцинаций Whisper на музыке («Продолжение следует…», «Субтитры сделал…»).
  4. Склеивает дубли поста ПО ПОЛЯМ (для каждого поля — самое полное значение), а не целыми строками.
  5. Один ключ автора на весь проект: author_key_final = id:<author_id>, иначе u:<username>.
     ID восстанавливается по username, если тот же автор где-то встречался с ID. Ник (nickname) ключом не бывает.
  6. Отчёт: какие колонки взяты из каждого файла, покрытие полей, сколько постов отброшено и почему.

Поиск школ, сигналы и сводки — в 02_STAGE1_V4_BRANDS.py (он читает выход этого скрипта).

Выход (ANALYSIS_STAGE1/):
  01_posts_master_features.parquet   (если нет pyarrow — .csv)
  00_input_file_qc.csv               какие колонки взяты из каждого файла
  00_field_coverage.csv              сколько постов с каждым полем текста
  07_QC_SUMMARY.json                 итоговые цифры + сколько отброшено по датам/ID
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import unicodedata
from pathlib import Path

import pandas as pd

# ============================================================
# CONFIG
# ============================================================

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "Финальный датасет"
OUT_DIR = HERE / "ANALYSIS_STAGE1"

DATE_FROM = pd.Timestamp("2025-10-01")
DATE_TO = pd.Timestamp("2026-10-01")  # не включительно

MAX_MISSING_ID_RATE = 0.05

# Поля текста поста, в порядке сборки match_text.
TEXT_FIELDS = ["description", "hashtags", "subtitles", "transcript", "screen_text", "slides_text"]

# ЯВНАЯ карта колонок по файлам. Ключ — имя файла. Для каждого канонического поля —
# список допустимых названий колонки (берётся первое найденное).
# required: без этих полей скрипт остановится. Остальные поля — если колонки нет, будет предупреждение.
SOURCES = {
    "ШКОЛЫ_посты.csv": {
        "group": "targeted",
        "required": ["post_id", "create_time", "author_username", "author_id", "description", "transcript"],
        "columns": {
            "post_id": ["ID поста"],
            "create_time": ["Время создания поста (UTC)"],
            "author_username": ["Автор (ник)"],
            "author_nickname": ["Автор (имя)"],
            "author_id": ["ID автора"],
            "author_bio": ["Описание аккаунта автора"],
            "post_url": ["Ссылка на пост"],
            "post_type": ["Тип поста"],
            # текст поста
            "description": ["Описание поста"],
            "hashtags": ["Хэштеги"],
            "subtitles": ["Субтитры"],
            "transcript": ["Транскрипт (Whisper)"],
            "screen_text": ["Текст на экране (видео)"],
            "slides_text": ["Текст со слайдов"],
            # атрибуты (в текст не идут)
            "whisper_status": ["Whisper статус"],
            "model_slides_summary": ["О чём слайды"],
            "model_slides_context": ["Контекст слайдов"],
            "model_slides_schools": ["Школы на слайдах"],
            "model_slides_comparison": ["Сравнение школ на слайдах"],
            "model_screen_schools": ["Школы на экране (видео)"],
            "model_screen_promo": ["Промо на экране (видео)"],
            "views": ["Просмотры"],
            "likes": ["Лайки"],
            "comment_count": ["Комментарии"],
            "shares": ["Репосты"],
            "saves": ["Сохранения"],
            "duration_sec": ["Длительность видео (сек)"],
            "music_title": ["Звук"],
            "author_followers": ["Количество подписчиков"],
            "author_videos": ["Количество видео автора"],
            "author_created": ["Дата создания аккаунта (UTC)"],
            "plan_schools": ["Школы (план)"],
            "found_by": ["Запросы, по которым найден"],
            "dataset_part": ["Набор"],
        },
    },
    "ОБЩИЕ_ЕГЭ_посты.csv": {
        "group": "generic",
        "required": ["post_id", "create_time", "author_username", "author_id", "description", "transcript"],
        "columns": {
            "post_id": ["post_id"],
            "create_time": ["create_time"],
            "author_username": ["author_username"],
            "author_nickname": ["author_nickname"],
            "author_id": ["author_id"],
            "author_bio": ["author_bio", "author_signature"],
            "post_url": ["canonical_url", "video_url"],
            "post_type": ["post_type", "post_type_inferred"],
            "description": ["description"],
            "hashtags": ["hashtags"],
            "subtitles": ["subtitle_text"],
            "transcript": ["Транскрипт (Whisper)"],
            "screen_text": ["Текст на экране / слайдах"],
            "slides_text": ["slides_text"],
            "whisper_status": ["Whisper статус"],
            "model_slides_schools": ["slides_schools"],
            "model_screen_schools": ["Школы на экране"],
            "model_screen_promo": ["Промо на экране"],
            "views": ["play_count"],
            "likes": ["like_count"],
            "comment_count": ["comment_count"],
            "shares": ["share_count"],
            "saves": ["collect_count"],
            "duration_sec": ["duration_sec"],
            "music_title": ["music_title"],
            "author_followers": ["author_followers"],
            "author_videos": ["author_videos"],
            "author_created": ["author_created"],
            "found_by": ["matched_queries"],
        },
    },
}

NUMERIC_FIELDS = ["views", "likes", "comment_count", "shares", "saves", "duration_sec", "author_followers", "author_videos"]
META_FIELDS = ["create_time", "author_username", "author_nickname", "author_id", "author_bio", "post_url", "post_type",
               "whisper_status", "model_slides_summary", "model_slides_context", "model_slides_schools",
               "model_slides_comparison", "model_screen_schools", "model_screen_promo", "music_title",
               "author_created", "plan_schools", "found_by", "dataset_part"]

# Галлюцинации Whisper на музыке / тишине (вырезаются из речи и субтитров).
ASR_HALLUCINATIONS = re.compile(
    r"продолжение следует[.!…]*"
    r"|субтитры (?:сделал|создавал|подготовил|делал)\w*(?:\s+[\w.]+)?"
    r"|(?:редактор|репетитор) субтитров(?:\s+[\w.]+){0,2}"
    r"|корректор [а-я]\.\s?[а-я]+"
    r"|dimatorzok"
    r"|amara\.org[^.\n]{0,60}"
    r"|спасибо за просмотр\w*[.!]*"
    r"|thanks? (?:you )?for watching[.!]*",
    re.I,
)


# ============================================================
# HELPERS
# ============================================================

def clean_cell(x) -> str:
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    s = str(x).strip()
    return "" if s.lower() in {"nan", "none", "null"} else s


def normalize_light(x) -> str:
    """Для regex-поиска школ: нижний регистр, ё→е, пунктуация сохраняется."""
    s = clean_cell(x)
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower().replace("ё", "е").replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def normalize_features(x) -> str:
    s = normalize_light(x)
    if not s:
        return ""
    s = re.sub(r"[^\wа-яa-z0-9#@._]+", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def norm_col(x) -> str:
    return normalize_features(str(x).replace("﻿", "")).replace(" ", "_")


def normalize_post_id(x) -> str:
    s = clean_cell(x)
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s


def normalize_username(x) -> str:
    return clean_cell(x).lstrip("@").strip().lower()


ASR_WHOLE_NOISE = re.compile(r"^\W*(?:\[?(?:играет )?музыка\]?|\[?аплодисменты\]?|\[?смех\]?)\W*$", re.I)


def clean_asr(text: str) -> tuple[str, bool]:
    """Вырезает галлюцинации Whisper. Возвращает (текст, был_только_мусор).
    Текст без известных галлюцинаций не меняется вообще."""
    t = clean_cell(text)
    if not t:
        return "", False
    if ASR_WHOLE_NOISE.match(t):
        return "", True
    if not ASR_HALLUCINATIONS.search(t):
        return t, False
    cleaned = re.sub(r"\s+", " ", ASR_HALLUCINATIONS.sub(" ", t)).strip()
    if len(re.sub(r"[\W\d_]+", "", cleaned)) < 25:
        return "", True
    return cleaned, False


def to_number(x):
    s = clean_cell(x).replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")


# ============================================================
# DATES
# ============================================================

def parse_dates(series: pd.Series, post_ids: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Возвращает (дата, источник даты: column / post_id / '')."""
    raw = series.map(clean_cell)
    result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    source = pd.Series("", index=series.index, dtype="object")

    numeric_mask = raw.str.fullmatch(r"\d+(?:\.0+)?", na=False)
    text_mask = raw.ne("") & ~numeric_mask
    if text_mask.any():
        parsed = pd.to_datetime(raw[text_mask], errors="coerce", utc=True).dt.tz_localize(None)
        bad = parsed.isna()
        if bad.any():
            parsed[bad] = pd.to_datetime(raw[text_mask][bad], errors="coerce", dayfirst=True, utc=True).dt.tz_localize(None)
        result[text_mask] = parsed
    if numeric_mask.any():
        num = pd.to_numeric(raw[numeric_mask], errors="coerce")
        sec = num.between(1_400_000_000, 2_200_000_000)
        ms = num.between(1_400_000_000_000, 2_200_000_000_000)
        result[num.index[sec]] = pd.to_datetime(num[sec], unit="s", errors="coerce")
        result[num.index[ms]] = pd.to_datetime(num[ms], unit="ms", errors="coerce")
    source[result.notna()] = "column"

    # запасной путь: в ID поста TikTok зашито время публикации
    for idx in result.index[result.isna()]:
        pid = normalize_post_id(post_ids[idx])
        if re.fullmatch(r"\d{17,20}", pid):
            dt = pd.to_datetime(int(pid) >> 32, unit="s", errors="coerce")
            if pd.notna(dt) and pd.Timestamp("2016-01-01") <= dt <= pd.Timestamp("2030-12-31"):
                result[idx] = dt
                source[idx] = "post_id"
    return result, source


# ============================================================
# READING
# ============================================================

def read_csv_path(path: Path) -> pd.DataFrame:
    with path.open("rb") as f:
        head = f.read(128 * 1024)
    enc = "utf-8-sig"
    for e in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            head.decode(e)
            enc = e
            break
        except UnicodeDecodeError:
            pass
    try:
        sep = csv.Sniffer().sniff(head.decode(enc, errors="replace"), delimiters=",;\t|").delimiter
    except Exception:
        sep = ","
    print(f"    encoding={enc}, sep={sep!r}")
    df = pd.read_csv(path, sep=sep, dtype=str, encoding=enc, low_memory=False, on_bad_lines="warn")
    df.columns = [str(c).replace("﻿", "").strip() for c in df.columns]
    return df


def find_source_files() -> list[Path]:
    if not DATA_DIR.exists():
        raise FileNotFoundError(f"Не найдена папка: {DATA_DIR}")
    files, unknown = [], []
    for p in sorted(DATA_DIR.rglob("*.csv")):
        rel = p.relative_to(DATA_DIR)
        # резервные копии и служебные папки (_до_добавления_Маков и т.п.) не читаем
        if any(part.startswith("_") for part in rel.parts[:-1]):
            continue
        name = p.name.lower()
        if any(x in name for x in ("коммент", "comment", "reply")):
            continue
        if p.name in SOURCES:
            files.append(p)
        elif "пост" in name or "post" in name:
            unknown.append(str(rel))
    if unknown:
        print("\n!!! Найдены файлы постов, которых нет в SOURCES — НЕ читаю их:")
        for u in unknown:
            print("    ", u)
    names = [p.name for p in files]
    dups = {n for n in names if names.count(n) > 1}
    if dups:
        raise RuntimeError(f"Один и тот же файл найден в нескольких папках: {sorted(dups)}. Оставьте одну копию.")
    missing = [n for n in SOURCES if n not in names]
    if missing:
        raise RuntimeError(f"Нет ожидаемых файлов в {DATA_DIR.name}: {missing}")
    return files


# ============================================================
# STANDARDIZATION
# ============================================================

INPUT_QC: list[dict] = []


def resolve_columns(df: pd.DataFrame, filename: str) -> dict[str, str]:
    cfg = SOURCES[filename]
    by_norm = {norm_col(c): c for c in df.columns}
    chosen, missing = {}, []
    for field, names in cfg["columns"].items():
        col = next((by_norm[norm_col(n)] for n in names if norm_col(n) in by_norm), None)
        if col:
            chosen[field] = col
        else:
            missing.append(field)
    req_missing = [f for f in cfg["required"] if f not in chosen]
    if req_missing:
        raise RuntimeError(
            f"\n{filename}: нет обязательных колонок для полей {req_missing}.\n"
            f"Ожидались: { {f: cfg['columns'][f] for f in req_missing} }\n"
            f"Колонки файла: {list(df.columns)}")
    if missing:
        print(f"    (нет колонок для полей: {missing})")
    return chosen


def standardize_source(df: pd.DataFrame, filename: str) -> pd.DataFrame:
    cfg = SOURCES[filename]
    cols = resolve_columns(df, filename)
    print(f"\n  SOURCE: {filename} | rows {len(df):,} | group {cfg['group']}")
    for f in ["post_id", "create_time", "author_username", "author_id", "author_nickname", "author_bio"] + TEXT_FIELDS:
        print(f"    {f:16} <- {cols.get(f, '—')}")

    out = pd.DataFrame(index=df.index)
    for field in set(cfg["columns"]) | set(TEXT_FIELDS) | set(META_FIELDS) | set(NUMERIC_FIELDS):
        out[field] = df[cols[field]].map(clean_cell) if field in cols else ""

    out["post_id"] = out["post_id"].map(normalize_post_id)
    out["author_username"] = out["author_username"].map(normalize_username)
    out["author_id"] = out["author_id"].map(normalize_post_id)

    # речь и субтитры: вырезаем галлюцинации Whisper
    hall = pd.Series(False, index=out.index)
    for f in ("transcript", "subtitles"):
        res = out[f].map(clean_asr)
        out[f] = res.map(lambda r: r[0])
        hall |= res.map(lambda r: r[1])
    out["asr_hallucination_only"] = hall

    missing_id = int(out["post_id"].eq("").sum())
    rate = missing_id / len(out) if len(out) else 0
    print(f"    missing post_id: {missing_id:,} ({rate:.2%})")
    if rate > MAX_MISSING_ID_RATE:
        raise RuntimeError(f"Слишком много пустых post_id в {filename}")

    out["_source_file"] = filename
    out["_source_group"] = cfg["group"]
    out["_richness"] = sum(out[f].str.len() for f in TEXT_FIELDS)

    INPUT_QC.append({
        "file": filename, "rows": len(out), "source_group": cfg["group"],
        "missing_ids": missing_id, "missing_id_rate": round(rate, 4),
        **{f"col_{f}": cols.get(f, "") for f in cfg["columns"]},
        **{f"filled_{f}": int(out[f].ne("").sum()) for f in ["author_username", "author_id", "create_time"] + TEXT_FIELDS},
        "asr_hallucination_only": int(hall.sum()),
    })
    return out


# ============================================================
# MASTER (склейка дублей по полям)
# ============================================================

def longest(values) -> str:
    best = ""
    for v in values:
        v = clean_cell(v)
        if len(v) > len(best):
            best = v
    return best


def first_nonempty(values) -> str:
    for v in values:
        v = clean_cell(v)
        if v:
            return v
    return ""


def max_number(values):
    nums = [to_number(v) for v in values]
    nums = [n for n in nums if n == n]
    return max(nums) if nums else float("nan")


def build_match_text(row) -> str:
    parts, seen = [], set()
    for f in TEXT_FIELDS:
        v = normalize_light(row[f])
        if v and v not in seen:
            seen.add(v)
            parts.append(v)
    return "\n".join(parts)


def build_master(frames: list[pd.DataFrame]) -> tuple[pd.DataFrame, dict]:
    all_df = pd.concat(frames, ignore_index=True, sort=False)
    stats = {"rows_before_dedup": len(all_df), "rows_without_post_id": int(all_df["post_id"].eq("").sum())}
    all_df = all_df[all_df["post_id"].ne("")]
    # самая полная строка первой: для полей «первое непустое» берётся из неё
    all_df = all_df.sort_values(["post_id", "_richness"], ascending=[True, False])

    agg = {f: longest for f in TEXT_FIELDS}
    agg.update({f: first_nonempty for f in META_FIELDS})
    agg.update({f: max_number for f in NUMERIC_FIELDS})
    agg["asr_hallucination_only"] = "max"
    agg["_source_file"] = lambda x: ";".join(sorted(set(x)))
    agg["_source_group"] = lambda x: ";".join(sorted(set(x)))
    master = all_df.groupby("post_id", sort=False).agg(agg).reset_index()
    master["source_occurrences"] = master["post_id"].map(all_df.groupby("post_id").size())
    master = master.rename(columns={"post_id": "post_id_master", "_source_file": "source_files",
                                    "_source_group": "source_groups", "create_time": "create_time_raw"})
    master["found_in_generic"] = master["source_groups"].str.split(";").map(lambda s: "generic" in s)
    master["found_in_targeted"] = master["source_groups"].str.split(";").map(lambda s: "targeted" in s)
    stats["unique_posts"] = len(master)
    print(f"\nСтрок до склейки: {stats['rows_before_dedup']:,} -> уникальных постов: {len(master):,}")

    # текст для поиска школ: только поля поста
    master["match_text"] = master.apply(build_match_text, axis=1)
    master["normalized_text"] = master["match_text"].map(normalize_features)
    master["text_fields_present"] = master.apply(lambda r: ";".join(f for f in TEXT_FIELDS if r[f]), axis=1)

    # даты
    master["create_time_parsed"], master["date_source"] = parse_dates(master["create_time_raw"], master["post_id_master"])
    no_date = master["create_time_parsed"].isna()
    before = master["create_time_parsed"] < DATE_FROM
    after = master["create_time_parsed"] >= DATE_TO
    stats.update({"dropped_no_date": int(no_date.sum()), "dropped_before_period": int(before.sum()),
                  "dropped_after_period": int(after.sum()),
                  "date_from_column": int(master["date_source"].eq("column").sum()),
                  "date_from_post_id": int(master["date_source"].eq("post_id").sum())})
    dropped = master[no_date | before | after]
    dropped[["post_id_master", "create_time_raw", "create_time_parsed", "source_files"]].to_csv(
        OUT_DIR / "00_dropped_by_period.csv", index=False, encoding="utf-8-sig")
    master = master[~(no_date | before | after)].copy()
    print(f"Период [{DATE_FROM.date()}, {DATE_TO.date()}): без даты {stats['dropped_no_date']:,}, "
          f"раньше {stats['dropped_before_period']:,}, позже {stats['dropped_after_period']:,} -> осталось {len(master):,}")
    stats["posts_in_period"] = len(master)
    return master, stats


# ============================================================
# AUTHORS: один ключ на весь проект
# ============================================================

def build_authors(master: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    pairs = master.loc[master["author_id"].ne("") & master["author_username"].ne(""), ["author_id", "author_username"]]
    cnt = pairs.groupby(["author_id", "author_username"]).size().reset_index(name="n")
    # для каждого username — самый частый id; для каждого id — самый частый username
    u2id = cnt.sort_values("n", ascending=False).drop_duplicates("author_username").set_index("author_username")["author_id"]
    id2u = cnt.sort_values("n", ascending=False).drop_duplicates("author_id").set_index("author_id")["author_username"]
    usernames_per_id = cnt.groupby("author_id")["author_username"].apply(lambda s: ";".join(sorted(set(s))))

    fill_id = master["author_id"].eq("") & master["author_username"].isin(u2id.index)
    master.loc[fill_id, "author_id"] = master.loc[fill_id, "author_username"].map(u2id)
    fill_u = master["author_username"].eq("") & master["author_id"].isin(id2u.index)
    master.loc[fill_u, "author_username"] = master.loc[fill_u, "author_id"].map(id2u)

    master["author_key_final"] = ""
    has_id = master["author_id"].ne("")
    master.loc[has_id, "author_key_final"] = "id:" + master.loc[has_id, "author_id"]
    only_u = ~has_id & master["author_username"].ne("")
    master.loc[only_u, "author_key_final"] = "u:" + master.loc[only_u, "author_username"]
    master["author_usernames_seen"] = master["author_id"].map(usernames_per_id).fillna(master["author_username"])
    # совместимость со старыми скриптами
    master["author_key"] = master["author_key_final"]

    stats = {"author_id_filled_from_username": int(fill_id.sum()),
             "username_filled_from_author_id": int(fill_u.sum()),
             "posts_with_author_key": int(master["author_key_final"].ne("").sum()),
             "posts_without_author": int(master["author_key_final"].eq("").sum()),
             "unique_authors": int(master.loc[master["author_key_final"].ne(""), "author_key_final"].nunique()),
             "author_ids_with_several_usernames": int((usernames_per_id.str.count(";") > 0).sum())}
    print(f"\nАвторы: ID восстановлен по нику у {stats['author_id_filled_from_username']:,} постов, "
          f"ник по ID — у {stats['username_filled_from_author_id']:,}; без автора {stats['posts_without_author']:,}; "
          f"уникальных авторов {stats['unique_authors']:,}")
    return master, stats


# ============================================================
# MAIN
# ============================================================

def main():
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)
    print("=" * 70 + "\nSTAGE 1 / 01 — НОРМАЛИЗАЦИЯ ПОСТОВ (без поиска школ)\n" + "=" * 70)

    frames = []
    for path in find_source_files():
        print(f"\nЧитаю: {path.relative_to(DATA_DIR)}")
        frames.append(standardize_source(read_csv_path(path), path.name))
    pd.DataFrame(INPUT_QC).to_csv(OUT_DIR / "00_input_file_qc.csv", index=False, encoding="utf-8-sig")

    master, stats = build_master(frames)
    master, author_stats = build_authors(master)

    coverage = pd.DataFrame([{
        "field": f, "posts_with_field": int(master[f].ne("").sum()),
        "share": round(float(master[f].ne("").mean()), 4),
        "in_match_text": f in TEXT_FIELDS,
    } for f in TEXT_FIELDS + ["author_bio", "whisper_status", "model_slides_summary", "model_slides_context"]])
    coverage.to_csv(OUT_DIR / "00_field_coverage.csv", index=False, encoding="utf-8-sig")

    try:
        master.to_parquet(OUT_DIR / "01_posts_master_features.parquet", index=False)
        out_name = "01_posts_master_features.parquet"
    except Exception as e:
        print("Parquet не записан:", e, "-> CSV")
        master.to_csv(OUT_DIR / "01_posts_master_features.csv", index=False, encoding="utf-8-sig")
        out_name = "01_posts_master_features.csv"

    qc = {**stats, **author_stats,
          "generic_posts": int(master["found_in_generic"].sum()),
          "targeted_posts": int(master["found_in_targeted"].sum()),
          "both_generic_and_targeted": int((master["found_in_generic"] & master["found_in_targeted"]).sum()),
          "posts_with_asr_hallucination_removed": int(master["asr_hallucination_only"].sum()),
          "posts_with_empty_match_text": int(master["match_text"].eq("").sum()),
          "text_fields_in_match_text": TEXT_FIELDS,
          "output": out_name}
    with (OUT_DIR / "07_QC_SUMMARY.json").open("w", encoding="utf-8") as f:
        json.dump(qc, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70 + "\nDONE\n" + "=" * 70)
    print(json.dumps(qc, ensure_ascii=False, indent=2))
    print("\nПокрытие полей:\n" + coverage.to_string(index=False))
    print("\nOUTPUT:")
    for p in sorted(OUT_DIR.iterdir()):
        print("  ", p.name)


if __name__ == "__main__":
    main()
