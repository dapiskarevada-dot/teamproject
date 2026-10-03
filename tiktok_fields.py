#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Плоские поля поста TikTok из сырого словаря, который возвращает PyTok.

Модуль собран из scrapping2.py (Маша): дата создания аккаунта автора,
статистика автора и видео, субтитры. Используется из
collect_tiktok_search_threads.py, чтобы рядом с JSON-слоем поиска сразу
получать таблицу (XLSX/CSV) с понятными колонками.

Ничего не запрашивает у TikTok: работает только с данными, которые уже
пришли (поиск или video.info()). Единственное исключение — download_subtitles,
который скачивает файл субтитров с CDN TikTok обычным HTTP-запросом.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

PREFERRED_SUBTITLE_LANGS = ["rus", "eng"]

# Колонки таблицы: "ключ": "название в таблице". Порядок = порядок колонок.
COLUMNS = {
    "post_id": "ID поста",
    "post_type": "Тип поста",
    "video_url": "Ссылка на пост",
    "matched_queries": "Запросы, по которым найден",
    "author_username": "Автор (ник)",
    "author_nickname": "Автор (имя)",
    "author_id": "ID автора",
    "author_created": "Дата создания аккаунта (UTC)",
    "author_bio": "Описание аккаунта автора",
    "author_videos": "Количество видео автора",
    "author_followers": "Количество подписчиков",
    "author_likes": "Количество лайков автора в целом",
    "create_time": "Время создания поста (UTC)",
    "description": "Описание поста",
    "hashtags": "Хэштеги",
    "play_count": "Просмотры",
    "like_count": "Лайки",
    "comment_count": "Комментарии",
    "share_count": "Репосты",
    "collect_count": "Сохранения",
    "duration_sec": "Длительность видео (сек)",
    "music_title": "Звук",
    "subtitle_langs": "Языки субтитров",
    "subtitle_text": "Субтитры",
    # заполняются конвейером для видео: речь (Whisper) — см. transcribe_whisper.py
    "transcript_whisper": "Транскрипт (Whisper)",
    "transcript_source": "Источник транскрипта",
    "video_file": "Файл видео",
    # заполняются конвейером для фото-постов (каруселей) после расшифровки слайдов
    "slides_count": "Слайдов",
    "slides_text": "Текст со слайдов",
    "slides_schools": "Школы на слайдах",
    "slides_context": "Контекст слайдов",
    "slides_comparison": "Сравнение школ на слайдах",
    "slides_summary": "О чём слайды",
    "screen_text": "Текст на экране (видео)",
    "screen_schools": "Школы на экране (видео)",
    "screen_promo": "Промо на экране (видео)",
    "in_sample": "В выборке (глубина)",
    "schools_mentioned": "Школы упомянуты (все поля)",
}


def g(d, *path, default=None):
    """Безопасно достаёт вложенное значение: g(d, 'stats', 'playCount')."""
    for key in path:
        if isinstance(d, dict) and key in d:
            d = d[key]
        else:
            return default
    return d


