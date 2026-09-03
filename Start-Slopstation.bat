@echo off
rem Start what is down, reload what is up: the thing to run after a git pull.
rem The lanes themselves start at logon as scheduled tasks (Setup-K15-Tasks.ps1).
cd /d "%~dp0"
.venv\Scripts\slopstation-start.exe
