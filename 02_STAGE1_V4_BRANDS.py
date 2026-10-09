"""
STAGE 1 / ШАГ 02 — ПОИСК ШКОЛ В ПОСТАХ (бренд-слой) + дешёвые сигналы.

Читает выход 01_STAGE1_POSTS.py (ANALYSIS_STAGE1/01_posts_master_features.parquet) и словарь НАПИСАНИЯ_ШКОЛ.xlsx.

Главные правила:
  * Тип строки словаря определяется по ЯРЛЫКУ («бренд», «искажение речи», «с контекстом», «в перечне школ»,
    «не школа», «аккаунт препода»). «не школа» = исключение. Неизвестный ярлык -> остановка. Номера строк Excel не используются.
  * Правки словаря, найденные на QC, лежат в коде (RULE_DISABLE / RULE_ADD) — по (школа, шаблон), а не по номеру строки.
  * Поиск идёт ПО ПОЛЯМ поста (описание, хэштеги, субтитры, речь, экран, слайды); у каждого совпадения записано поле.
  * exact  — однозначное название; отменяется только исключением, которое ПЕРЕСЕКАЕТСЯ с самим совпадением.
    asr    — искажение речи; отменяется исключением рядом (±40 символов).
    context — многозначное слово; засчитывается, если в ±60 символах есть признак ИМЕННО школы
             (онлайн-школа, ош, курс, вебинар, промокод, тгк, «учусь в», «перешла из»…) или рядом другая школа.
             «егэ», «огэ», «школа», «готовлюсь» сами по себе признаком не считаются (они есть почти везде).
    list   — засчитывается, только если в ±80 символах стоит другая однозначная школа (перечень, рейтинг).
  * Глобальные антипримеры (школа «-») гасят пересекающиеся с ними совпадения.
  * Ключ автора — author_key_final из шага 01 (здесь не пересчитывается).
  * Перед прогоном — регрессионные тесты (00_regression_tests.csv).

Выход (ANALYSIS_STAGE1_V4/):
  01_posts_master_features_v4.parquet  — посты + школы + доказательства (school_evidence_json, school_rejected_json)
  00_dictionary_rules_v4.csv           — итоговые правила (что из словаря, что добавлено/отключено в коде)
  00_regression_tests.csv              — результаты тестов
  02_school_baseline_v4.csv, 03_school_comentions_v4.csv, 04_author_school_v4.csv,
  05_signal_baseline_v4.csv, 05b_school_by_field_kind_v4.csv, 06_posts_for_review_v4.xlsx, 07_QC_SUMMARY_V4.json
"""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import pandas as pd

# ============================================================
# CONFIG
# ============================================================

HERE = Path(__file__).resolve().parent
MASTER_PATH = HERE / "ANALYSIS_STAGE1" / "01_posts_master_features.parquet"
DICT_PATH = HERE / "НАПИСАНИЯ_ШКОЛ.xlsx"
OUT_DIR = HERE / "ANALYSIS_STAGE1_V4"

TEXT_FIELDS = ["description", "hashtags", "subtitles", "transcript", "screen_text", "slides_text"]

REJECT_WINDOW = 40     # исключение «рядом» для asr/context/list
CUE_WINDOW = 60        # признак школы рядом для context
LIST_WINDOW = 80       # другая школа рядом для list
SNIPPET = 150          # окно для QC

GLOBAL_NOISE_SCHOOL = "-"

# ------------------------------------------------------------
# Правки словаря по итогам QC. Ключ — (школа, шаблон ровно как в Excel).
# ------------------------------------------------------------
RULE_DISABLE = {
    ("MAXIMUM Education", r"(?:онлайн[- ]?)?школ\w* максимум"): "«в школе максимум 5 уроков»",
    ("MAXIMUM Education", r"максимум ?изи|maximum ?(?:изи|easy)"): "«это максимум изи»",
    ("MAXIMUM Education", r"#maximum(?![a-z])"): "заменено на list ниже",
    ("MAXIMUM Education", r"максимум(?![а-я])"): "заменено на list ниже",
    ("Сотка", r"sotka(?:tv|ege|school|online|_|(?![a-z]))"): "sotkatv — Twitch-стример",
    ("Сотка", r"школ[аеуы]? сотк"): "«после школы сотку набрала»",
    ("Сотка", r"#сотка(?:_на_егэ|егэ|_егэ)"): "#сотка_на_егэ — хэштег Турбо-литры",
    ("Сотка", r"(?:на|за|в) сотку|сотку (?:балл|по|на)|сотк[аиу] (?:балл|по|на|из)|получил\w* сотку|набрал\w* сотку|сотк[ау] на егэ|(?:забрал|взял|сдал|получил|набрал|дадут)\w* (?:\w+ )?сотк|\d+ сотк|сотк[аиу] (?:\d|на \w+ буд)"):
        "заменено: «\\d+ сотк» убивало «100 сотка» в рейтингах",
    ("Турбо ЕГЭ", r"турболитр"): "заменено на турбо[_ ]?литр",
    ("Турбо ЕГЭ", r"турбо(?![а-я])"): "заменено на context со своими признаками",
    ("Lomonosov School", r"lomonosov ?school"): "#lomonosovschool — хэштег обычной школы им. Ломоносова",
    ("Lomonosov School", r"ломоносов(?![а-я])"): "заменено на list",
    ("99 Баллов", r"#?99баллов"): "#99баллов бывает и результатом — заменено на context",
    ("Пифагор", r"пифагор"): "заменено на context со строгими признаками",
    ("Adrenaline", r"adrenaline"): "заменено: exact только adrenaline_chem/school",
    ("99 Баллов", r"(?:на|набрал\w*|получил\w*|сдал\w*|из) 99 баллов|99 баллов (?:из|по|на)"):
        "заменено: «99 баллов по» убивало «в онлайн-школе 99 баллов по подготовке»",
    ("Skysmart", r"skymars"): "это «не школа» — уходит в исключения автоматически",
}

