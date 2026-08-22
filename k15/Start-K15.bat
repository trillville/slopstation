@echo off
rem The one Startup shortcut for the whole K15 (shell:startup -> this file),
rem and the one thing to run after a git pull. Both cases converge on the same
rem state: both lanes running, on the code that is on disk right now.
rem   listener = the chord lane, load-bearing, system python
rem   voice    = the overlay, its own venv, allowed to die
rem
rem Per lane, exactly one rule:
rem   supervisor down -> start it (its agent comes up on current code anyway)
rem   supervisor up   -> kill the AGENT; the supervisor relaunches it on
rem                      current code after its 10s backoff
rem
rem No flags and nothing to remember: at boot it starts both, after a pull it
rem reloads both. Whether a lane is up is read from the fd-9 lock its
rem supervisor holds for the window's lifetime - the same probe the
rem supervisors use to bounce each other, so the two cannot disagree.
rem
rem Killing AGENTS rather than supervisor windows is load-bearing: couch.py
rem runs in its own console, so killing the listener's window would orphan a
rem live session's watch loop, and the replacement supervisor's `couch.py
rem reconcile` would adopt that same session - two watch loops racing one
rem teardown. reconcile runs at :main, outside the restart loop, so bouncing
rem the agent alone cannot re-trigger it.
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

call :supervised "%TEMP%\couch-voice-supervisor.lock"
if errorlevel 1 (
  echo [start-k15] voice lane down - starting it
  python "%~dp0events.py" emit supervisor lane_started what=voice >nul 2>&1
  start "K15 voice" /min /d "%~dp0voice" Start-Voice.bat
) else (
  set "RELOADED=1"
  call :reload voice_agent.py "voice agent" voice
)

rem Pause ONLY when something was reloaded, which is the double-click case: a
rem .bat closes its window the instant it exits, and a reload you can't read
rem looks identical to nothing happening. It cannot fire at boot - both lanes
rem are down then, so both get started and nothing is reloaded.
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
rem %~1 = agent script name, %~2 = label, %~3 = lane. Kills the python running that script
rem so its supervisor relaunches it on fresh code. Name-filtered to python* so
rem the powershell process running the match - whose own command line
rem necessarily contains the script name - can never match itself. The exit
rem code is the number of processes killed, so a stray duplicate agent is swept
rem too and still reports honestly.
powershell -NoProfile -Command "$p = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*%~1*' }); $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; exit $p.Count"
rem Capture the count FIRST - every command after this resets %errorlevel%
rem (the event used to say killed=1 whatever the count was).
set "KILLED=%errorlevel%"
if %KILLED% GEQ 1 (
  echo [start-k15] stopped the %~2 - its supervisor will relaunch it
  python "%~dp0events.py" emit supervisor lane_reloaded what=%~3 killed=%KILLED% >nul 2>&1
) else (
  echo [start-k15] %~2 was already down - its supervisor will bring it back
  python "%~dp0events.py" emit supervisor lane_reloaded what=%~3 killed=0 >nul 2>&1
)
goto :eof
