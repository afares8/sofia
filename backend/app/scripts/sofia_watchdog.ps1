# sofia_watchdog.ps1
# -------------------------------------------------------------------
# Run this script as a scheduled task every 2 minutes to make sure
# Sofia Monitor itself is up. If two consecutive ping checks fail,
# Sofia is restarted via start.bat.
#
# Setup (run once, as Administrator):
#   schtasks /Create /TN "Sofia Watchdog" /SC MINUTE /MO 2 ^
#       /TR "powershell -ExecutionPolicy Bypass -File C:\path\to\sofia_watchdog.ps1" ^
#       /RU SYSTEM
# -------------------------------------------------------------------

$SOFIA_URL  = $env:SOFIA_URL
if (-not $SOFIA_URL)  { $SOFIA_URL  = "http://localhost:5180/api/ping" }

$START_BAT  = $env:SOFIA_START_BAT
if (-not $START_BAT)  { $START_BAT  = "D:\sofia\start.bat" }

$STATE_FILE = $env:SOFIA_WATCHDOG_STATE
if (-not $STATE_FILE) { $STATE_FILE = Join-Path $env:TEMP "sofia_watchdog.state" }

function Get-FailCount {
    if (Test-Path $STATE_FILE) {
        try { return [int](Get-Content -Raw -Path $STATE_FILE) } catch { return 0 }
    }
    return 0
}

function Set-FailCount($n) {
    Set-Content -Path $STATE_FILE -Value "$n" -Force
}

$ok = $false
try {
    $resp = Invoke-WebRequest -Uri $SOFIA_URL -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
    if ($resp.StatusCode -lt 500) { $ok = $true }
} catch {
    $ok = $false
}

if ($ok) {
    Set-FailCount 0
    Write-Host "[Sofia watchdog] Healthy - $SOFIA_URL"
    exit 0
}

$fails = (Get-FailCount) + 1
Set-FailCount $fails
Write-Host "[Sofia watchdog] Ping failed ($fails consecutive)"

if ($fails -ge 2) {
    Write-Host "[Sofia watchdog] Restarting Sofia via $START_BAT"
    if (Test-Path $START_BAT) {
        Start-Process -FilePath $START_BAT -WindowStyle Hidden
        Set-FailCount 0
    } else {
        Write-Host "[Sofia watchdog] start.bat not found at $START_BAT"
    }
}
