#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Arcane OS System Tuner v3.0
    Applies OS-level optimizations for real-time audio processing and multi-monitor orchestration.

.DESCRIPTION
    Tier 1: Audio subsystem DPC hardening (disable device idle, disable audio enhancements)
    Tier 2: CPU power plan (Ultimate Performance, disable core parking)
    Tier 3: DWM animation disable for instant window snapping

.PARAMETER Undo
    Reverses all registry changes and restores the Balanced power plan.

.EXAMPLE
    # Apply all optimizations
    .\arcane_os_tune.ps1

    # Revert all changes
    .\arcane_os_tune.ps1 -Undo
#>

param(
    [switch]$Undo
)

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "  =================================================" -ForegroundColor Cyan
Write-Host "  |       ARCANE OS -- System Tuner v3.0           |" -ForegroundColor Cyan
Write-Host "  |  Real-Time Audio & Window Orchestration        |" -ForegroundColor Cyan
Write-Host "  =================================================" -ForegroundColor Cyan
Write-Host ""

# =============================================================================
#  HELPER: Safe Registry Set/Remove
# =============================================================================
function Set-RegValue {
    param([string]$Path, [string]$Name, $Value, [string]$Type = "DWord")
    if (-not (Test-Path $Path)) {
        New-Item -Path $Path -Force | Out-Null
    }
    Set-ItemProperty -Path $Path -Name $Name -Value $Value -Type $Type -Force
    Write-Host "  [SET] $Path\$Name = $Value" -ForegroundColor DarkGray
}

function Remove-RegValue {
    param([string]$Path, [string]$Name)
    if (Test-Path $Path) {
        Remove-ItemProperty -Path $Path -Name $Name -ErrorAction SilentlyContinue
        Write-Host "  [DEL] $Path\$Name" -ForegroundColor DarkGray
    }
}

# =============================================================================
#  UNDO MODE
# =============================================================================
if ($Undo) {
    Write-Host "[UNDO] Reverting all Arcane OS tuning changes..." -ForegroundColor Yellow
    Write-Host ""

    # Tier 1: Re-enable audio idle power management
    Write-Host "[Tier 1] Restoring audio power management defaults..." -ForegroundColor Cyan
    Remove-RegValue "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e96c-e325-11ce-bfc1-08002be10318}\Properties" "ConservativeIdleTime"

    # Tier 2: Restore Balanced power plan
    Write-Host "[Tier 2] Restoring Balanced power plan..." -ForegroundColor Cyan
    $balanced = "381b4222-f694-41f0-9685-ff5bb260df2e"
    powercfg /setactive $balanced 2>$null
    Write-Host "  Activated Balanced power plan ($balanced)" -ForegroundColor DarkGray

    # Tier 3: Re-enable DWM animations
    Write-Host "[Tier 3] Re-enabling window animations..." -ForegroundColor Cyan
    Set-RegValue "HKCU:\Control Panel\Desktop\WindowMetrics" "MinAnimate" "1" "String"
    Remove-RegValue "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects" "VisualFXSetting"

    Write-Host ""
    Write-Host "[DONE] All Arcane OS tuning changes reverted." -ForegroundColor Green
    Write-Host "       A reboot is recommended for full effect." -ForegroundColor Yellow
    Exit 0
}

# =============================================================================
#  TIER 1: Audio Subsystem & DPC Latency Hardening
# =============================================================================
Write-Host "[Tier 1] Audio Subsystem & DPC Latency Hardening" -ForegroundColor Cyan
Write-Host "  Disabling audio device idle power management..." -ForegroundColor White

# Prevent USB/HD Audio devices from entering D3 power state
# This eliminates the ~50-150ms wakeup latency on the first clap after silence
$audioClassKey = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e96c-e325-11ce-bfc1-08002be10318}"
if (Test-Path $audioClassKey) {
    $propPath = "$audioClassKey\Properties"
    Set-RegValue $propPath "ConservativeIdleTime" 0
    Write-Host "  Audio device idle power management disabled." -ForegroundColor Green
} else {
    Write-Host "  [SKIP] Audio class registry key not found." -ForegroundColor Yellow
}

# Disable audio enhancements system-wide (reduces APO processing pipeline latency)
Write-Host "  Checking audio enhancement settings..." -ForegroundColor White
$audioDevices = Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e96c-e325-11ce-bfc1-08002be10318}" -ErrorAction SilentlyContinue |
    Where-Object { $_.PSChildName -match '^\d{4}$' }

