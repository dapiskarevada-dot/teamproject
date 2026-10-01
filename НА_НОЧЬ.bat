@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem ═══════════════════════════════════════════════════════════════════════════
rem  НА НОЧЬ (Windows) — двойной клик. Делает всё сам:
rem   0) подтягивает свежий код из GitHub (если есть интернет),
rem   1) ставит окружение, если его ещё нет (первый запуск: 5–15 минут),
rem   2) спрашивает, добавить ли аккаунты TikTok (если не нужно — просто Enter),
rem   3) запускает сбор по всем школам из schools_plan.txt, параллельно, по потоку на аккаунт.
rem  Компьютер не уснёт, пока идёт сбор. Окно можно свернуть, НЕ закрывать.
rem  Нужен Python 3.10+ с python.org (при установке поставить галочку "Add Python to PATH").
rem  Секрет в этой папке: openrouter_key.txt (расшифровка слайдов). Без него слайды пропустятся.
rem ═══════════════════════════════════════════════════════════════════════════

echo =================== ШАГ 0. ОБНОВЛЕНИЕ КОДА ИЗ GITHUB ===================
set ZIP=%TEMP%\teamproject_main.zip
set DIR=%TEMP%\teamproject_main
powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -Uri 'https://github.com/dapiskarevada-dot/teamproject/archive/refs/heads/main.zip' -OutFile '%ZIP%' -TimeoutSec 60; if (Test-Path '%DIR%') { Remove-Item -Recurse -Force '%DIR%' }; Expand-Archive -Path '%ZIP%' -DestinationPath '%DIR%' -Force; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  copy /y "%DIR%\teamproject-main\*.py" . >nul
  copy /y "%DIR%\teamproject-main\*.bat" . >nul
  copy /y "%DIR%\teamproject-main\README.md" . >nul
  copy /y "%DIR%\teamproject-main\requirements.txt" . >nul
  copy /y "%DIR%\teamproject-main\schools.txt" . >nul
  if not exist schools_plan.txt copy /y "%DIR%\teamproject-main\schools_plan.txt" . >nul
  echo ^>^>^> Код обновлён.
) else (
  echo ^>^>^> GitHub недоступен ^(нет сети / VPN^) — работаю с тем кодом, что есть в папке.
)
if exist __pycache__ rd /s /q __pycache__

echo.
echo =================== ШАГ 1. ОКРУЖЕНИЕ ===================
if not exist .venv (
  echo ^>^>^> Первый запуск на этом компьютере: ставлю окружение ^(5–15 минут^)...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist .venv (
    echo !!! Не найден Python. Установите Python 3.11+ с python.org, поставьте галочку "Add Python to PATH" и запустите снова.
    pause & exit /b 1
  )
)
call .venv\Scripts\activate.bat
python -c "import pytok" 2>nul || pip install -q "git+https://github.com/MEOMcGill/pytok.git@master"
python -c "import pandas, openpyxl, requests" 2>nul || pip install -q -r requirements.txt
python -c "import camoufox" 2>nul || pip install -q camoufox
python -m camoufox path >nul 2>nul || python -m camoufox fetch
python -c "import faster_whisper, av, numpy" 2>nul || (echo ^>^>^> Ставлю Whisper ^(5–10 минут^)... & pip install -q faster-whisper av numpy)
pip install -q -U yt-dlp 2>nul
python -u collect_tiktok_search_threads.py --check >nul 2>nul || (echo !!! Проверка кода не прошла: & python -u collect_tiktok_search_threads.py --check & pause & exit /b 1)
if not exist openrouter_key.txt echo !!! Нет openrouter_key.txt — слайды каруселей не будут расшифрованы ^(остальное пойдёт^).
if exist cases\tiktok_search_threads if not exist cases\Умскул move cases\tiktok_search_threads cases\Умскул >nul
echo ^>^>^> Окружение в порядке.

echo.
echo =================== ШАГ 2. АККАУНТЫ TIKTOK ===================
echo Сейчас в пуле:
python -m pytok.accounts.cli list
echo.
:ask
set "U="
echo ^>^>^> Добавить аккаунт? Введите логин ^(email / телефон / username^) и Enter.
echo ^>^>^> Если добавлять НЕ нужно — просто нажмите Enter.
set /p U=
if "!U!"=="" goto run
python -m pytok.accounts.cli add --username "!U!"
echo ^>^>^> Откроется браузер: войдите в TikTok, пройдите капчу, дождитесь ленты — окно закроется само.
python -m pytok.accounts.cli login --username "!U!" --manual-login
echo ^>^>^> Аккаунт !U! добавлен. В пуле:
python -m pytok.accounts.cli list
echo.
goto ask

:run
echo.
echo =================== ШАГ 3. ЗАПУСК НА НОЧЬ ===================
echo ^>^>^> Школы из schools_plan.txt, до 500 постов на школу, потоков = число аккаунтов в пуле.
echo ^>^>^> Логи: night_^<дата^>.log ^(общий^) и pipeline_^<школа^>.log ^(по школам^).
echo ^>^>^> Компьютер не уснёт, пока идёт сбор. Это окно можно свернуть, НЕ закрывать.
echo.
python -u pipeline.py --parallel 0 --log %*
echo.
echo =================== ГОТОВО ===================
echo Итоги: ИТОГ_^<школа^>.xlsx по каждой школе и ИТОГ_ВСЕ_ШКОЛЫ.xlsx. Состояние: pipeline_state.json
echo Если что-то оборвалось — запустите этот же файл снова: готовые школы пропустит, продолжит остальные.
pause
