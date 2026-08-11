@echo off
rem What to double-click after a git pull. A double-clicked .bat can't carry
rem arguments, so this is Start-K15.bat's --restart path in a file of its own:
rem both agents are killed and their supervisors relaunch them on the new code
rem after a ~10s backoff. Windows stay up, and a live session keeps its watch
rem loop (see the reasoning in Start-K15.bat).
call "%~dp0Start-K15.bat" --restart
echo.
pause
