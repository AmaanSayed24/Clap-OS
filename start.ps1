# Arcane OS Lifecycle Bootstrapper
# -----------------------------------------------
Clear-Host
Write-Host "[Arcane] Initializing headless repository environment..." -ForegroundColor Cyan

# 1. Pull down any potential updates directly from your origin main node
if (git rev-parse --is-inside-work-tree 2>$null) {
    Write-Host "[Arcane] Syncing remote matrices with AmaanSayed24/Clap-OS..." -ForegroundColor DarkGray
    git pull origin main --quiet
}

# 2. Automatically locate and map your virtual environment path
if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
} else {
    Write-Host "[Error] Virtual environment binary link not found. Run pip setup routines." -ForegroundColor Red
    Exit 1
}

# 3. Fire up the background listener in an isolated headless state
Write-Host "[Arcane] Audio transient loop processing active. Standing by for handshakes..." -ForegroundColor Green
python arcane_server.py --headless