@echo off
title TC Trade Journal Dashboard
cd /d "%~dp0"

set "VENV_PY=%~dp0venv\Scripts\python.exe"
if not exist "%VENV_PY%" set "VENV_PY=python"

"%VENV_PY%" launch_dashboard.py
set EXIT_CODE=%errorlevel%

if %EXIT_CODE% neq 0 echo.
if %EXIT_CODE% neq 0 echo launch_dashboard.py exited with an error (code %EXIT_CODE%)
if %EXIT_CODE% neq 0 pause
