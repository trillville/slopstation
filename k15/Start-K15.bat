@echo off
rem The K15's Startup shortcut (shell:startup -> this file) and the thing to
rem run after a git pull: both lanes on the code on disk right now.
rem   listener = the chord lane, load-bearing, system python
rem   voice    = the overlay, its own venv, allowed to die
rem
rem Per lane: supervisor down -> start it; supervisor up -> kill the AGENT and
rem let the supervisor relaunch it after its 10s backoff. Up/down comes from
rem the fd-9 lock the supervisor holds for its window's lifetime.
rem
rem Kill AGENTS, never supervisor windows: couch.py runs in its own console, so
rem killing the listener's window orphans a live session's watch loop and the
rem replacement's `couch.py reconcile` adopts that session - two watch loops
rem racing one teardown. reconcile runs at :main, outside the restart loop.
setlocal
set "RELOADED="
if not "%~1"=="" echo [start-k15] note: no arguments needed - this always reloads

call :supervised "%TEMP%\couch-listener-supervisor.lock"
if errorlevel 1 (
  echo [start-k15] chord lane down - starting it
  python "%~dp0events.py" emit supervisor lane_started what=listener >nul 2>&1
  start "K15 chord listener" /min /d "%~dp0" Start-Listener.bat
) else (
  set "RELOADED=1"
  call :reload chord_listener.py "chord listener" listener
)

rem The pre-migration supervisor holds this SAME lock and can never relaunch:
rem its requirements.txt moved to agent\, so its gate pip-installs a file that
rem is gone and wedges at a pause. The lock name must not change to dodge it -
rem a second supervisor puts two agents on one microphone. The tell is the venv
rem git could not move. No pause below: this file also runs at boot.
set "LEGACYVOICE="
if exist "%~dp0voice\.venv" set "LEGACYVOICE=1"

call :supervised "%TEMP%\couch-voice-supervisor.lock"
if errorlevel 1 (
  echo [start-k15] voice lane down - starting it
  python "%~dp0events.py" emit supervisor lane_started what=voice >nul 2>&1
  start "K15 voice" /min /d "%~dp0agent" Start-Voice.bat
) else if defined LEGACYVOICE (
  echo [start-k15] the PRE-MIGRATION voice supervisor holds the lock and cannot
  echo [start-k15] relaunch - close the "K15 voice" window, then run this again
) else (
  set "RELOADED=1"
  call :reload voice_agent.py "voice agent" voice
)

rem Pause ONLY on a reload (the double-click case); cannot fire at boot.
if defined RELOADED (
  echo.
  echo [start-k15] reloaded - each supervisor relaunches its agent within ~10s.
  echo.
  pause
)
exit /b 0

:supervised
rem exit 0 = a supervisor window holds this lock, 1 = nobody does.
setlocal
set "FREE="
2>nul (9>"%~1" (set FREE=1))
if defined FREE (endlocal & exit /b 1)
endlocal & exit /b 0

:reload
rem %~1 = agent script, %~2 = label, %~3 = lane. Kills the python running that
rem script. Name-filtered to python* so the matching powershell - whose command
rem line contains the script name - cannot match itself. Exit code = kills.
powershell -NoProfile -Command "$p = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*%~1*' }); $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; exit $p.Count"
if errorlevel 1 (
  echo [start-k15] stopped the %~2 - its supervisor will relaunch it
  python "%~dp0events.py" emit supervisor lane_reloaded what=%~3 killed=1 >nul 2>&1
) else (
  echo [start-k15] %~2 was already down - its supervisor will bring it back
  python "%~dp0events.py" emit supervisor lane_reloaded what=%~3 killed=0 >nul 2>&1
)
goto :eof
