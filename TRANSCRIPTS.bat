@echo off
rem Transcripts by links: yt-dlp -> frames -> Whisper large-v3 -> delete video. No browser, no TikTok accounts.
cd /d "%~dp0"
set PYTHONUTF8=1
call .venv\Scripts\activate.bat
python -c "import faster_whisper, av, numpy, PIL" 2>nul || pip install -q faster-whisper av numpy pillow
pip install -q -U yt-dlp 2>nul
python -u transcribe_links.py %*
pause
