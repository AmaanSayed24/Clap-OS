@echo off
:: Arcane OS — Silent Background Launcher
:: Called by Windows Task Scheduler at login
cd /d "%~dp0"
call .\.venv\Scripts\activate.bat
start "" /MIN /HIGH .\.venv\Scripts\pythonw.exe arcane_server.py --headless
