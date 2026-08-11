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
rem Nothing to remember and no flags: at boot it starts both, after a pull it
rem reloads both, and a pointless double-click costs 10s of nothing. Whether a
rem lane is up is read from the fd-9 lock its supervisor holds for the window's
rem lifetime - the same probe the supervisors use to bounce each other, so the
rem two can never disagree about what "running" means.
rem
rem Killing agents rather than supervisor windows is load-bearing, not taste:
rem couch.py runs in its own console (CREATE_NEW_CONSOLE), so killing the
rem listener's WINDOW would orphan a live session's watch loop, and the
rem replacement supervisor's `couch.py reconcile` would adopt that same session
rem - two watch loops racing one teardown. reconcile runs at :main, OUTSIDE the
rem restart loop, so bouncing the agent alone can't re-trigger it. A live
rem session is undisturbed: the relaunched listener finds the Puck claimed and
rem stands by at 1Hz until the session ends.
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
if errorlevel 1 (
  echo [start-k15] stopped the %~2 - its supervisor will relaunch it
  python "%~dp0events.py" emit supervisor lane_reloaded what=%~3 killed=1 >nul 2>&1
) else (
  echo [start-k15] %~2 was already down - its supervisor will bring it back
  python "%~dp0events.py" emit supervisor lane_reloaded what=%~3 killed=0 >nul 2>&1
)
goto :eof
