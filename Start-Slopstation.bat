@echo off
rem The Windows startup shortcut (shell:startup -> this file) and the thing to
rem run after a git pull: both lanes on the code on disk right now.
cd /d "%~dp0"
.venv\Scripts\slopstation-start.exe
