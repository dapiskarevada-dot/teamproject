@echo off
rem Comments and replies for the same posts, no videos/Whisper/frames.
cd /d "%~dp0"
call "%~dp0NIGHT.bat" plan_census.txt --heavy --redo --no-videos --no-images --whisper off --frames off
