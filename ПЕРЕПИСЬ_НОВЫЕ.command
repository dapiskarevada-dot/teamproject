#!/bin/bash
# ПЕРЕПИСЬ НОВЫХ И ПУСТЫХ ШКОЛ (EGEHub, NOO, Морозилка, Профиматика, Insperia, Школково, Егэфлекс, NeoFamily, …) на Маке,
# через залогиненные аккаунты TikTok: поиск по запросам, хэштегам, аккаунтам школ и лентам авторов.
# Только ссылки и данные постов — без видео, Whisper, OCR и комментариев (это сделает сервер).
# Окно 01.10.2025–01.10.2026. Потом: НОВЫЕ_ДЛЯ_СЕРВЕРА.command -> server/new_posts_mac.csv -> загрузить на под.
cd "$(dirname "$0")"
[ -f plan_new_schools.txt ] || { echo "Нет plan_new_schools.txt — сначала 0_update_from_github.command"; read -p Enter; exit 1; }
exec bash "./НА_НОЧЬ.command" --plan plan_new_schools.txt --search-only --no-videos --no-ocr --whisper off "$@"
