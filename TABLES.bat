@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
call .venv\Scripts\activate.bat
python -u pipeline.py --final-only
if exist plan_general_ege.txt python -u pipeline.py --final-only --plan plan_general_ege.txt
pause
