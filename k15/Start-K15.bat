@echo off
rem The one Startup shortcut for the whole K15 (shell:startup -> this file):
rem launches both supervisors, each in its own minimized window, then exits.
rem Close a window to stop that lane.
rem   listener = the chord lane, load-bearing, system python
rem   voice    = the overlay, its own venv, allowed to die
rem
rem   Start-K15.bat            start whatever isn't already up
rem   Start-K15.bat --restart  reload both agents (run this after a git pull:
rem                            a live agent holds the OLD modules in memory)
rem
rem Safe to run twice. Both supervisors are single-instance (an fd-9 handle on
rem a lock file held for the window's lifetime), so this probes those same
rem locks and starts only the lanes that are actually down - a second run is a
rem no-op instead of two windows sitting on an "already running" pause.
rem
rem --restart kills the AGENTS, never the supervisor windows. Each supervisor
rem relaunches its own agent after its 10s backoff, which is the reload. Two
rem reasons it must be this way and not a window kill:
rem   - couch.py runs in its own console (CREATE_NEW_CONSOLE), so killing the
rem     listener's window would leave a live session's watch loop orphaned,
rem     and the replacement supervisor's `couch.py reconcile` would adopt that
rem     same session - two watch loops racing one teardown.
rem   - reconcile runs once at :main, OUTSIDE the restart loop, so bouncing
rem     the agent alone can't re-trigger it. A live session is undisturbed.
setlocal
set "LISTLOCK=%TEMP%\couch-listener-supervisor.lock"
set "VOICELOCK=%TEMP%\couch-voice-supervisor.lock"

if /i "%~1"=="--restart" goto restart
if /i "%~1"=="-r" goto restart
if not "%~1"=="" (
  echo usage: Start-K15.bat [--restart]
  exit /b 2
)
goto start

:restart
set "RELOADED=1"
echo [start-k15] reloading agents - supervisors relaunch them after ~10s
call :reload voice_agent.py "voice agent"
call :reload chord_listener.py "chord listener"

:start
set "STARTED="
call :supervised "%LISTLOCK%"
if errorlevel 1 (
  set "STARTED=1"
  start "K15 chord listener" /min /d "%~dp0" Start-Listener.bat
) else (
  echo [start-k15] chord lane already supervised
)
call :supervised "%VOICELOCK%"
if errorlevel 1 (
  set "STARTED=1"
  start "K15 voice" /min /d "%~dp0voice" Start-Voice.bat
) else (
  echo [start-k15] voice lane already supervised
)
rem Nothing started and nothing reloaded = a double-click that did nothing.
rem Hold the window open and say so: this path is the one that looks like it
rem worked, and it's exactly what a post-git-pull double-click lands on.
rem It cannot fire at boot (both lanes are down then, so both get started).
if not defined STARTED if not defined RELOADED (
  echo.
  echo [start-k15] both lanes were already running - nothing to do.
  echo [start-k15] if you just pulled, the agents are still on the OLD code:
  echo [start-k15]     run  Start-K15.bat --restart   ^(or double-click Restart-K15.bat^)
  echo.
  pause
)
exit /b 0

:supervised
rem exit 0 = a supervisor window holds this lock, 1 = nobody does. Same fd-9
rem probe the supervisors use to bounce each other, so the two can't disagree
rem about what "running" means.
setlocal
set "FREE="
2>nul (9>"%~1" (set FREE=1))
if defined FREE (endlocal & exit /b 1)
endlocal & exit /b 0

:reload
rem %~1 = agent script name, %~2 = label. Kills the python running that script
rem so its supervisor relaunches it on fresh code; a lane that is down matches
rem nothing and just falls through to :start. Name-filtered to python so the
rem powershell process running this line - whose own command line necessarily
rem contains the script name - can never match itself.
powershell -NoProfile -Command "$p = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*%~1*' }); $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; exit $p.Count"
if errorlevel 1 (
  echo [start-k15] stopped the %~2 - its supervisor will relaunch it
) else (
  echo [start-k15] no %~2 running
)
goto :eof
