@echo off
title QC360 AI - finish the report
cd /d "%~dp0.."
rem  After the content review in a Claude chat: save Claude's answers.json into the report folder,
rem  then double-click this file (or drop the report folder on it).
python -X utf8 -m beta.local finish %1
echo.
pause
