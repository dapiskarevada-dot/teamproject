@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
call .venv\Scripts\activate.bat
python status.py
if exist plan_general_ege.txt python status.py --plan plan_general_ege.txt
pause
