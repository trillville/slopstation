@echo off
rem Voice agent supervisor - close this window to stop the voice overlay.
rem Voice is never load-bearing: the chord listener is a separate process on
rem system python and does not care what happens in here.
rem First run: creates the venv + installs pinned deps (internet, ~2 min).
rem Args pass through: Start-Voice.bat --dry-run  runs the whole agent
rem with side effects logged instead of executed.
rem
rem SINGLE INSTANCE: an fd-9 handle on a lock file is held for this window's
rem lifetime, so a second launch (a manual test run while the startup copy is
rem live, or vice versa) bounces instead of fighting over the mic and double-
rem dispatching. The handle self-releases on ANY exit - crash, Ctrl+C, window
rem close - so a dead supervisor can never wedge the next one.
cd /d "%~dp0"
set "STARTED="
2>nul ( 9>"%TEMP%\couch-voice-supervisor.lock" ( set STARTED=1 & call :main %* ) )
if not defined STARTED (
  echo [supervisor] another Start-Voice window is already running - close it first
  pause
  exit /b 1
)
exit /b

:main
if not exist ".venv\Scripts\python.exe" (
  echo [supervisor] first run: creating venv...
  python -m venv .venv || (echo [supervisor] venv create failed - is python on PATH? & pause & exit /b 1)
)

rem Mic array reboot-hang workaround; no-ops until xvf_host is installed here
if exist "xvf_host\xvf_host.exe" (
  echo [supervisor] rebooting mic array - reboot-hang workaround
  "xvf_host\xvf_host.exe" REBOOT 1
  timeout /t 3 /nobreak >nul
)

python ..\events.py emit supervisor start what=voice >nul 2>&1
:agent
rem Dependency gate, INSIDE the restart loop and content-addressed.
rem
rem The sentinel is a COPY of requirements.txt, not the word "ok", so the
rem question it answers is "are the installed pins the ones on disk?" rather
rem than "did an install ever succeed?". A git pull that changes pins
rem therefore installs itself on the next agent launch, which is the whole
rem promise of Start-K15.bat being the one thing to run after a pull.
rem
rem It lives here rather than at :main because Start-K15.bat reloads by
rem killing the AGENT - the supervisor loops back to :agent and never
rem revisits :main, so a gate up there is unreachable on every path except a
rem cold start. That is exactly how the OTel pins shipped and did not install.
fc /b requirements.txt ".venv\deps-ok" >nul 2>&1
if errorlevel 1 (
  rem NO PARENTHESES in echo text inside a parenthesised block: an unescaped
  rem ')' closes the block early and cmd dies with "... was unexpected at this
  rem time", taking the whole supervisor with it before it starts.
  echo [supervisor] pins changed - installing pinned deps, takes a minute or two...
  .venv\Scripts\python -m pip install -r requirements.txt || (echo [supervisor] pip install failed - fix network and rerun & pause & exit /b 1)
  rem Sentinel written only AFTER pip succeeds: a half-built venv (network
  rem died mid-install) must retry, not skip forever.
  copy /y requirements.txt ".venv\deps-ok" >nul
  python ..\events.py emit supervisor deps_installed what=voice >nul 2>&1
)
.venv\Scripts\python voice_agent.py %*
rem Capture the exit code FIRST: every command after this resets %errorlevel%,
rem so reading it twice is reading the echo's success the second time.
set "CODE=%errorlevel%"
echo [supervisor] voice agent exited (code %CODE%) - restarting in 10s
echo [%date% %time%] [voice-supervisor] agent exited (code %CODE%) - restarting in 10s>> ..\couch.log
rem Structured twin of the line above, so a crash LOOP is alertable rather
rem than just recorded. Output suppressed - the console already has it.
python ..\events.py emit supervisor restart what=voice code=%CODE% --level warn >nul 2>&1
timeout /t 10 /nobreak >nul
goto agent
