@echo off
rem Depth without comments: videos, then Whisper + frame text in one process.
cd /d "%~dp0"
call "%~dp0NIGHT.bat" plan_census.txt --heavy --redo --search-only
