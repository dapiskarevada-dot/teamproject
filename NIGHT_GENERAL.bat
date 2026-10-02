@echo off
rem Second collector: same as NIGHT.bat, but with the general EGE/OGE plan (plan_general_ege.txt).
cd /d "%~dp0"
call "%~dp0NIGHT.bat" plan_general_ege.txt
