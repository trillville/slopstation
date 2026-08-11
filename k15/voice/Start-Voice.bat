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
rem Gate on a sentinel written only after pip succeeds - a half-built venv
rem (network died mid-install) must re-install, not skip forever on "python.exe
rem exists". Bail out loudly on bootstrap failure instead of crash-looping.
if not exist ".venv\deps-ok" (
  echo [supervisor] first run: creating venv + installing pinned deps...
  python -m venv .venv || (echo [supervisor] venv create failed - is python on PATH? & pause & exit /b 1)
  .venv\Scripts\python -m pip install -r requirements.txt || (echo [supervisor] pip install failed - fix network and rerun & pause & exit /b 1)
  echo ok> .venv\deps-ok
)

rem Mic array reboot-hang workaround; no-ops until xvf_host is installed here
if exist "xvf_host\xvf_host.exe" (
  echo [supervisor] rebooting mic array - reboot-hang workaround
  "xvf_host\xvf_host.exe" REBOOT 1
  timeout /t 3 /nobreak >nul
)

python ..\events.py emit supervisor start what=voice >nul 2>&1
:agent
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
