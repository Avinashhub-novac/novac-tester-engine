@echo off
title QC360 AI - set up this PC
cd /d "%~dp0.."
echo.
echo  QC360 AI - one-time set-up of this PC
echo  ======================================
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo  Python is not installed.
  echo  Install Python 3.13 from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" on the first screen. Then run this file again.
  pause
  exit /b 1
)
echo  1/3  Python packages ...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r pc\requirements.txt
if errorlevel 1 ( echo  Python packages could not be installed. & pause & exit /b 1 )
echo  2/3  Browser engine (backup for when Google Chrome is missing) ...
python -m playwright install chromium
echo  3/3  Google Chrome, LibreOffice (storyboards), Poppler (SB pages), FFmpeg (videos) ...
where winget >nul 2>nul
if errorlevel 1 (
  echo  winget is not available: install these by hand ^(see README_PC.md^):
  echo  Google Chrome, LibreOffice, Poppler for Windows, FFmpeg
) else (
  winget install -e --id Google.Chrome --silent --accept-package-agreements --accept-source-agreements
  winget install -e --id TheDocumentFoundation.LibreOffice --silent --accept-package-agreements --accept-source-agreements
  winget install -e --id oschwartz10612.Poppler --silent --accept-package-agreements --accept-source-agreements
  winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
)
echo.
python -X utf8 -m beta.local setup
echo.
echo  If something says MISSING, close this window, open it again (new installs need a fresh window)
echo  and run setup.bat once more.
pause
