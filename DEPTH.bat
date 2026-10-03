@echo off
rem Depth stage over collected posts: sample per school -> videos + comments/replies, then Whisper + frame text in one process.
cd /d "%~dp0"
call "%~dp0NIGHT.bat" plan_census.txt --heavy --redo
