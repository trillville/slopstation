@echo off
REM smartd -M exec target: one SMART warning becomes one event on the K15's
REM stream. smartd runs as SYSTEM because raw device access needs it, so this
REM script is the only thing that crosses back into the repo's telemetry.
REM
REM The venv interpreter by absolute path, not `python`: SYSTEM's PATH does not
REM carry the per-user install, so a bare `python` resolves to nothing and
REM every alert is lost silently.
setlocal
set LEVEL=warn
if /I "%SMARTD_FAILTYPE%"=="Health" set LEVEL=error
if /I "%SMARTD_FAILTYPE%"=="Usage" set LEVEL=error
REM stdout only: a broken interpreter must still reach smartd's log, and
REM `exit /b 0` keeps a failed alert from taking the service down with it.
"%~dp0.venv\Scripts\python.exe" -m slopstation.events emit supervisor smart_warning device="%SMARTD_DEVICE%" failtype="%SMARTD_FAILTYPE%" msg="%SMARTD_MESSAGE%" --level %LEVEL% >nul
exit /b 0