RULE_ADD = [
    # MAXIMUM
    ("MAXIMUM Education", r"онлайн[- ]?школ\w* (?:«|\")?максимум", "exact", "онлайн-школа Максимум"),
    ("MAXIMUM Education", r"#максимумизи|maximum ?easy|максимум ?изи(?=\s*#|$)", "exact", "продукт Максимум изи"),
    ("MAXIMUM Education", r"максимум(?![а-я])", "list", "только в перечне рядом с другой школой"),
    ("MAXIMUM Education", r"#maximum(?![a-z])", "list", "часто витебский детский центр"),
    ("MAXIMUM Education", r"по максимуму|на максимум\w*|максимум \d|максимум балл|максимальн\w*|максимум (?:за|на|по|час|минут|пар|полтор|два|три)|в максимум(?!е)|роскошн\w* ?максимум",
     "reject", "обычное слово"),
    # Сотка
    ("Сотка", r"sotka(?:ege|school|online)|sotka_(?!tv)", "exact", "аккаунты школы, без sotkatv"),
    ("Сотка", r"sotka(?![a-z])", "context", "голое sotka"),
    ("Сотка", r"школ[аеуы]? «?сотк(?:а|е|и|ой)(?![а-я])", "exact", "«школа Сотка», не «школы сотку»"),
    ("Сотка", r"#сотка(?:егэ|_егэ)", "exact", "хэштеги школы"),
    ("Сотка", r"#сотка_на_егэ", "reject", "хэштег-цель Турбо-литры, не школа"),
    ("Сотка", r"(?:на|за|в) сотку|сотку (?:балл|по|на)|сотк[аиу] (?:балл|по|на|из|за)|получ\w* сотк|набра\w* сотк|сотк[ау] на егэ|(?:забрал|взял|сдал|дадут)\w* (?:\w+ )?сотк|\d+ сотк(?:у|и)|сотк[аиу] (?:\d+(?![\d)])|на \w+ буд)|сотк\w* в кармане|до сотки",
     "reject", "100 баллов"),
    ("Сотка", r"sotkatv|sotkameow", "reject", "стримеры"),
    # Турбо
    ("Турбо ЕГЭ", r"турбо[_ ]?литр", "exact", "Турбо-литра"),
    ("Турбо ЕГЭ", r"турбо(?![а-я])", "context", "голое турбо: нужен признак курса/канала/экзамена"),
    ("Турбо ЕГЭ", r"слово ?пацана|турбословопацана|туркин|копейкин|dota|energy|энергетик|турбо[ -]?си(?![а-я])|турбо-репетитор|турбо[ -]?режим|турбин|kirgiz",
     "reject", "не школа"),
    # Lomonosov
    ("Lomonosov School", r"lomonosov ?school", "context", "нужен признак онлайн-школы/ЕГЭ"),
    ("Lomonosov School", r"ломоносов(?![а-я])", "list", "только в перечне школ"),
    ("Lomonosov School", r"олимпиад\w*|мгу|михаил|м\.\s?в\.|имени|им\.|атындағы|мектеб\w*|мектеп\w*|орта|поэт\w*|учен\w*|17\d\d|18 ?век|университет\w*|#\d+[а-я]|класс\w*",
     "reject", "МГУ / учёный / обычная школа"),
    # 99 Баллов
    ("99 Баллов", r"#?99баллов", "context", "хэштег школы или результат"),
    ("99 Баллов", r"(?:онлайн[- ]?школ\w*|(?<![а-я])ош|занима\w* в|учусь в|уроки в|урок в|вебы? в|вебинар\w* в|тгк:?) «?99 баллов",
     "exact", "явно школа"),
    ("99 Баллов", r"(?:на|из|от|всего|в сумме|набра\w*|получ\w*|сдал\w*|сдам) (?:\w+ ){0,2}99 баллов|99 баллов (?:из|на|за)(?![а-я])|99 баллов по (?!подготов)|пересда\w*",
     "reject", "результат экзамена"),
    # 100балльный
    ("100балльный репетитор", r"100 ?-?бал+ьн\w* шкал\w*|(?:сто|сту|100 ?-?)бал+ьниц\w*|(?:сто|сту|100 ?-?)бал+ьн\w* (?:работ|недел|ответ|препод|преподавател|эксперт|куратор|ученик|выпускник|результат|балл)\w*|бал+ьн\w* систем\w*",
     "reject", "шкала / результат"),
    # Пифагор
    ("Пифагор", r"пифагор\w*", "context", "строгие признаки (STRICT_CUES)"),
    ("Пифагор", r"теорем\w*|катет\w*|гипотенуз\w*|потеря\w* пифагор|по пифагору|пифагоров\w*|тройк\w*|воскрес\w*|штаны",
     "reject", "геометрия"),
    # Adrenaline
    ("Adrenaline", r"adrenaline[_.]?(?:school|chem)", "exact", "аккаунты школы"),
    ("Adrenaline", r"adrenaline(?![_.]?(?:school|chem))", "context", "голое adrenaline"),
    ("Adrenaline", r"rush|энергетик\w*|напит\w*|гормон\w*|надпочечник\w*|кортизол\w*|выброс\w*|заряд\w*|адреналинов\w*|дофамин\w*",
     "reject", "энергетик / гормон"),
    # глобальный шум: «оксфорд» — подстрока «фоксфорд»
    (GLOBAL_NOISE_SCHOOL, r"(?<!ф)оксфорд", "global_reject", "вместо «оксфорд»"),
]
RULE_DISABLE[(GLOBAL_NOISE_SCHOOL, "оксфорд")] = "заменено на (?<!ф)оксфорд"

# Признак именно ШКОЛЫ рядом с многозначным словом.
GENERIC_CUE = re.compile(
    r"онлайн[- ]?школ\w*|(?<![а-яa-z])ош(?![а-я])|курс\w*|вебинар\w*|куратор\w*|препод\w*|тариф\w*|промокод\w*|промик\w*"
    r"|(?<![а-я])тгк(?![а-я])|телеграм\w*|платформ\w*|отзыв\w*|скидк\w*"
    r"|(?<![а-я])(?:учусь|училась|учился|занимаюсь|занималась|занимался|готовлюсь|готовилась|готовился|перешл\w*|перешел|ушл\w*|ушел)\s+(?:в|из)\s",
    re.I)
# Дополнительные признаки для отдельных школ (добавляются к общим).
EXTRA_CUES = {
    "Lomonosov School": re.compile(r"(?<![а-я])(?:егэ|огэ)(?![а-я])|онлайн", re.I),
    "99 Баллов": re.compile(r"веб\w*|урок\w*|ош(?![а-я])", re.I),
    "Турбо ЕГЭ": re.compile(r"#(?:егэ|огэ|информатик|истори|литератур|обществ)\w*|(?<![а-я])цт(?![а-я])|канал\w*", re.I),
}
# Строгие признаки: для этих школ общие признаки НЕ используются.
STRICT_CUES = {
    "Пифагор": re.compile(r"школ\w*\s+(?:«)?пифагор|у\s+пифагора|курс\w*[^.!?]{0,25}пифагор|пифагор\w*[^.!?]{0,25}(?:курс|школ|ош(?![а-я])|\d+/10)"
                          r"|онлайн[- ]?школ\w*[^.!?]{0,40}пифагор", re.I),
    "Морозилка": re.compile(r"онлайн[- ]?школ\w*|(?<![а-я])ош(?![а-я])|курс\w*|вебинар\w*|(?<![а-я])тгк(?![а-я])", re.I),
}

LB = r"(?<![а-яa-z0-9])"   # левая граница слова для context/list/asr

# «Рядом другая школа» = перечень: между ними не больше LIST_GAP_MAX символов и только разделители
# (запятые, «и», «или», «vs», номера, хэштеги). «умскуле, это максимум» — не перечень.
LIST_GAP_MAX = 40
LIST_GAP = re.compile(r"^[\s,;:/|•·\-–—)(\d.!?#*+]*(?:(?:и|или|либо|vs|а также)(?![а-яa-z])[\s,;:/|•·\-–—)(\d.!?#*+]*)?$", re.I)

# «учусь в …», «в онлайн-школе …» прямо перед словом: тогда исключение должно задевать само слово,
# соседнее «набрала сотку» уже не отменяет «готовилась в онлайн-школе в сотке».
STRONG_PREFIX = re.compile(
    r"(?<![а-я])(?:онлайн[- ]?школ\w*|ош|учусь|училась|учился|занимаюсь|занималась|занимался|готовлюсь|готовилась|готовился"
    r"|перешл\w*|перешел|ушл\w*|ушел)\W+(?:\w+\W+){0,2}$", re.I)

# ============================================================
# СКОРИНГ МНОГОЗНАЧНЫХ УПОМИНАНИЙ (context)
#
# Каждое вхождение получает балл из признаков; итог — три исхода:
#   балл >= ACCEPT_AT  -> школа (в schools_canonical)
#   UNSURE_AT..ACCEPT_AT-1 -> не ясно (в schools_unsure: на разметку / LLM, в выводах — как верхняя граница)
#   меньше UNSURE_AT  -> не школа
# ============================================================
ACCEPT_AT = 3
AUTHOR_PRIOR_MIN_POSTS = 2   # автор «свой» для школы, если назвал её точно хотя бы в 2 постах
UNSURE_AT = 1

