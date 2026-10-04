@echo off
rem Download videos through the browser (when yt-dlp fails). Transcription: TRANSCRIPTS.bat --wait in parallel.
cd /d "%~dp0"
call "%~dp0NIGHT.bat" plan_census.txt --heavy --redo --search-only --whisper off --frames off
