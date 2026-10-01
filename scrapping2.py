import asyncio
import json
import re
from datetime import datetime, timezone

import pandas as pd
import requests

from pytok.tiktok import PyTok
from pytok.accounts import AccountsPool

# ---------- НАСТРОЙКИ ----------
SEARCH_QUERY = "умскул"            # поисковый запрос
COUNT = 200                         # сколько видео собрать
PREFERRED_LANGS = ["rus", "eng"]   # какой язык субтитров предпочесть
FETCH_AUTHOR_IF_MISSING = True     # если данных об авторе нет в видео, запросить профиль автора

# столбцы Excel: "ключ": "название в таблице" (удали лишние или поменяй порядок)
COLUMNS = {
    "author_username": "Автор (ник)",
    "author_nickname": "Автор (имя)",
    "author_created": "Дата создания аккаунта (UTC)",
    "create_time": "Время создания видео (UTC)",
    "description": "Описание видео",
    "author_bio": "Описание аккаунта автора",
    "author_videos": "Количество видео автора",
    "author_followers": "Количество подписчиков",
    "author_likes": "Количество лайков автора в целом",
    "comment_count": "Комментарии под видео",
    "like_count": "Лайки под видео",
    "play_count": "Просмотры видео",
    "share_count": "Репосты видео",
    "duration_sec": "Длительность видео (сек)",
    "subtitle_text": "Субтитры",
    "video_url": "Ссылка на видео",
}
# --------------------------------


def g(d, *path, default=None):
    """Безопасно достаёт вложенное значение: g(d, 'stats', 'playCount')."""
    for key in path:
        if isinstance(d, dict) and key in d:
            d = d[key]
        else:
            return default
    return d


def find_key(obj, key):
    """Рекурсивно ищет первое значение по ключу в любой глубине данных."""
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
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def account_created_from_id(user_id):
    """Дата создания аккаунта, вычисленная из ID пользователя."""
    try:
        ts = int(user_id) >> 32
        if ts < 1_400_000_000:   # раньше 2014 года: ID старого формата, дата недостоверна
            return ""
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def get_stat(item, name):
    value = g(item, "stats", name)
    if value is None:
        value = g(item, "statsV2", name)
    return to_int(value)


def find_subtitle_entries(obj, found=None):
    """Рекурсивно ищет в данных записи о субтитрах (со ссылкой Url)."""
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


def download_subtitles(item):
    """Возвращает текст субтитров. Если субтитров нет, вернёт пустую строку."""
    entries = find_subtitle_entries(item)
    if not entries:
        return ""

    chosen = entries[0]
    for pref in PREFERRED_LANGS:
        match = [e for e in entries
                 if str(e.get("languagecodename", e.get("languagecode", ""))).lower().startswith(pref)]
        if match:
            chosen = match[0]
            break

    try:
        resp = requests.get(chosen["url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        return parse_subtitles(resp.text)[:32000]   # лимит ячейки Excel
    except Exception as e:
        print("  Не удалось скачать субтитры:", e)
        return ""


def extract_row(data):
    item = g(data, "itemInfo", "itemStruct", default=data)

    username = g(item, "author", "uniqueId", default="")
    video_id = g(item, "id", default="")
    author_stats = g(item, "authorStats", default={}) or {}

    ts = g(item, "createTime")
    try:
        created = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        created = ""

    likes = author_stats.get("heartCount", author_stats.get("heart"))

    row = {
        "author_username": username,
        "author_nickname": g(item, "author", "nickname", default=""),
        "author_created": account_created_from_id(g(item, "author", "id")),
        "create_time": created,
        "description": g(item, "desc", default=""),
        "author_bio": g(item, "author", "signature"),
        "author_videos": to_int(author_stats.get("videoCount")),
        "author_followers": to_int(author_stats.get("followerCount")),
        "author_likes": to_int(likes),
        "comment_count": get_stat(item, "commentCount"),
        "like_count": get_stat(item, "diggCount"),
        "play_count": get_stat(item, "playCount"),
        "share_count": get_stat(item, "shareCount"),
        "duration_sec": to_int(g(item, "video", "duration")),
        "video_url": f"https://www.tiktok.com/@{username}/video/{video_id}" if username else "",
    }
    return row, item


async def fill_author_info(api, row, cache):
    """Если данных об авторе нет в самом видео, запрашивает профиль автора."""
    fields = {
        "author_bio": "signature",
        "author_videos": "videoCount",
        "author_followers": "followerCount",
        "author_likes": "heartCount",
    }
    if not FETCH_AUTHOR_IF_MISSING or not row["author_username"]:
        return
    if all(row[k] is not None for k in fields):
        return

    username = row["author_username"]
    if username not in cache:
        try:
            cache[username] = await api.user(username=username).info()
            if len(cache) == 1:   # пример данных профиля для проверки
                with open("raw_user_sample.json", "w", encoding="utf-8") as f:
                    json.dump(cache[username], f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            print("  Не удалось получить профиль автора:", e)
            cache[username] = {}

    user_data = cache[username]
    for column, key in fields.items():
        if row[column] is None:
            value = find_key(user_data, key)
            if value is None and column == "author_likes":
                value = find_key(user_data, "heart")
            if value is not None:
                row[column] = to_int(value) if column != "author_bio" else value


async def main():
    pool = AccountsPool()
    rows = []
    author_cache = {}
    sample_saved = False

    try:
        async with await PyTok.from_pool(pool) as api:
            async for video in api.search(SEARCH_QUERY).videos(count=COUNT):
                data = await video.info()

                if not sample_saved:   # пример сырых данных видео для проверки
                    with open("raw_sample.json", "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                    sample_saved = True

                row, item = extract_row(data)
                await fill_author_info(api, row, author_cache)
                row["subtitle_text"] = await asyncio.to_thread(download_subtitles, item)

                rows.append(row)
                print("Собрано видео:", len(rows))
    finally:
        # сохраняем то, что успели собрать, даже если скрипт упал посередине
        if rows:
            df = pd.DataFrame(rows)
            df = df[[c for c in COLUMNS if c in df.columns]].rename(columns=COLUMNS)
            safe_query = re.sub(r"[^\w\-]+", "_", SEARCH_QUERY)
            filename = f"tiktok_search_{safe_query}.xlsx"
            df.to_excel(filename, index=False)
            print("Сохранено в файл:", filename)
        print("Итого видео:", len(rows))


if __name__ == "__main__":
    asyncio.run(main())