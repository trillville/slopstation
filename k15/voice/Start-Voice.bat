@echo off
rem Voice agent supervisor - close this window to stop the voice overlay.
rem First run: creates the venv + installs pinned deps (internet, ~2 min).
rem Args pass through: Start-Voice.bat --dry-run
rem SINGLE INSTANCE: an fd-9 handle on a lock file, held for this window's
rem lifetime, bounces a second launch instead of fighting over the mic. It
rem self-releases on ANY exit, so a dead supervisor can't wedge the next one.
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
rem Dependency gate. The sentinel is a COPY of requirements.txt, so a pull that
rem changes pins installs itself on the next agent launch. constraints.txt is
rem pip's -c (freezes transitive versions); the gate compares requirements.txt
rem alone, so a constraints-only change must ride a requirements touch.
rem Must be inside the restart loop: Start-K15.bat reloads by killing the
rem AGENT, and the supervisor loops back to :agent without revisiting :main.
fc /b requirements.txt ".venv\deps-ok" >nul 2>&1
if errorlevel 1 (
  rem NO PARENTHESES in echo text inside a parenthesised block: an unescaped
  rem ')' closes the block early and cmd dies before the supervisor starts.
  echo [supervisor] pins changed - installing pinned deps, takes a minute or two...
  .venv\Scripts\python -m pip install -r requirements.txt -c constraints.txt || (echo [supervisor] pip install failed - fix network and rerun & pause & exit /b 1)
  rem Sentinel written only AFTER pip succeeds, so a half-built venv retries.
  copy /y requirements.txt ".venv\deps-ok" >nul
  python ..\events.py emit supervisor deps_installed what=voice >nul 2>&1
)
.venv\Scripts\python voice_agent.py %*
rem Capture the exit code FIRST: every command after this resets %errorlevel%.
set "CODE=%errorlevel%"
echo [supervisor] voice agent exited (code %CODE%) - restarting in 10s
echo [%date% %time%] [voice-supervisor] agent exited (code %CODE%) - restarting in 10s>> ..\couch.log
rem Structured twin of the line above, so a crash LOOP is alertable.
python ..\events.py emit supervisor restart what=voice code=%CODE% --level warn >nul 2>&1
timeout /t 10 /nobreak >nul
goto agent
