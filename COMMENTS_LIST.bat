@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist server\comment_urls.txt (echo No server\comment_urls.txt & pause & exit /b 1)
python -u comments_list.py server\comment_urls.txt --count 200
pause
