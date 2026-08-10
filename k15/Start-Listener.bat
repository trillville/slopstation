@echo off
rem Chord listener supervisor. To STOP the listener, close this window -
rem any exit (crash or Ctrl+C) restarts it after 10s. reconcile runs once
rem per boot, outside the loop: re-running it mid-session would spawn a
rem second watch loop against the live session lock.
cd /d "%~dp0"
python couch.py reconcile
:listener
python chord_listener.py
echo [supervisor] listener exited (code %errorlevel%) - restarting in 10s
echo [%date% %time%] [supervisor] listener exited (code %errorlevel%) - restarting in 10s>> couch.log
timeout /t 10 /nobreak >nul
goto listener