W = {
    "construction": 3,       # «учусь в X», «онлайн-школа X», «курс от X», «X — онлайн-школа»…
    "list": 3,              # вплотную в перечне с другой школой (QC-1: такие почти всегда школы)
    "capital_or_quotes": 2,  # «Сотка» / Сотка с большой буквы посреди предложения
    "same_post_exact": 2,    # в этом же посте есть точное название той же школы
    "school_dense": 3,       # в ±150 символах ещё 2+ разные школы (обзор, рейтинг) — QC-1
    "school_near": 1,        # в ±150 символах ещё 1 школа
    "hashtag_cluster": 1,    # #x рядом с #онлайншкола / #ош / хэштегом другой школы
    "cue_in_sentence": 1,    # общий признак школы (курс, вебинар, промокод…) в том же предложении
    "extra_cue": 1,          # признак, специфичный для школы
    "evaluation": 1,         # «лучшая», «топ», «не советую», «дорогая»… — оценка как об организации
    "targeted_search": 1,    # пост найден поиском именно по этой школе
    "author_prior": 1,       # автор в >=2 постах называл эту школу точно
    "reject_near": -3,       # исключение рядом (не пересекается с самим словом)
    "competing_sense": -2,   # в том же предложении слова другого смысла (теорема, напиток, баллы…)
    "noisy_field": -1,       # речь/субтитры (Whisper) — шумнее
    "accusative_score": -1,  # «сотку», «максимум» в форме результата без конструкции
}

CONSTRUCTION_BEFORE = re.compile(
    r"(?<![а-яa-z])(?:онлайн[- ]?школ\w*|ош|школ\w*|платформ\w*|канал\w*|тгк:?|телеграм\w*|курс\w*(?:\s+(?:от|у|в))?"
    r"|вебинар\w*\s+(?:в|от)|препод\w*\s+(?:из|в)|куратор\w*\s+(?:из|в)|промокод\w*\s+(?:в|на)"
    r"|учусь|училась|учился|занимаюсь|занималась|занимался|готовлюсь|готовилась|готовился"
    r"|перешл\w*|перешел|ушл\w*|ушел|выбрал\w*|купил\w*\s+курс\w*\s+(?:в|у)|называ\w*|посоветовать)"
    r"(?:\s+(?:в|из|на|у|от))?\s*[«\"“]?\s*$", re.I)
CONSTRUCTION_AFTER = re.compile(
    r"^\s*[»\"”]?\s*,?\s*(?:[—–-]\s*(?:это)?|это|=|\()?\s*(?:(?:реально|самая|самой|лучш\w*|топ\w*|хорош\w*|классн\w*|крут\w*|любим\w*|моя|наша|норм\w*)\s+){0,2}"
    r"(?:онлайн[- ]?школ\w*|ош(?![а-я])|курс\w*|школ\w*(?:\s+(?:для|подготовки|на земле))?|платформ\w*)", re.I)
# Свои конструкции для отдельных школ (QC-1).
#   NOO: «в ноо», «от ноо», «выпускников ноо», «ноо егэ био» — аббревиатура почти не бывает другим словом
#   Морозилка: основатель/хэштег основателя где угодно в этом же поле
SCHOOL_CONSTRUCTION = {
    "NOO": (re.compile(r"(?:(?<![а-я])(?:в|во|от|из|с|у|про|для|приходите в|выпускник\w*|курс\w*|школ\w*|канал\w*)\s+$)"), 
            re.compile(r"^\s*(?:егэ|огэ|био|хим|рус|-?трекер|2\d\d\d)")),
    # «в стобальном», «с 100балльным», «организация стобального», «это стубальный репетитор»
    "100балльный репетитор": (re.compile(r"(?<![а-я])(?:в|во|с|со|из|от|у|организаци\w*|препод\w*|куратор\w*|курс\w*|это|работаю с)\s+$"),
                              re.compile(r"^\s*(?:репетитор\w*|скул|school)")),
}
SCHOOL_FIELD_CUE = {
    # «уроки в 99», «смотрю вебы у 99баллов», «готовлюсь в 99» — но не «на 99», «в 99 году»
    "99 Баллов": re.compile(r"(?<![а-я])(?:в|у)\s+99(?:\s*баллов)?(?=[\s,.!?)]|$)(?!\s*(?:год|%|балл(?!ов)))", re.I),
    "Морозилка": re.compile(r"папа\s?дан\w*|пападан\w*|даниил\w*\s+морозов\w*|морозов\w*\s+даниил\w*", re.I),
}
# Точное название засчитывается, только если в посте вообще есть учёба/экзамены (QC-1:
# «#морозилка #пустая #детство» — не школа, а «#морозилка #егэ» — школа). Иначе слово идёт в обычную оценку по баллам.
STUDY_TOPIC = re.compile(r"егэ|огэ|экзам|учеб|учёб|study|подготов|класс|дядя ?тош|пападан|папа дан|школ|профмат|профил"
                         r"|русск|математ|физик|информат|хими|биолог|истори|обществ|балл|вуз|поступ|абитур|репетитор|курс", re.I)
EXACT_NEEDS_TOPIC = {("Морозилка", r"#морозилк")}
# Другие школы рядом (не обязательно вплотную): обзор/рейтинг/сравнение школ
DENSE_WINDOW = 150
EVALUATION = re.compile(
    r"(?<![а-я])(?:лучш\w*|топ\w*|худш\w*|не советую|советую|рекомендую|дорог\w*|деш[её]в\w*|развод\w*|скам|кураторы|вебы|подача|тариф\w*)", re.I)
# #егэ/#огэ сюда НЕ входят: «#сотка #егэ» = и «сдал на сотку», и школа
HASHTAG_SCHOOLISH = re.compile(r"#(?:онлайн_?школ\w*|ош|подготовка_?к_?(?:егэ|огэ)\w*|курс\w*_?егэ|вебинар\w*)(?![а-я])", re.I)
# Признаки «откуда пост» (а не что в тексте). Одни они не делают пост школьным:
#   принять  — общий балл >= ACCEPT_AT и балл ТЕКСТА >= ACCEPT_TEXT_MIN
#   спорно   — балл текста >= 1, или текст нейтрален (0), но оба признака источника (поиск + автор)
PRIOR_FEATURES = {"targeted_search", "author_prior"}
ACCEPT_TEXT_MIN = 2
COMPETING = {
    "Сотка": r"балл\w*|из 100|набра\w*|получи\w*|результат\w*|жим\w*|присед\w*|станов\w*|\bкг\b|\bкм\b|рубл\w*|тыс\w*",
    "99 Баллов": r"набра\w*|получи\w*|результат\w*|пересда\w*|из 100|егэ по \w+\s*[—:–-]|балл(?:а|ов)?\s*(?:егэ|$)",
    "100балльный репетитор": r"результат\w*|набра\w*|получи\w*|стал\w* стобал",
    "Пифагор": r"теорем\w*|треугольн\w*|катет\w*|гипотенуз\w*|задач\w*",
    "Adrenaline": r"стресс\w*|эмоци\w*|волнени\w*|сердц\w*|кров\w*|напит\w*|энергетик\w*",
    "Турбо ЕГЭ": r"слово\s?пацана|словопацана|кащей\w*|кащея|марат\w*|вова\w*|адидас\w*|(?<![а-я])фф(?![а-я])|фанфик\w*|ожп|актер\w*|актёр\w*|копейкин\w*|сериал\w*"
                 r"|компрессор\w*|наддув\w*|двигател\w*|(?<![а-я])авто\w*|машин\w*|(?<![а-я])зно(?![а-я])|(?<![а-я])нмт(?![а-я])|pov|wattpad|фикбук",
    "Морозилка": r"холодильн\w*|заморо\w*|морожен\w*|ягод\w*|мяс\w*|пельмен\w*|положил\w*|достал\w*|лед(?![а-я])",
    "Lomonosov School": r"поэт\w*|учен\w*|век(?![а-я])|мгу|олимпиад\w*|михаил",
    "Турбо ЕГЭ": r"машин\w*|двигател\w*|режим\w*|скорост\w*",
}
COMPETING = {k: re.compile(v, re.I) for k, v in COMPETING.items()}
ACCUSATIVE_SCORE = {"Сотка": re.compile(r"^сотку$"), "MAXIMUM Education": re.compile(r"^максимум$")}
NOISY_FIELDS = {"transcript", "subtitles"}
# Заглавные буквы и кавычки ставил сам автор только в описании и хэштегах;
# в расшифровке/субтитрах/тексте с экрана их расставила модель (Whisper/Gemini) — там это не признак.
CASE_TRUSTED_FIELDS = {"description", "hashtags"}
# Школы-фамилии: «теорема Пифагора», «МГУ им. Ломоносова» пишутся с заглавной и без школы
CASE_USELESS_SCHOOLS = {"Пифагор", "Lomonosov School", "Турбо ЕГЭ"}   # Турбо — герой «Слова пацана»
# Для этих школ «другой смысл» ищем во всём поле (хэштеги сериала стоят в конце, в другом «предложении»)
COMPETING_FIELD_SCOPE = {"Турбо ЕГЭ"}
SENTENCE_SPLIT = re.compile(r"[.!?…\n]")


