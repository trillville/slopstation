@echo off
REM smartd -M exec target: one SMART warning becomes one event on the K15's
REM stream. smartd runs as SYSTEM because raw device access needs it, so this
REM script is the only thing that crosses back into the repo's telemetry.
REM
REM EDIT PY. Python here is a per-user install and SYSTEM's PATH does not carry
REM it, so a bare `python` resolves to nothing and every alert is lost silently.
REM Confirm with: (Get-Command python).Source
set PY=C:\Users\minipc\AppData\Local\Programs\Python\Python313\python.exe
if not exist "%PY%" set PY=python
setlocal
set LEVEL=warn
if /I "%SMARTD_FAILTYPE%"=="Health" set LEVEL=error
if /I "%SMARTD_FAILTYPE%"=="Usage" set LEVEL=error
REM stdout only: a broken interpreter must still reach smartd's log, and
REM `exit /b 0` keeps a failed alert from taking the service down with it.
"%PY%" "%~dp0events.py" emit supervisor smart_warning device="%SMARTD_DEVICE%" failtype="%SMARTD_FAILTYPE%" msg="%SMARTD_MESSAGE%" --level %LEVEL% >nul
exit /b 0
