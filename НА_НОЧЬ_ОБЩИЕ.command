#!/bin/bash
# Для второго сборщика: то же, что НА_НОЧЬ.command, но по плану общих запросов про ЕГЭ (plan_general_ege.txt).
cd "$(dirname "$0")"
exec bash "./НА_НОЧЬ.command" --plan plan_general_ege.txt "$@"