def find_key(obj, key):
    """Рекурсивно ищет первое значение по ключу на любой глубине."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def ts_to_utc(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def account_created_from_id(user_id):
    """
    Дата создания аккаунта, вычисленная из ID пользователя
    (старшие 32 бита snowflake-ID — unix-время регистрации).
    Для ID старого формата (до ~2014) возвращает пустую строку.
    """
    try:
        ts = int(user_id) >> 32
        if ts < 1_400_000_000:
            return ""
        return ts_to_utc(ts)
    except (TypeError, ValueError):
        return ""


def unwrap_item(data):
    """video.info() возвращает {"itemInfo": {"itemStruct": {...}}}, поиск — сам itemStruct."""
    if not isinstance(data, dict):
        return {}
    return g(data, "itemInfo", "itemStruct", default=data) or {}


def get_stat(item, name):
    value = g(item, "stats", name)
    if value is None:
        value = g(item, "statsV2", name)
    return to_int(value)


def extract_hashtags(item):
    tags = []
    for extra in item.get("textExtra") or []:
        if isinstance(extra, dict) and extra.get("hashtagName"):
            tags.append("#" + str(extra["hashtagName"]))
    if not tags:
        for ch in item.get("challenges") or []:
            if isinstance(ch, dict) and ch.get("title"):
                tags.append("#" + str(ch["title"]))
    return " ".join(dict.fromkeys(tags))


def find_subtitle_entries(obj, found=None):
    """Рекурсивно ищет записи о субтитрах (объекты с Url + языком/форматом)."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        lowered = {str(k).lower(): v for k, v in obj.items()}
        if "url" in lowered and any(
            k in lowered for k in ("languagecodename", "languagecode", "format")
        ):
            found.append(lowered)
        for v in obj.values():
            find_subtitle_entries(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_subtitle_entries(v, found)
    return found


def subtitle_langs(item):
    langs = []
    for e in find_subtitle_entries(item):
        lang = e.get("languagecodename") or e.get("languagecode")
        if lang:
            langs.append(str(lang))
    return ",".join(dict.fromkeys(langs))


def parse_subtitles(raw):
    """Превращает файл субтитров (WebVTT или JSON) в обычный текст."""
    raw = raw.strip()
    if raw.startswith("WEBVTT"):
        lines = []
        for line in raw.splitlines():
            line = line.strip()
            if (not line or line.startswith(("WEBVTT", "NOTE", "STYLE"))
                    or "-->" in line or line.isdigit()):
                continue
            lines.append(line)
        return " ".join(lines)
    try:
        data = json.loads(raw)
        return " ".join(u.get("text", "") for u in data.get("utterances", []))
    except Exception:
        return ""


def download_subtitles(item, preferred_langs=None, timeout=30):
    """
    Текст авто-субтитров TikTok (пустая строка, если их нет).
    Это HTTP-запрос к CDN TikTok, не к странице поста.
    """
    import requests

    preferred_langs = preferred_langs or PREFERRED_SUBTITLE_LANGS
    entries = find_subtitle_entries(item)
    if not entries:
        return ""

    chosen = entries[0]
    for pref in preferred_langs:
        match = [
            e for e in entries
            if str(e.get("languagecodename", e.get("languagecode", ""))).lower().startswith(pref)
        ]
        if match:
            chosen = match[0]
            break

    try:
        resp = requests.get(chosen["url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        resp.raise_for_status()
        return parse_subtitles(resp.text)[:32000]  # лимит ячейки Excel
    except Exception as exc:
        print("  Не удалось скачать субтитры:", exc)
        return ""


def flatten_post(data, post_type=None):
    """
    Плоский словарь полей поста из сырых данных PyTok
    (результат поиска или video.info()).
    """
    item = unwrap_item(data)

    username = g(item, "author", "uniqueId", default="") or ""
    author_id = g(item, "author", "id")
    post_id = str(g(item, "id", default="") or "")
    author_stats = g(item, "authorStats", default={}) or {}
    likes = author_stats.get("heartCount", author_stats.get("heart"))

    if post_type is None:
        post_type = "photo" if (item.get("imagePost") or item.get("image_post")) else "video"

    return {
        "post_id": post_id,
        "post_type": post_type,
        "video_url": f"https://www.tiktok.com/@{username}/{post_type}/{post_id}" if username and post_id else "",
        "author_username": username,
        "author_nickname": g(item, "author", "nickname", default=""),
        "author_id": str(author_id) if author_id is not None else "",
        "author_created": account_created_from_id(author_id),
        "author_bio": g(item, "author", "signature"),
        "author_videos": to_int(author_stats.get("videoCount")),
        "author_followers": to_int(author_stats.get("followerCount")),
        "author_likes": to_int(likes),
        "create_time": ts_to_utc(g(item, "createTime")),
        "description": g(item, "desc", default=""),
        "hashtags": extract_hashtags(item),
        "play_count": get_stat(item, "playCount"),
        "like_count": get_stat(item, "diggCount"),
        "comment_count": get_stat(item, "commentCount"),
        "share_count": get_stat(item, "shareCount"),
        "collect_count": get_stat(item, "collectCount"),
        "duration_sec": to_int(g(item, "video", "duration")),
        "music_title": g(item, "music", "title", default=""),
        "subtitle_langs": subtitle_langs(item),
        "subtitle_text": "",
    }


AUTHOR_FIELDS = {
    "author_bio": "signature",
    "author_videos": "videoCount",
    "author_followers": "followerCount",
    "author_likes": "heartCount",
}


def fill_author_from_profile(row, user_data):
    """Дополняет пустые поля автора данными профиля (api.user(...).info())."""
    for column, key in AUTHOR_FIELDS.items():
        if row.get(column) is None or row.get(column) == "":
            value = find_key(user_data, key)
            if value is None and column == "author_likes":
                value = find_key(user_data, "heart")
            if value is not None:
                row[column] = to_int(value) if column != "author_bio" else value
    if not row.get("author_created"):
        uid = find_key(user_data, "id")
        row["author_created"] = account_created_from_id(uid)


def author_fields_missing(row):
    return any(row.get(c) is None for c in AUTHOR_FIELDS)


def rows_to_table(rows, path_base):
    """
    Сохраняет список словарей в <path_base>.xlsx (если есть pandas+openpyxl)
    и всегда в <path_base>.csv. Возвращает список записанных путей.
    """
    import csv
    from pathlib import Path

    path_base = Path(path_base)
    keys = [k for k in COLUMNS if any(k in r for r in rows)] or list(COLUMNS)
    written = []

    csv_path = path_base.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS[k] for k in keys)
        for r in rows:
            w.writerow(_cell(r.get(k)) for k in keys)
    written.append(csv_path)

    try:
        import pandas as pd
        df = pd.DataFrame([{COLUMNS[k]: _cell(r.get(k)) for k in keys} for r in rows])
        xlsx_path = path_base.with_suffix(".xlsx")
        df.to_excel(xlsx_path, index=False)
        written.append(xlsx_path)
    except Exception as exc:
        print("XLSX не записан (нужны pandas и openpyxl):", exc)

    return written


def _cell(v):
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return v
