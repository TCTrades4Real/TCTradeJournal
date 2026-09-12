@echo off
cd /d "%~dp0"
venv\Scripts\python.exe import_tradezero.py >> logs\import_tradezero.log 2>&1
