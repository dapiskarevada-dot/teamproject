@echo off
rem Census run: date window, all channels, no post cap, NO videos/Whisper/comments. Coverage sheet in each ITOG.
cd /d "%~dp0"
call "%~dp0NIGHT.bat" plan_census.txt --redo --search-only --no-videos --whisper off
