@echo off
title QC360 AI - test
cd /d "%~dp0.."
if "%~1"=="" (
  echo.
  echo  Drag the course zip onto this file - together with its storyboard if you have one.
  echo  ^(Select both files in Explorer, then drag them onto test.bat.^)
  echo.
  pause
  exit /b 1
)
echo.
set STAGE=beta
set /p STAGE= Stage - beta, alpha, gold or sb  [press Enter for beta]: 
echo.
python -X utf8 -m beta.local test %* --stage %STAGE%
echo.
pause
