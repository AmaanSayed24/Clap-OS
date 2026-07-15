# Arcane OS Lifecycle Bootstrapper v3.0
# -----------------------------------------------
# Launches the Arcane backend with HIGH process priority for audio thread protection.
# This prevents CPU starvation during burst app deployment (Claude, Brave, Antigravity IDE).
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

# 3. Fire up the backend with elevated process priority
Write-Host "[Arcane] Launching acoustic processing core with HIGH priority..." -ForegroundColor Green
$proc = Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList "arcane_server.py --headless" `
    -WindowStyle Hidden -PassThru

# 4. Elevate to HIGH_PRIORITY_CLASS — shields the audio stream loop from
#    CPU starvation when Electron/Chromium apps burst during workspace deployment
try {
    $proc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::High
    Write-Host "[Arcane] Process elevated to HIGH priority (PID: $($proc.Id))" -ForegroundColor Green
} catch {
    Write-Host "[Arcane] Warning: Could not elevate process priority. Running at normal priority." -ForegroundColor Yellow
}

Write-Host "[Arcane] Audio transient loop processing active. Standing by for handshakes..." -ForegroundColor Green
Write-Host "[Arcane] Press Ctrl+C or close this window to terminate." -ForegroundColor DarkGray

# 5. Keep the bootstrapper alive so it can be used to monitor/kill the background process
try {
    $proc.WaitForExit()
} catch {
    # User closed the window — process continues in background
}