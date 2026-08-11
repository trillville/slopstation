@echo off
rem The one Startup shortcut for the whole K15 (shell:startup -> this file):
rem launches both supervisors, each in its own minimized window, then exits.
rem Close a window to stop that lane. Both supervisors are single-instance
rem (fd-9 lock), so running this again - or a stray old per-lane shortcut -
rem bounces instead of double-running anything.
rem   listener = the chord lane, load-bearing, system python
rem   voice    = the overlay, its own venv, allowed to die
start "K15 chord listener" /min /d "%~dp0" Start-Listener.bat
start "K15 voice" /min /d "%~dp0voice" Start-Voice.bat
