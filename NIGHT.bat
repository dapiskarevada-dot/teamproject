@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem =====================================================================
rem  NIGHT RUN (Windows). Double-click. Does everything itself:
rem   0) pulls fresh code from GitHub (if internet is available)
rem   1) creates/completes the Python environment (first run: 5-15 min)
rem   2) asks whether to add TikTok accounts (press Enter to skip)
rem   3) runs the collection for every school in the plan, in parallel
rem  The PC will not sleep while it runs. Minimize this window, do NOT close it.
rem  Needs Python 3.10+ from python.org ("Add Python to PATH" checked).
rem  Put openrouter_key.txt in this folder (slide transcription), else slides are skipped.
rem  Plan file: schools_plan.txt by default; NIGHT_GENERAL.bat uses plan_general_ege.txt.
rem =====================================================================
set PLAN=schools_plan.txt
if not "%~1"=="" set PLAN=%~1
set EXTRA=
:more
shift
if "%~1"=="" goto done
set EXTRA=%EXTRA% %1
goto more
:done

echo =================== STEP 0. UPDATE CODE FROM GITHUB ===================
set ZIP=%TEMP%\teamproject_main.zip
set DIR=%TEMP%\teamproject_main
powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -Uri 'https://github.com/dapiskarevada-dot/teamproject/archive/refs/heads/main.zip' -OutFile '%ZIP%' -TimeoutSec 60; if (Test-Path '%DIR%') { Remove-Item -Recurse -Force '%DIR%' }; Expand-Archive -Path '%ZIP%' -DestinationPath '%DIR%' -Force; exit 0 } catch { exit 1 }"
if "%errorlevel%"=="0" (
  copy /y "%DIR%\teamproject-main\*.py" . >nul
  copy /y "%DIR%\teamproject-main\*.bat" . >nul
  copy /y "%DIR%\teamproject-main\README.md" . >nul
  copy /y "%DIR%\teamproject-main\requirements.txt" . >nul
  copy /y "%DIR%\teamproject-main\schools.txt" . >nul
  if not exist schools_plan.txt copy /y "%DIR%\teamproject-main\schools_plan.txt" . >nul
  for %%f in ("%DIR%\teamproject-main\plan_*.txt") do if not exist "%%~nxf" copy /y "%%f" . >nul
  echo   code updated.
) else (
  echo   GitHub not reachable - using the code already in this folder.
)
if exist __pycache__ rd /s /q __pycache__

echo.
echo =================== STEP 1. ENVIRONMENT ===================
if not exist .venv (
  echo   first run on this PC: creating the environment, 5-15 minutes...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
)
if not exist .venv\Scripts\activate.bat (
  echo   ERROR: Python not found. Install Python 3.11+ from python.org with "Add Python to PATH" and run again.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -c "import pytok" 2>nul || pip install -q "git+https://github.com/MEOMcGill/pytok.git@master"
python -c "import pandas, openpyxl, requests" 2>nul || pip install -q -r requirements.txt
python -c "import camoufox" 2>nul || pip install -q camoufox
python -m camoufox path >nul 2>nul || python -m camoufox fetch
python -c "import faster_whisper, av, numpy" 2>nul || (echo   installing Whisper, 5-10 minutes... & pip install -q faster-whisper av numpy)
pip install -q -U yt-dlp 2>nul
python -u collect_tiktok_search_threads.py --check >nul 2>nul || (echo   ERROR: code check failed: & python -u collect_tiktok_search_threads.py --check & pause & exit /b 1)
if not exist openrouter_key.txt echo   WARNING: no openrouter_key.txt - carousel slides will not be transcribed, everything else runs.
if not exist "%PLAN%" (echo   ERROR: plan file "%PLAN%" not found & pause & exit /b 1)
echo   environment OK. Plan: %PLAN%

echo.
echo =================== STEP 2. TIKTOK ACCOUNTS ===================
echo   accounts in the pool now:
python -m pytok.accounts.cli list
echo.
:ask
set "U="
echo   Add an account? Type its login (email / phone / username) and press Enter.
echo   If NOT needed - just press Enter.
set /p U=
if "!U!"=="" goto run
python -m pytok.accounts.cli add --username "!U!"
echo   A browser will open: log in to TikTok, pass the captcha, wait for the feed - it closes itself.
python -m pytok.accounts.cli login --username "!U!" --manual-login
echo   account !U! added. Pool:
python -m pytok.accounts.cli list
echo.
goto ask

:run
echo.
echo =================== STEP 3. NIGHT RUN ===================
echo   schools from %PLAN%, up to 500 posts per school, threads = accounts in the pool.
echo   logs: night_DATE.log (overall) and pipeline_SCHOOL.log (per school).
echo   The PC will not sleep. Minimize this window, do NOT close it.
echo.
python -u pipeline.py --parallel 0 --log --plan "%PLAN%"%EXTRA%
echo.
echo =================== DONE ===================
echo   results: ITOG_SCHOOL.xlsx per school and ITOG_ALL.xlsx in this folder. State: pipeline_state.json
echo   If something crashed - run this file again: finished schools are skipped, the rest continue.
pause
