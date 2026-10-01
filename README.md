# teamproject — сбор TikTok-данных по онлайн-школам (Умскул)

Финальная версия кода на 01.10.2026. Основа — скрипты Рин (поиск → посты →
комментарии → реплаи; картинки + OCR). Из скрипта Маши (`scrapping2.py`)
перенесены поля для таблицы: **дата создания аккаунта автора**, статистика
автора и поста, субтитры — теперь они живут в `tiktok_fields.py` и
автоматически попадают в таблицу при каждом поиске.

## Файлы

| Файл | Что делает |
|---|---|
| `collect_tiktok_search_threads.py` | Поиск по запросам → дедуп постов → **таблица постов (xlsx/csv)** → комментарии → реплаи |
| `collect_tiktok_threads.py` | Комментарии + реплаи для конкретных URL (/video/ и /photo/) |
| `tiktok_fields.py` | Плоские поля поста: дата создания аккаунта (из ID автора), подписчики, лайки, просмотры, хэштеги, субтитры |
| `collect_tiktok_images.py` | Скачивание картинок из фото-постов/каруселей (+манифест, дедуп по SHA256) |
| `paddle_ocr_folder.py` | OCR сохранённых картинок через PaddleOCR (отдельное окружение, TikTok не трогает) |
| `TIKTOK_SEARCH_THREADS_README.txt` | Команды для поиска и комментариев |
| `TIKTOK_IMAGES_OCR_README.txt` | Команды для картинок и OCR |

Все скрипты держать в одной папке (`pytok_research`).

## Установка

```
pip install git+https://github.com/MEOMcGill/pytok.git@master
python -m camoufox fetch
pip install -r requirements.txt
python -m pytok.accounts.cli add      # добавить исследовательский аккаунт TikTok
python -m pytok.accounts.cli login
```

Проверка без обращения к TikTok:

```
python collect_tiktok_search_threads.py --check
```

## Типовой запуск

Только поиск + таблица (без комментариев):

```
python collect_tiktok_search_threads.py --query "умскул" --query "егэленд" --search-count 50 --search-only
```

То же + субтитры и дозапрос профилей авторов, если в поиске нет статистики:

```
python collect_tiktok_search_threads.py --queries-file queries.txt --search-count 50 --search-only --subtitles --fetch-author
```

Поиск + комментарии + реплаи:

```
python collect_tiktok_search_threads.py --query "умскул" --search-count 20 --comments 50
```

## Что получается на выходе

`cases/tiktok_search_threads/search/`
- `search_results_<run>.jsonl` — сырой слой: каждая выдача по каждому запросу, с рангом.
- `search_posts_<run>.json` — уникальные посты + все запросы/ранги, по которым они найдены, + сырой JSON TikTok.
- `search_posts_<run>.xlsx` и `.csv` — **таблица для анализа**, колонки:
  ID поста, тип (video/photo), ссылка, запросы, автор (ник, имя, ID),
  **дата создания аккаунта (UTC)**, био, число видео / подписчиков / лайков автора,
  время поста, описание, хэштеги, просмотры, лайки, комментарии, репосты,
  сохранения, длительность, звук, языки субтитров, текст субтитров.

`cases/tiktok_search_threads/raw/posts/<post_id>/`
- `comments_<run>.jsonl` — верхнеуровневые комментарии.
- `replies_<run>.jsonl` — реплаи (с `parent_comment_id`, `reply_to_reply_id`).
- `thread_<run>_summary.json` — сколько запрошено / собрано / полнота реплаев.

## Важно

- Дата создания аккаунта вычисляется из ID пользователя (старшие 32 бита —
  unix-время). Для очень старых аккаунтов (до 2014) поле пустое.
- Поиск TikTok — это механизм выдачи, не случайная и не полная выборка.
- Автоматических ретраев нет; задержки консервативные по умолчанию.
- Лицензия pytok — PolyForm Noncommercial.