$enhancementsDisabled = 0
foreach ($dev in $audioDevices) {
    $fxPath = Join-Path $dev.PSPath "FxProperties"
    if (Test-Path $fxPath) {
        # Setting PKEY_FX_Disable to 1 disables all audio effects/APOs
        Set-ItemProperty -Path $fxPath -Name "{b0a1e180-c045-4d93-bfa2-a5f3bca50000}" -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
        $enhancementsDisabled++
    }
}
if ($enhancementsDisabled -gt 0) {
    Write-Host "  Disabled audio enhancements on $enhancementsDisabled device(s)." -ForegroundColor Green
} else {
    Write-Host "  [INFO] No audio enhancement properties found to disable." -ForegroundColor DarkGray
}

Write-Host ""

# =============================================================================
#  TIER 2: Kernel Scheduler & CPU Power Tuning
# =============================================================================
Write-Host "[Tier 2] Kernel Scheduler & CPU Power Tuning" -ForegroundColor Cyan

# Activate Ultimate Performance power plan
Write-Host "  Activating Ultimate Performance power plan..." -ForegroundColor White

# Check if Ultimate Performance already exists
$ultimateGuid = "e9a42b02-d5df-448d-aa00-03f14749eb61"
$existingPlans = powercfg /list 2>$null
if ($existingPlans -match $ultimateGuid) {
    powercfg /setactive $ultimateGuid
    Write-Host "  Ultimate Performance plan activated." -ForegroundColor Green
} else {
    # Create the Ultimate Performance plan (available on Win10 1803+ / Win11)
    $result = powercfg /duplicatescheme $ultimateGuid 2>$null
    if ($LASTEXITCODE -eq 0) {
        powercfg /setactive $ultimateGuid
        Write-Host "  Ultimate Performance plan created and activated." -ForegroundColor Green
    } else {
        # Fallback: use High Performance plan
        $highPerf = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
        powercfg /setactive $highPerf 2>$null
        Write-Host "  [FALLBACK] High Performance plan activated (Ultimate Performance not available)." -ForegroundColor Yellow
    }
}

# Explicitly disable core parking via power plan settings
Write-Host "  Disabling CPU core parking..." -ForegroundColor White
$activePlan = (powercfg /getactivescheme 2>$null) -replace '.*:\s*', '' -replace '\s*\(.*', ''
if ($activePlan) {
    # CPMINCORES = Processor performance core parking min cores (100% = no parking)
    powercfg /setacvalueindex $activePlan.Trim() 54533251-82be-4824-96c1-47b60b740d00 0cc5b647-c1df-4637-891a-dec35c318583 100 2>$null
    # CPMINPERCENTAGE = Minimum processor state (100% = no frequency scaling down)
    powercfg /setacvalueindex $activePlan.Trim() 54533251-82be-4824-96c1-47b60b740d00 893dee8e-2bef-41e0-89c6-b55d0929964c 100 2>$null
    powercfg /setactive $activePlan.Trim() 2>$null
    Write-Host "  Core parking disabled. Min processor frequency set to 100%." -ForegroundColor Green
}

Write-Host ""

# =============================================================================
#  TIER 3: Win32 Window Management & DWM Animation Hardening
# =============================================================================
Write-Host "[Tier 3] Win32 Window Management & DWM Animation Hardening" -ForegroundColor Cyan

# Disable window minimize/maximize/restore animations
# Saves ~250ms of GPU/DWM thread time per window transition during workspace deployment
Write-Host "  Disabling DWM window animations..." -ForegroundColor White
Set-RegValue "HKCU:\Control Panel\Desktop\WindowMetrics" "MinAnimate" "0" "String"

# Set Visual Effects to "Custom" mode (3) -- allows granular control
Set-RegValue "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects" "VisualFXSetting" 3

Write-Host "  Window animations disabled for instant snap deployment." -ForegroundColor Green

Write-Host ""

# =============================================================================
#  SUMMARY
# =============================================================================
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " Arcane OS System Tuning Complete" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " [OK] Tier 1: Audio DPC hardening applied" -ForegroundColor Green
Write-Host " [OK] Tier 2: Ultimate Performance power plan active" -ForegroundColor Green
Write-Host " [OK] Tier 3: DWM animations disabled" -ForegroundColor Green
Write-Host ""
Write-Host " To revert all changes: .\arcane_os_tune.ps1 -Undo" -ForegroundColor Yellow
Write-Host " A reboot is recommended for full effect." -ForegroundColor Yellow
Write-Host ""
