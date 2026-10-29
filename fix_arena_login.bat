@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo ============================================================
echo  Capri: Arena login
echo  If the Capri profile already has a live Arena session, nothing changes.
echo  Otherwise: open arena.ai in your usual Chrome, make sure you are
echo  logged in, close Chrome and run this file again.
echo ============================================================
echo.

"%PY%" tools\seed_browser_profile.py --force --close-chrome
if errorlevel 3 goto already
if errorlevel 1 goto fail

echo.
echo Checking what the Capri browser sees...
"%PY%" tools\diagnose_arena_browser.py

echo.
echo If "Asks for login: NO" above - start Capri (run.bat).
echo If "YES" - log in on arena.ai in your usual Chrome, close it, run this file again.
pause
exit /b 0

:already
echo Arena login is already in the Capri profile - start Capri (run.bat).
pause
exit /b 0

:fail
echo.
echo Failed - see the text above.
pause
exit /b 1
