@echo off
title QC360 AI - make the offline kit
cd /d "%~dp0.."
echo.
echo  Run this once on a PC WITH internet. It downloads Python, Google Chrome, LibreOffice,
echo  Poppler, FFmpeg and the Python packages into pc\offline_kit.
echo  Then copy the whole tester folder to the other PCs and run pc\setup.bat there.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "pc\offline_kit.ps1" -Make
echo.
pause
