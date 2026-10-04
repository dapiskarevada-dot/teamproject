@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
call .venv\Scripts\activate.bat
python -u export_cookies.py
pause