# ============================================================
# REGRESSION TESTS (из ручной проверки)
# ============================================================
TESTS = [
    # (текст, школы, которые ДОЛЖНЫ быть, школы, которых НЕ должно быть)
    ("после школы сотку набрала по профилю егэ", [], ["Сотка"]),
    ("в школе сотку получила на пробнике егэ", [], ["Сотка"]),
    ("в школе максимум 5 уроков, готовлюсь к егэ", [], ["MAXIMUM Education"]),
    ("это максимум изи, егэ по русскому", [], ["MAXIMUM Education"]),
    ("занимаюсь в умскуле, это максимум что я могу", ["Умскул"], ["MAXIMUM Education"]),
    ("у меня 99 баллов по русскому егэ", [], ["99 Баллов"]),
    ("набрав всего 99 баллов в сумме можно поступить", [], ["99 Баллов"]),
    ("стобалльница по химии егэ", [], ["100балльный репетитор"]),
    ("результаты оцениваются по 100-балльной шкале егэ", [], ["100балльный репетитор"]),
    ("найдём гипотенузу по пифагору, задача егэ", [], ["Пифагор"]),
    ("пифагор воскрес ященко плачет", [], ["Пифагор"]),
    ("адреналин на экзамене егэ зашкаливал", [], ["Adrenaline"]),
    ("энергетический напиток adrenaline rush", [], ["Adrenaline"]),
    ("ломоносов основал мгу, задание егэ по истории", [], ["Lomonosov School"]),
    ("#lomonosovschool #class #3а деньпобеды", [], ["Lomonosov School"]),
    ("положила в морозилку и пошла готовиться к егэ", [], ["Морозилка"]),
    ("учусь в умскуле и мой турбо режим подготовки", ["Умскул"], ["Турбо ЕГЭ"]),
    ("#словопацана #турбо #валературкин", [], ["Турбо ЕГЭ"]),
    ("skymars игра", [], ["Skysmart"]),
    ("красотке помог с физикой егэ", [], ["Сотка"]),
    ("я этому научился. запоминай в максимуме!", [], ["MAXIMUM Education"]),
    ("смотрела вебы от стобальных преподавателей в сотке", ["Сотка"], ["100балльный репетитор"]),
    ("преподаватель тамара, которая является стубальницей из сотки", [], ["100балльный репетитор"]),
    ("#sotkatv #стример #minecraft", [], ["Сотка"]),
    ("#сотка_на_егэ #турболитра", ["Турбо ЕГЭ"], ["Сотка"]),
    ("в фоксфорде лучше", ["Фоксфорд"], []),
    # настоящие упоминания
    ("учусь в сотке, сотка лучшая школа", ["Сотка"], []),
    ("школа сотка или умскул что выбрать", ["Сотка", "Умскул"], []),
    ("сравниваю умскул, вебиум и максимум", ["MAXIMUM Education", "Умскул", "Вебиум"], []),
    ("онлайн школа пифагор курс по математике", ["Пифагор"], []),
    ("занимаюсь в максимуме, кураторы топ", ["MAXIMUM Education"], []),
    ("100балльный репетитор промокод", ["100балльный репетитор"], []),
    ("adrenaline_chem лучшая онлайн-школа по химии", ["Adrenaline"], []),
    ("готовилась в онлайн-школе в сотке и набрала сотку", ["Сотка"], []),
    ("мой топ ош: 1) boost 2) neofamily 3) 100б 4) 100 сотка 5) егэland", ["Сотка", "NeoFamily", "ЕГЭленд"], []),
    ("демо-доступ к курсам турбо по революциям", ["Турбо ЕГЭ"], []),
    ("#турбо_литра #мастер_и_маргарита", ["Турбо ЕГЭ"], []),
    ("я занимаюсь в онлайн школе 99 баллов по подготовке к огэ", ["99 Баллов"], []),
    ("lomonosov school курсы егэ", ["Lomonosov School"], []),
    # «не ясно»: текст допускает оба смысла — не школа и не «нет», а на проверку (4-й элемент)
    ("сдала егэ, сотка лучшая", [], ["Сотка"], ["Сотка"]),
    ("Готовилась с репетитором, а Сотка мне не зашла", [], [], ["Сотка"]),
    # QC-1 (ручная разметка 09.10)
    ("ребята, в ноо срочно нужны новые кураторы по биологии", ["NOO"], []),
    ("главный совет. приходите в ноо.", ["NOO"], []),
    ("химия — ноо история — смитап физика, профмат и инфа — морозилка", ["NOO", "Морозилка"], []),
    ("биология топ • neofamily норм • егэхаб стрем • ноо профмат топ • морозилка норм", ["NOO", "Морозилка"], []),
    ("#морозилка #пустая #детство #врек #жиза", [], ["Морозилка"]),
    ("морозилка лучшая школа на земле", ["Морозилка"], []),
    ("мотивация учиться #егэ2027 #пападаня #профильнаяматематика #морозилка", ["Морозилка"], []),
    ("фанфик ожп/Турбо — \"девичьи слезы\" на фикбук #рекомендации #словопацана #турбо #fyp", [], ["Турбо ЕГЭ"]),
    ("мой любимый Турбо, надеюсь и это видео залетит, как с кащеем #театральный #турбо", [], ["Турбо ЕГЭ"]),
    ("вебінар від Турбо ЗНО #нмт #львів", [], ["Турбо ЕГЭ"]),
    ("по профильной математике конечно же хочу посоветовать школу пифагор", ["Пифагор"], []),
    ("она называется сотка, и там все очень бюджетно", ["Сотка"], []),
    ("егэ, егэ, ноо, морозилка,", ["NOO", "Морозилка"], []),
    ("дальше на этом же уровне у нас ноо, это школа для химбио", ["NOO"], []),
    ("не зря я все-таки сидела до трех утра в 100балльном)", ["100балльный репетитор"], []),
    ("следующая онлайн-школа тоже немало известная, это стубальный репетитор", ["100балльный репетитор"], []),
    ("я внимательно смотрю уроки в 99, честно! #99баллов #fyp #школа #егэ", ["99 Баллов"], []),
    ("егэ по химии — 96 баллов егэ по биологии — 99 баллов егэ по русскому — 91 балл", [], ["99 Баллов"]),
    ("стобалльный результат на егэ по химии", [], ["100балльный репетитор"]),
]


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
    s = clean_cell(x)
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower().replace("ё", "е").replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def normalize_features(x) -> str:
    s = normalize_light(x)
    s = re.sub(r"[^\wа-яa-z0-9#@._]+", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def split_multi(value):
    return [x.strip() for x in clean_cell(value).split(";") if x.strip()]


# ============================================================
# DICTIONARY
# ============================================================

def kind_from_label(label: str) -> str | None:
    t = normalize_features(label)
    if not t:
        return None
    if any(w in t for w in ("не школ", "исключ", "шум", "noise", "reject", "exclude")):
        return "reject"
    if any(w in t for w in ("перечн", "list")):
        return "list"
    if any(w in t for w in ("контекст", "context", "многознач", "ambiguous")):
        return "context"
    if any(w in t for w in ("искаж", "asr", "whisper", "fuzzy")):
        return "asr"
    if any(w in t for w in ("аккаунт", "препод", "автор", "амбассадор", "author")):
        return "author"
    if any(w in t for w in ("бренд", "brand", "exact")):
        return "exact"
    return None


def compile_rule(pattern: str, kind: str):
    p = pattern
    if kind in ("context", "list", "asr") and not p.startswith(("#", "(?<", "^")):
        p = LB + "(?:" + p + ")"
    return re.compile(p, re.I)


def load_rules() -> tuple[dict, pd.DataFrame]:
    raw = pd.read_excel(DICT_PATH, sheet_name="Словарь", dtype=str)
    for col in ("школа", "шаблон (regex)", "тип"):
        if col not in raw.columns:
            raise RuntimeError(f"В листе «Словарь» нет колонки «{col}»")
    rules, unknown, seen_disable = [], [], set()
    for i, row in raw.iterrows():
        school, pattern, label = clean_cell(row["школа"]), clean_cell(row["шаблон (regex)"]), clean_cell(row["тип"])
        if not school or not pattern:
            continue
        kind = kind_from_label(label)
        if kind is None:
            unknown.append((i + 2, school, label))
            continue
        if school == GLOBAL_NOISE_SCHOOL:
            kind = "global_reject"
        status = "from_dictionary"
        if (school, pattern) in RULE_DISABLE:
            seen_disable.add((school, pattern))
            status = "disabled: " + RULE_DISABLE[(school, pattern)]
        rules.append({"school": school, "pattern": pattern, "label": label, "kind": kind, "status": status,
                      "comment": clean_cell(row.get("комментарий", ""))})
    if unknown:
        raise RuntimeError("Неизвестные ярлыки типа в словаре (строка Excel, школа, ярлык):\n"
                           + "\n".join(map(str, unknown)) + "\nДобавьте ярлык в kind_from_label().")
    for school, pattern, kind, note in RULE_ADD:
        rules.append({"school": school, "pattern": pattern, "label": "(код)", "kind": kind,
                      "status": "added_in_code", "comment": note})
    missing_disable = [k for k in RULE_DISABLE if k not in seen_disable]
    if missing_disable:
        print("\n!!! Эти отключения не нашли строку в словаре (словарь поменялся?):")
        for k in missing_disable:
            print("    ", k)

    groups = {"exact": [], "asr": [], "context": [], "list": [], "author": [],
              "reject": defaultdict(list), "global_reject": []}
    for r in rules:
        if r["status"].startswith("disabled"):
            continue
        try:
            r["_rx"] = compile_rule(r["pattern"], r["kind"])
        except re.error as e:
            r["status"] = f"compile_error: {e}"
            continue
        if r["kind"] == "reject":
            groups["reject"][r["school"]].append(r)
        else:
            groups[r["kind"]].append(r)
    table = pd.DataFrame([{k: v for k, v in r.items() if k != "_rx"} for r in rules])
    bad = table[table["status"].str.startswith("compile_error")]
    if len(bad):
        raise RuntimeError("Битые regex:\n" + bad.to_string(index=False))
    return groups, table


# ============================================================
# MATCHING
# ============================================================

def overlaps(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def reject_reason(school, text, span, groups, mode) -> str:
    """mode='overlap' — только пересечение с совпадением; 'near' — в окне ±REJECT_WINDOW."""
    lo, hi = (span if mode == "overlap" else (max(0, span[0] - REJECT_WINDOW), span[1] + REJECT_WINDOW))
    for r in groups["reject"].get(school, []):
        for m in r["_rx"].finditer(text, max(0, lo - 60), hi + 60):
            ms = (m.start(), m.end())
            if (mode == "overlap" and overlaps(ms, span)) or (mode == "near" and overlaps(ms, (lo, hi))):
                return m.group(0)
    return ""


def snippet(text, span, width=SNIPPET):
    lo, hi = max(0, span[0] - width), min(len(text), span[1] + width)
    return ("…" if lo else "") + text[lo:span[0]] + "‹" + text[span[0]:span[1]] + "›" + text[span[1]:hi] + ("…" if hi < len(text) else "")


def normalize_cased(x) -> str:
    """Как normalize_light, но без перевода в нижний регистр (для признака «большая буква / кавычки»)."""
    s = clean_cell(x)
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).replace("ё", "е").replace("Ё", "Е").replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def sentence_bounds(text, span):
    lo = max((m.end() for m in SENTENCE_SPLIT.finditer(text, 0, span[0])), default=0)
    m = SENTENCE_SPLIT.search(text, span[1])
    return lo, (m.start() if m else len(text))


def hashtag_cluster(text, span):
    """Непрерывная цепочка хэштегов вокруг совпадения (или '' если слово не в хэштеге)."""
    if not (text[span[0]:span[0] + 1] == "#" or (span[0] > 0 and text[span[0] - 1] == "#")):
        return ""
    lo, hi = span[0], span[1]
    while lo > 0 and re.match(r"[#\w\s]", text[lo - 1]):
        lo -= 1
    while hi < len(text) and re.match(r"[#\w\s]", text[hi]):
        hi += 1
    return text[lo:hi]


def match_post(fields: dict, groups, ctx: dict | None = None, exact_only: bool = False) -> dict:
    """ctx: {'targeted': set(школ, по которым пост найден поиском), 'author_schools': set(школ, которые автор называл точно)}."""
    ctx = ctx or {}
    accepted, unsure, rejected, noise_hits = [], [], [], []
    texts, cased, noise_by_field, definite = {}, {}, {}, {}

    # ---- проход 1: однозначные названия (exact, asr) во всех полях ----
    post_has_topic = bool(STUDY_TOPIC.search(" ".join(normalize_light(v) for v in fields.values())))
    for field, raw in fields.items():
        text = normalize_light(raw)
        if not text:
            continue
        texts[field] = text
        c = normalize_cased(raw)
        cased[field] = c if len(c) == len(text) and c.lower() == text else ""
        noise = []
        for r in groups["global_reject"]:
            for m in r["_rx"].finditer(text):
                noise.append((m.start(), m.end()))
                noise_hits.append(m.group(0))
        noise_by_field[field] = noise
        definite[field] = []
        for kind in ("exact", "asr"):
            for r in groups[kind]:
                if not post_has_topic and (r["school"], r["pattern"]) in EXACT_NEEDS_TOPIC:
                    continue
                for m in r["_rx"].finditer(text):
                    span = (m.start(), m.end())
                    if any(overlaps(span, n) for n in noise):
                        rejected.append(_rec(r, field, text, m, "global_noise"))
                        continue
                    why = reject_reason(r["school"], text, span, groups, "overlap" if kind == "exact" else "near")
                    if why:
                        rejected.append(_rec(r, field, text, m, f"reject: {why}"))
                        continue
                    definite[field].append((r["school"], span))
                    accepted.append({**_rec(r, field, text, m, ""), "decision": "accept", "score": None, "features": kind})
    exact_schools = {a["school"] for a in accepted}

    # ---- проход 2: многозначные (context, list) — по баллам ----
    if not exact_only:
        for field, text in texts.items():
            ctext, noise, defs = cased[field], noise_by_field[field], definite[field]

            cands = []
            for kind_ in ("context", "list"):
                for r_ in groups[kind_]:
                    for m_ in r_["_rx"].finditer(text):
                        sp_ = (m_.start(), m_.end())
                        if not any(overlaps(sp_, n) for n in noise):
                            cands.append((r_["school"], sp_))
            all_mentions = defs + cands

            def others_near(school, span):
                lo_, hi_ = span[0] - DENSE_WINDOW, span[1] + DENSE_WINDOW
                return {s_ for s_, sp in all_mentions if s_ != school and lo_ <= sp[0] <= hi_}

            def list_near(school, span):
                for s_, sp in all_mentions:
                    if s_ == school:
                        continue
                    gap = text[sp[1]:span[0]] if sp[1] <= span[0] else text[span[1]:sp[0]] if span[1] <= sp[0] else ""
                    if len(gap) <= LIST_GAP_MAX and LIST_GAP.match(gap):
                        return True
                return False

            for kind in ("context", "list"):
                for r in groups[kind]:
                    school = r["school"]
                    for m in r["_rx"].finditer(text):
                        span = (m.start(), m.end())
                        if any(overlaps(span, n) for n in noise):
                            rejected.append(_rec(r, field, text, m, "global_noise"))
                            continue
                        if any(s_ == school and overlaps(sp, span) for s_, sp in defs):
                            continue
                        we = re.match(r"\w*", text[span[1]:]).end() + span[1]   # конец слова: «морозилк|а»
                        before, after = text[max(0, span[0] - 45):span[0]], text[we:we + 40]
                        construction = bool(CONSTRUCTION_BEFORE.search(before) or CONSTRUCTION_AFTER.search(after)
                                            or STRONG_PREFIX.search(before))
                        if school in SCHOOL_CONSTRUCTION:
                            b_rx, a_rx = SCHOOL_CONSTRUCTION[school]
                            construction = construction or bool(b_rx.search(before) or a_rx.search(after))
                        if school in SCHOOL_FIELD_CUE and SCHOOL_FIELD_CUE[school].search(text):
                            construction = True
                        overlap_rej = reject_reason(school, text, span, groups, "overlap")
                        if overlap_rej:
                            rejected.append(_rec(r, field, text, m, f"reject: {overlap_rej}"))
                            continue
                        is_list = list_near(school, span)
                        if kind == "list":
                            if not is_list:
                                rejected.append(_rec(r, field, text, m, "no_list_support"))
                                continue
                            accepted.append({**_rec(r, field, text, m, "other_school_near"), "decision": "accept",
                                             "score": None, "features": "list"})
                            continue

                        feats = {}
                        if construction:
                            feats["construction"] = W["construction"]
                        else:
                            near_rej = reject_reason(school, text, span, groups, "near")
                            if near_rej:
                                feats["reject_near"] = W["reject_near"]
                        if is_list:
                            feats["list"] = W["list"]
                        else:
                            near_ = others_near(school, span)
                            if len(near_) >= 2:
                                feats["school_dense"] = W["school_dense"]
                            elif near_:
                                feats["school_near"] = W["school_near"]
                        if ctext and field in CASE_TRUSTED_FIELDS and school not in CASE_USELESS_SCHOOLS:
                            ch, prev = ctext[span[0]], ctext[:span[0]].rstrip()
                            letters = re.sub(r"[^a-zа-яA-ZА-Я]", "", ctext)
                            mostly_caps = letters and sum(c.isupper() for c in letters) / len(letters) > 0.5
                            mid_sentence = prev and prev[-1] not in ".!?…:\n"
                            quoted = span[0] > 0 and ctext[span[0] - 1] in "«\"“"
                            if quoted or (ch.isupper() and mid_sentence and not mostly_caps):
                                feats["capital_or_quotes"] = W["capital_or_quotes"]
                        if school in exact_schools:
                            feats["same_post_exact"] = W["same_post_exact"]
                        cluster = hashtag_cluster(text, span)
                        c0 = text.find(cluster) if cluster else -1
                        if cluster and (HASHTAG_SCHOOLISH.search(cluster)
                                        or any(s_ != school and c0 <= sp[0] <= c0 + len(cluster) for s_, sp in defs)):
                            feats["hashtag_cluster"] = W["hashtag_cluster"]
                        lo, hi = sentence_bounds(text, span)
                        sent = text[lo:hi]
                        if school not in STRICT_CUES and GENERIC_CUE.search(sent):
                            feats["cue_in_sentence"] = W["cue_in_sentence"]
                        if school in STRICT_CUES and STRICT_CUES[school].search(text[max(0, span[0] - CUE_WINDOW):span[1] + CUE_WINDOW]):
                            feats["construction"] = W["construction"]
                        if school in EXTRA_CUES and EXTRA_CUES[school].search(sent):
                            feats["extra_cue"] = W["extra_cue"]
                        if EVALUATION.search(sent):
                            feats["evaluation"] = W["evaluation"]
                        if school in ctx.get("targeted", ()):
                            feats["targeted_search"] = W["targeted_search"]
                        if school in ctx.get("author_schools", ()):
                            feats["author_prior"] = W["author_prior"]
                        if school in COMPETING and not construction and COMPETING[school].search(
                                text if school in COMPETING_FIELD_SCOPE else sent):
                            feats["competing_sense"] = W["competing_sense"]
                        if field in NOISY_FIELDS and not construction:
                            feats["noisy_field"] = W["noisy_field"]
                        if school in ACCUSATIVE_SCORE and not construction and ACCUSATIVE_SCORE[school].match(m.group(0)):
                            feats["accusative_score"] = W["accusative_score"]
                        score = sum(feats.values())
                        rec = {**_rec(r, field, text, m, ""), "score": score,
                               "features": ";".join(f"{k}{v:+d}" for k, v in feats.items())}
                        prior = sum(v for k, v in feats.items() if k in PRIOR_FEATURES)
                        text_score = score - prior
                        if score >= ACCEPT_AT and text_score >= ACCEPT_TEXT_MIN:
                            accepted.append({**rec, "decision": "accept"})
                        elif score >= UNSURE_AT and (text_score >= 1 or (text_score == 0 and prior >= 2)):
                            unsure.append({**rec, "decision": "unsure"})
                        else:
                            rejected.append({**rec, "reason": f"score {score}: {rec['features'] or 'нет признаков'}"})

    acc_schools = {a["school"] for a in accepted}
    uns_schools = {u["school"] for u in unsure} - acc_schools

    def dedup(items, key):
        out, seen = [], set()
        for it in items:
            k = key(it)
            if k not in seen:
                seen.add(k)
                out.append(it)
        return out

    ev = dedup(accepted, lambda a: (a["school"], a["field"], a["kind"])) + \
        dedup([u for u in unsure if u["school"] in uns_schools], lambda u: (u["school"], u["field"]))
    rej = dedup(rejected, lambda r: (r["school"], r["form"], r["reason"]))
    for r in rej:
        r["school_accepted_anyway"] = r["school"] in acc_schools or r["school"] in uns_schools
    strongest = {}
    for a in accepted:
        rank = {"exact": 0, "asr": 1, "context": 2, "list": 3}[a["kind"]]
        strongest[a["school"]] = min(strongest.get(a["school"], 9), rank)
    return {
        "schools": sorted(acc_schools),
        "unsure": sorted(uns_schools),
        "review": sorted(s_ for s_, rk in strongest.items() if rk > 0),
        "types": sorted({a["kind"] for a in accepted}),
        "observed": sorted({a["form"] for a in accepted}),
        "fields": sorted({f"{a['school']}@{a['field']}" for a in accepted}),
        "rejected": sorted({f"{r['school']}::{r['form']}::{r['reason']}" for r in rej if not r["school_accepted_anyway"]}),
        "rejected_mixed": sorted({f"{r['school']}::{r['form']}::{r['reason']}" for r in rej if r["school_accepted_anyway"]}),
        "noise": sorted(set(noise_hits)),
        "evidence": ev,
        "rejected_evidence": rej,
    }


def _rec(r, field, text, m, reason_or_cue):
    return {"school": r["school"], "kind": r["kind"], "field": field, "form": m.group(0), "pattern": r["pattern"],
            "reason": reason_or_cue, "cue": reason_or_cue, "snippet": snippet(text, (m.start(), m.end()))}


def run_tests(groups) -> pd.DataFrame:
    rows = []
    for t in TESTS:
        text, must, must_not = t[:3]
        must_unsure = t[3] if len(t) > 3 else []
        res = match_post({"description": text}, groups)
        got, uns = set(res["schools"]), set(res["unsure"])
        miss = [s_ for s_ in must if s_ not in got]
        extra = [s_ for s_ in must_not if s_ in got]
        miss_uns = [s_ for s_ in must_unsure if s_ not in uns]
        rows.append({"text": text, "found": ";".join(sorted(got)), "unsure": ";".join(sorted(uns)),
                     "missing": ";".join(miss), "wrongly_found": ";".join(extra), "should_be_unsure": ";".join(miss_uns),
                     "ok": not miss and not extra and not miss_uns})
    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "00_regression_tests.csv", index=False, encoding="utf-8-sig")
    bad = df[~df["ok"]]
    print(f"\nРегрессионные тесты: {len(df) - len(bad)}/{len(df)} прошли")
    if len(bad):
        print(bad[["text", "found", "unsure", "missing", "wrongly_found", "should_be_unsure"]].to_string(index=False))
    return df


