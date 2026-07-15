@echo off
:: Arcane OS — Silent Background Launcher
:: Called by Windows Task Scheduler at login
cd /d "%~dp0"
call .\.venv\Scripts\activate.bat
start /B /HIGH python arcane_server.py --headless
