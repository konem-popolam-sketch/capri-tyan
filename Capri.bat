@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Capri

set "PY="
for %%v in (3.13 3.12 3.11 3.14) do if not defined PY py -%%v -c "" >nul 2>nul && set "PY=py -%%v"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

%PY% -c "import PySide6, vosk, sounddevice, edge_tts" >nul 2>nul
if errorlevel 1 (
  echo [Capri] First run: installing libraries and the speech model, it takes a few minutes.
  echo.
  %PY% tools\install.py
  if errorlevel 1 goto fail
  echo.
)

set "PYW="
for %%v in (3.13 3.12 3.11 3.14) do if not defined PYW pyw -%%v -c "" >nul 2>nul && set "PYW=pyw -%%v"
if not defined PYW where pyw >nul 2>nul && set "PYW=pyw"
if not defined PYW set "PYW=pythonw"
start "" %PYW% main.py
exit /b 0

:nopython
echo.
echo [Capri] Python not found. Install Python 3.13:
echo         https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe
echo         Tick "Add python.exe to PATH" during setup, then run Capri.bat again.
pause
exit /b 1

:fail
echo.
echo [Capri] Installation failed - see the text above.
pause
exit /b 1
