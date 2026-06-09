# Sofia Monitor — Nightly Review Trigger
# Llama al endpoint /api/nightly/run de Sofia para disparar la revisión nocturna.
# Registrar en Task Scheduler para ejecutar a medianoche.
#
# Para instalar la tarea automáticamente ejecuta como Administrador:
#   PowerShell -ExecutionPolicy Bypass -File D:\sofia\backend\app\scripts\nightly_trigger.ps1 -Install
#
# Para ejecutar manualmente:
#   PowerShell -ExecutionPolicy Bypass -File D:\sofia\backend\app\scripts\nightly_trigger.ps1

param(
    [switch]$Install
)

$SOFIA_URL   = "http://localhost:5180"
$TASK_NAME   = "Sofia-NightlyReview"
$SCRIPT_PATH = $MyInvocation.MyCommand.Path

# ── Install mode: register in Task Scheduler ─────────────────────────────────
if ($Install) {
    Write-Host "Registrando tarea '$TASK_NAME' en el Programador de tareas de Windows..." -ForegroundColor Cyan

    $action  = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$SCRIPT_PATH`""

    # Runs every day at 00:05 (5 min past midnight to let the system settle)
    $trigger = New-ScheduledTaskTrigger -Daily -At "00:05"

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest

    Register-ScheduledTask `
        -TaskName   $TASK_NAME `
        -Action     $action `
        -Trigger    $trigger `
        -Settings   $settings `
        -Principal  $principal `
        -Description "Sofia Monitor: ejecuta la revision nocturna de errores con Devin CLI." `
        -Force | Out-Null

    Write-Host "Tarea '$TASK_NAME' registrada correctamente." -ForegroundColor Green
    Write-Host "Se ejecutará todos los días a las 00:05."
    exit 0
}

# ── Run mode: call the API ────────────────────────────────────────────────────
$log = "D:\sofia\backend\logs\nightly_trigger.log"
$ts  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

function Log($msg) {
    $line = "$ts  $msg"
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

Log "Nightly trigger started."

# Wait up to 30s for Sofia to be reachable
$attempts = 0
$ready    = $false
while ($attempts -lt 6) {
    try {
        $ping = Invoke-RestMethod -Uri "$SOFIA_URL/api/ping" -TimeoutSec 5 -ErrorAction Stop
        if ($ping.status -eq "ok") { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
    $attempts++
}

if (-not $ready) {
    Log "ERROR: Sofia no responde en $SOFIA_URL — abortando."
    exit 1
}

try {
    $body    = '{"force": true, "since_hours": 24}'
    $headers = @{ "Content-Type" = "application/json" }
    $resp    = Invoke-RestMethod `
        -Method  POST `
        -Uri     "$SOFIA_URL/api/nightly/run" `
        -Body    $body `
        -Headers $headers `
        -TimeoutSec 30 `
        -ErrorAction Stop

    Log "OK: $($resp.message)"
    exit 0
} catch {
    Log "ERROR llamando a /api/nightly/run: $_"
    exit 1
}