# ============================================================
# SIGNALS
# ============================================================

SIGNALS = {
    "has_promo_code": r"\bпромокод\w*|\bпромик\w*|\bмой код\b|\bпо коду\b",
    "has_discount": r"\bскидк\w*|\bдешевле\b",
    "has_telegram_cta": r"\bтелеграм\w*|\bтгк?\b|\bпиши мне\b|\bнапиши мне\b|\bссылка в профиле\b|\bссылка в био\b|\bв шапке профиля\b",
    "has_ad_disclosure": r"\bреклам\w*|\bсотрудничеств\w*|\bпартн[её]р\w*|\bбартер\w*|\berid\b",
    "has_ambassador": r"\bамба?ссадор\w*",
    "has_comparison_language": r"\bсравн\w*|\bлучше чем\b|\bхуже чем\b|\bvs\b|\bпротив\b|\bили\b.{0,40}\bчто выбрать\b|\bтоп[- ]?\d+\b",
    "has_transition": r"\bпереш\w* (?:из|в)\b|\bушл\w* из\b|\bушел из\b|\bсменил\w* школ\w*",
    "has_negative": r"\bне совет\w*|\bне рекоменд\w*|\bразочаров\w*|\bхудш\w*|\bне понрав\w*|\bплох\w*|\bужас\w*|\bразвод\w*|\bденьги на ветер\b",
    "has_recommendation": r"(?<!не )\bсовет\w*|(?<!не )\bрекоменд\w*|\bлучш\w*|\bмне понрав\w*",
    "has_personal_experience": r"\bя (?:учусь|училась|учился)\b|\bзанима(?:юсь|лась|лся)\b|\bготови(?:лась|лся)\b|\bготовлюсь\b|\b(?:брал|брала|купил|купила) курс\b",
}
SIGNALS = {k: re.compile(v, re.I) for k, v in SIGNALS.items()}


