@echo off
rem Chord listener supervisor. To STOP the listener, close this window -
rem any exit (crash or Ctrl+C) restarts it after 10s. reconcile runs once
rem per boot, outside the loop: re-running it mid-session would spawn a
rem second watch loop against the live session lock.
rem SINGLE INSTANCE: an fd-9 handle on a lock file is held for this window's
rem lifetime, so a second launch (stray shortcut, manual run alongside the
rem startup copy) bounces instead of double-listening on the Puck. The handle
rem self-releases on ANY exit, so a dead supervisor can't wedge the next one.
cd /d "%~dp0"
set "STARTED="
2>nul ( 9>"%TEMP%\couch-listener-supervisor.lock" ( set STARTED=1 & call :main ) )
if not defined STARTED (
  echo [supervisor] another listener window is already running - close it first
  pause
  exit /b 1
)
exit /b

:main
python couch.py reconcile
:listener
python chord_listener.py
echo [supervisor] listener exited (code %errorlevel%) - restarting in 10s
echo [%date% %time%] [supervisor] listener exited (code %errorlevel%) - restarting in 10s>> couch.log
timeout /t 10 /nobreak >nul
goto listener
