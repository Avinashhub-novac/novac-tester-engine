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
set /p STAGE= Stage - beta, alpha, gold, sb or custom  [press Enter for beta]: 
set CHECKS=
if /i "%STAGE%"=="custom" (
  echo  Checks: navigation, buttons, assessment, screen_number, seekbar, language, transcript, content,
  echo          logic, consistency, visual, restrictions, swipe, completion, style_guide
  set /p CHECKS= Checks to run, separated by commas  [press Enter for all]: 
)
echo.
if defined CHECKS (
  python -X utf8 -m beta.local test %* --stage %STAGE% --checks "%CHECKS%"
) else (
  python -X utf8 -m beta.local test %* --stage %STAGE%
)
echo.
pause