def author_hints(row, patterns) -> str:
    text = normalize_light(" ".join(clean_cell(row.get(c, "")) for c in ("author_username", "author_nickname", "author_bio")))
    return ";".join(sorted({r["school"] for r in patterns if text and r["_rx"].search(text)}))


# ============================================================
# MAIN
# ============================================================

def main():
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)
    print("=" * 70 + "\nSTAGE 1 / 02 — ПОИСК ШКОЛ (по полям, с доказательствами)\n" + "=" * 70)

    groups, rules_table = load_rules()
    rules_table.to_csv(OUT_DIR / "00_dictionary_rules_v4.csv", index=False, encoding="utf-8-sig")
    active = rules_table[~rules_table["status"].str.startswith("disabled")]
    print("\nПравила:", active["kind"].value_counts().to_dict(), "| отключено:", int(rules_table["status"].str.startswith("disabled").sum()))
    tests = run_tests(groups)

    if MASTER_PATH.exists():
        master = pd.read_parquet(MASTER_PATH)
    elif MASTER_PATH.with_suffix(".csv").exists():
        master = pd.read_csv(MASTER_PATH.with_suffix(".csv"), dtype=str, keep_default_na=False)
        for c in ("found_in_generic", "found_in_targeted"):
            master[c] = master[c].astype(str).str.lower().eq("true")
        master["create_time_parsed"] = pd.to_datetime(master["create_time_parsed"], errors="coerce")
    else:
        raise FileNotFoundError(f"Нет {MASTER_PATH}: сначала запустите 01_STAGE1_POSTS.py")
    print(f"\nПостов: {len(master):,}")
    for c in ("post_id_master", "author_key_final", "create_time_parsed", "found_in_generic", "found_in_targeted"):
        if c not in master.columns:
            raise RuntimeError(f"В master нет колонки {c}: сначала запустите новый 01_STAGE1_POSTS.py")
    fields_present = [f for f in TEXT_FIELDS if f in master.columns]
    if not fields_present:
        raise RuntimeError("В master нет полей текста (description, transcript…): нужен новый 01_STAGE1_POSTS.py")

    # ---- проход A: только однозначные названия -> «какие школы автор называет сам» ----
    print("Проход A: точные названия (для априорных данных по автору)…")
    rows = master[fields_present].to_dict("records")
    exact_res = [match_post({f: r[f] for f in fields_present}, groups, exact_only=True) for r in rows]
    author_counts = defaultdict(lambda: defaultdict(int))
    for key, r in zip(master["author_key_final"], exact_res):
        if key:
            for s_ in r["schools"]:
                author_counts[key][s_] += 1
    author_schools = {k: {s_ for s_, n in v.items() if n >= AUTHOR_PRIOR_MIN_POSTS} for k, v in author_counts.items()}

    # ---- откуда пост: по какой школе его искали ----
    known = set(rules_table["school"]) - {"-"}
    exact_rules = groups["exact"] + groups["asr"]

    def targeted_schools(row):
        out = {s_ for s_ in split_multi(row.get("plan_schools", "")) if s_ in known}
        q = normalize_light(row.get("found_by", ""))
        if q:
            out |= {r["school"] for r in exact_rules if r["_rx"].search(q)}
        return out
    tcols = [c for c in ("plan_schools", "found_by") if c in master.columns]
    targeted = [targeted_schools(r) for r in master[tcols].to_dict("records")] if tcols else [set()] * len(master)
    unknown_plan = sorted({s_ for v in master.get("plan_schools", pd.Series(dtype=str)) for s_ in split_multi(v)} - known)
    if unknown_plan:
        print("  ! в plan_schools есть названия не из словаря (не учитываются):", unknown_plan[:20])

    # ---- проход B: всё, многозначные — по баллам ----
    print("Проход B: многозначные названия по баллам…")
    res = [match_post({f: r[f] for f in fields_present}, groups,
                      ctx={"targeted": t, "author_schools": author_schools.get(k, set())})
           for r, t, k in zip(rows, targeted, master["author_key_final"])]
    master["schools_canonical"] = [";".join(r["schools"]) for r in res]
    master["schools_unsure"] = [";".join(r["unsure"]) for r in res]
    master["schools_canonical_max"] = [";".join(sorted(set(r["schools"]) | set(r["unsure"]))) for r in res]
    master["schools_review"] = [";".join(r["review"]) for r in res]
    master["school_match_types"] = [";".join(r["types"]) for r in res]
    master["schools_observed"] = [";".join(r["observed"]) for r in res]
    master["school_fields"] = [";".join(r["fields"]) for r in res]
    master["school_rejected_matches"] = [";".join(r["rejected"]) for r in res]
    master["school_rejected_mixed"] = [";".join(r["rejected_mixed"]) for r in res]
    master["global_noise_hits"] = [";".join(r["noise"]) for r in res]
    master["school_evidence_json"] = [json.dumps(r["evidence"], ensure_ascii=False) for r in res]
    master["school_rejected_json"] = [json.dumps(r["rejected_evidence"], ensure_ascii=False) for r in res]
    master["school_count"] = [len(r["schools"]) for r in res]
    master["school_unsure_count"] = [len(r["unsure"]) for r in res]
    if master["schools_canonical"].str.contains(r"(?:^|;)-(?:;|$)", regex=True).any():
        raise RuntimeError("Школа «-» попала в schools_canonical")

    print("Подсказки по автору (ник, имя, описание профиля — отдельно от упоминаний)…")
    master["author_affiliation_hint"] = [author_hints(r, groups["author"]) for r in master.to_dict("records")]

    print("Сигналы…")
    text = master["match_text"].fillna("").map(normalize_features)
    for name, rx in SIGNALS.items():
        master[name] = text.str.contains(rx, na=False)
    master["has_multiple_schools"] = master["school_count"] >= 2
    master["promo_candidate"] = master["has_promo_code"] | (master["has_discount"] & master["has_telegram_cta"])
    master["comparison_candidate"] = master["has_multiple_schools"] & master["has_comparison_language"]
    master["switch_candidate"] = master["has_multiple_schools"] & master["has_transition"]
    master["negative_candidate"] = master["school_count"].gt(0) & master["has_negative"]
    master["recommendation_candidate"] = master["school_count"].gt(0) & master["has_recommendation"]
    master["affiliate_candidate"] = master["has_ambassador"] | master["has_ad_disclosure"] | master["author_affiliation_hint"].ne("")
    labels = ["promo", "comparison", "switch", "negative", "recommendation", "affiliate"]
    master["post_logic_candidates"] = master.apply(
        lambda r: ";".join(l for l in labels if r[f"{l}_candidate"]) or "other", axis=1)
    master["needs_review"] = (master["schools_review"].ne("") | master["schools_unsure"].ne("")
                              | master["school_rejected_matches"].ne(""))

    # ------------------ сводки ------------------
    ex = master[master["schools_canonical"].ne("")][["post_id_master", "schools_canonical", "author_key_final",
                                                     "found_in_generic", "found_in_targeted", "create_time_parsed"]].copy()
    ex["school"] = ex["schools_canonical"].map(split_multi)
    ex = ex.explode("school")
    baseline = (ex.groupby("school").agg(posts=("post_id_master", "nunique"),
                                         unique_authors=("author_key_final", lambda x: x[x.ne("")].nunique()),
                                         generic_posts=("found_in_generic", "sum"),
                                         targeted_posts=("found_in_targeted", "sum"))
                .reset_index().sort_values("posts", ascending=False))
    review_counts = master["schools_review"].map(split_multi).explode().value_counts()
    baseline["posts_without_exact"] = baseline["school"].map(review_counts).fillna(0).astype(int)
    unsure_counts = master["schools_unsure"].map(split_multi).explode().value_counts()
    baseline = baseline.merge(pd.DataFrame({"school": unsure_counts.index, "posts_unsure": unsure_counts.values}),
                              on="school", how="outer").fillna({"posts": 0, "posts_unsure": 0, "posts_without_exact": 0})
    for c in ("posts", "posts_unsure", "posts_without_exact", "unique_authors", "generic_posts", "targeted_posts"):
        baseline[c] = baseline[c].fillna(0).astype(int)
    baseline["posts_max"] = baseline["posts"] + baseline["posts_unsure"]
    baseline = baseline.sort_values("posts", ascending=False)
    baseline.to_csv(OUT_DIR / "02_school_baseline_v4.csv", index=False, encoding="utf-8-sig")

    pairs = defaultdict(int)
    for s in master["schools_canonical"]:
        for a, b in combinations(sorted(split_multi(s)), 2):
            pairs[(a, b)] += 1
    pd.DataFrame([{"school_a": a, "school_b": b, "posts": n} for (a, b), n in pairs.items()]) \
        .sort_values("posts", ascending=False).to_csv(OUT_DIR / "03_school_comentions_v4.csv", index=False, encoding="utf-8-sig")

    (ex[ex["author_key_final"].ne("")].groupby(["author_key_final", "school"])
     .agg(posts=("post_id_master", "nunique"), first_seen=("create_time_parsed", "min"), last_seen=("create_time_parsed", "max"))
     .reset_index().sort_values("posts", ascending=False)
     .to_csv(OUT_DIR / "04_author_school_v4.csv", index=False, encoding="utf-8-sig"))

    sig_cols = list(SIGNALS) + [f"{l}_candidate" for l in labels]
    pd.DataFrame({"signal": sig_cols, "posts": [int(master[c].sum()) for c in sig_cols]}) \
        .sort_values("posts", ascending=False).to_csv(OUT_DIR / "05_signal_baseline_v4.csv", index=False, encoding="utf-8-sig")

    fk = defaultdict(int)
    for evs in master["school_evidence_json"]:
        for e in json.loads(evs):
            fk[(e["school"], e["field"], e["kind"])] += 1
    pd.DataFrame([{"school": s, "field": f, "kind": k, "posts": n} for (s, f, k), n in fk.items()]) \
        .sort_values(["school", "posts"], ascending=[True, False]) \
        .to_csv(OUT_DIR / "05b_school_by_field_kind_v4.csv", index=False, encoding="utf-8-sig")

    try:
        master.to_parquet(OUT_DIR / "01_posts_master_features_v4.parquet", index=False)
    except Exception as e:
        print("Parquet не записан:", e, "-> CSV")
        master.to_csv(OUT_DIR / "01_posts_master_features_v4.csv", index=False, encoding="utf-8-sig")

    review = master[master["needs_review"]].copy()
    review["evidence"] = review["school_evidence_json"].map(
        lambda s: "\n".join(f"{e['school']} [{e.get('decision', 'accept')} {e['kind']}/{e['field']}"
                            f"{' балл ' + str(e['score']) + ': ' + e['features'] if e.get('score') is not None else ''}] {e['snippet']}"
                            for e in json.loads(s))[:3000])
    review["rejected_evidence"] = review["school_rejected_json"].map(
        lambda s: "\n".join(f"{e['school']} [{e['reason']}/{e['field']}] {e['snippet']}" for e in json.loads(s))[:3000])
    cols = ["post_id_master", "post_url", "create_time_parsed", "author_key_final", "author_username", "source_groups",
            "schools_canonical", "schools_unsure", "schools_review", "school_match_types", "school_fields", "school_rejected_matches",
            "author_affiliation_hint", "post_logic_candidates", "evidence", "rejected_evidence"]
    review[[c for c in cols if c in review.columns]].head(100_000).to_excel(OUT_DIR / "06_posts_for_review_v4.xlsx", index=False)

    qc = {
        "posts": len(master),
        "posts_with_school": int(master["school_count"].gt(0).sum()),
        "posts_with_multiple_schools": int(master["school_count"].gt(1).sum()),
        "posts_school_only_context_or_list": int(master["schools_review"].ne("").sum()),
        "posts_with_unsure_school": int(master["schools_unsure"].ne("").sum()),
        "posts_with_school_max (accepted+unsure)": int(master["schools_canonical_max"].ne("").sum()),
        "authors_with_prior": sum(1 for v in author_schools.values() if v),
        "scoring": {"accept_at": ACCEPT_AT, "accept_text_min": ACCEPT_TEXT_MIN, "unsure_at": UNSURE_AT, "weights": W},
        "posts_with_true_rejections": int(master["school_rejected_matches"].ne("").sum()),
        "posts_with_global_noise": int(master["global_noise_hits"].ne("").sum()),
        "unique_authors": int(master.loc[master["author_key_final"].ne(""), "author_key_final"].nunique()),
        "regression_tests_passed": f"{int(tests['ok'].sum())}/{len(tests)}",
        "rules_active": active["kind"].value_counts().to_dict(),
        "rules_disabled": int(rules_table["status"].str.startswith("disabled").sum()),
        "brand_layer_status": "not frozen — run 03_FINAL_QC_AND_AUTHORS.py and label the sample",
    }
    with (OUT_DIR / "07_QC_SUMMARY_V4.json").open("w", encoding="utf-8") as f:
        json.dump(qc, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 70 + "\nDONE\n" + "=" * 70)
    print(json.dumps(qc, ensure_ascii=False, indent=2))
    print("\nTOP SCHOOLS:\n" + baseline.head(30).to_string(index=False))


if __name__ == "__main__":
    main()
