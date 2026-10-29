@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" bb.py
) else (
    python bb.py
)
if errorlevel 1 pause
