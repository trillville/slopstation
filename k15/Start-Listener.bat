@echo off
rem Chord listener supervisor. To STOP the listener, close this window -
rem any exit (crash or Ctrl+C) restarts it after 10s. reconcile runs once
rem per boot, outside the loop: re-running it mid-session would spawn a
rem second watch loop against the live session lock.
rem SINGLE INSTANCE: an fd-9 handle on a lock file, held for this window's
rem lifetime, bounces a second launch instead of double-listening on the Puck.
rem It self-releases on ANY exit, so a dead supervisor can't wedge the next one.
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
python events.py emit supervisor start what=listener >nul 2>&1
:listener
python chord_listener.py
rem Capture the exit code FIRST: every command after this resets %errorlevel%.
set "CODE=%errorlevel%"
echo [supervisor] listener exited (code %CODE%) - restarting in 10s
echo [%date% %time%] [supervisor] listener exited (code %CODE%) - restarting in 10s>> couch.log
rem Structured twin of the line above, so a crash LOOP is alertable.
python events.py emit supervisor restart what=listener code=%CODE% --level warn >nul 2>&1
timeout /t 10 /nobreak >nul
goto listener
