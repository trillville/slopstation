@echo off
rem Voice agent supervisor - close this window to stop the voice overlay.
rem Voice is never load-bearing: the chord listener is a separate process on
rem system python and does not care what happens in here.
rem First run: creates the venv + installs pinned deps (internet, ~2 min).
rem Args pass through: Start-Voice.bat --dry-run  runs the whole agent
rem with side effects logged instead of executed.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [supervisor] first run: creating venv + installing pinned deps...
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)

rem Mic array reboot-hang workaround (C0 places xvf_host here; no-ops until then)
if exist "xvf_host\xvf_host.exe" (
  echo [supervisor] rebooting mic array - reboot-hang workaround
  "xvf_host\xvf_host.exe" REBOOT 1
  timeout /t 3 /nobreak >nul
)

:agent
.venv\Scripts\python voice_agent.py %*
echo [supervisor] voice agent exited (code %errorlevel%) - restarting in 10s
echo [%date% %time%] [voice-supervisor] agent exited (code %errorlevel%) - restarting in 10s>> ..\couch.log
timeout /t 10 /nobreak >nul
goto agent
