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
| `transcribe_whisper.py` | Речь из видео через faster-whisper (large-v3-turbo), по коду Маши; кэш `transcript.whisper.json`; вызывается из поиска автоматически (`--whisper missing/all/off`) |
| `ocr_vlm.py` | Расшифровка слайдов каруселей нейросетью через API (OpenRouter); список школ — `schools.txt`; вызывается из поиска автоматически |
| `export_comments_table.py` | **Таблица комментариев и реплаев** (xlsx/csv, одна строка = один комментарий, с привязкой к посту и родителю); вызывается автоматически в конце поиска |
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

## Конвейер целиком (одна команда)

**Финальная точка входа — `pipeline.py` (на Mac: двойной клик по `RUN.command`).**
Настройки (школа, файлы запросов/хэштегов, лимиты, Whisper, модель OCR) — в шапке `pipeline.py`
или аргументами:

```
python pipeline.py                     # полный прогон по настройкам из шапки
python pipeline.py --test              # пробный: 5 результатов на источник, 10 комментариев
python pipeline.py --final-only        # только пересобрать ИТОГ_<школа>.xlsx из уже собранного
python pipeline.py --school Фоксфорд --queries queries_foxford.txt --hashtags hashtags_foxford.txt
```

На выходе `ИТОГ_<школа>.xlsx` с листами **Посты** (дедуп по всем прогонам), **Комментарии и реплаи**, **Сводка**.
Под капотом `pipeline.py` вызывает:

```
python collect_tiktok_search_threads.py --queries-file queries.txt --hashtags-file hashtags.txt --search-count 200 --comments 200 --fetch-author
```

Шаги: поиск по запросам + ленты хэштегов → дедуп → таблица постов → для фото-постов
скачивание слайдов (`cases/tiktok_media/`) и расшифровка через API (ключ в `openrouter_key.txt`,
модель `--ocr-model`, по умолчанию `google/gemini-2.5-flash`) → комментарии и реплаи → таблица комментариев.
Для видео: `video.info()` → субтитры TikTok (если есть) → видео сохраняется в `cases/tiktok_media/raw/posts/<id>/video.mp4`
→ Whisper (`--whisper missing` — только где нет субтитров TikTok; `all`; `off`). Колонки «Субтитры», «Транскрипт (Whisper)»,
«Источник транскрипта». Whisper на CPU ≈ 0.3–0.5 длительности ролика: для тысяч роликов ставьте `--whisper off`
и запускайте `11_whisper.command` отдельно (например, ночью) — конвейер подхватит готовые транскрипты при сборке таблиц.
В таблице постов появляются колонки «Слайдов», «Текст со слайдов», «Школы на слайдах»,
«Контекст слайдов», «Сравнение школ на слайдах», «О чём слайды».
Для другой школы достаточно поменять `queries.txt` и `hashtags.txt`; `schools.txt` — общий список школ,
которые модель распознаёт на слайдах. Флаги `--no-images` / `--no-ocr` отключают эти шаги.

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
- `comments_<run>.xlsx` и `.csv` — **таблица комментариев и реплаев**: ID поста, уровень (1 — комментарий, 2 — ответ),
  ID комментария, ID родительского, автор (ник, имя, ID, дата создания аккаунта), время, текст, лайки,
  ответов по данным TikTok / собрано, лайк и закреп автора поста, язык, ссылка на комментарий.
- `search_posts_<run>.xlsx` и `.csv` — **таблица постов**, колонки:
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
