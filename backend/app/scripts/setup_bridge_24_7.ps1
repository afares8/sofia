# setup_bridge_24_7.ps1
# Deja los dos puentes Sofia-Devin arrancando solos al encender la PC y
# sobreviviendo al cierre de sesion, sin depender de que alguien haga login.
#
# Hoy ambas tareas son LogonTrigger + LogonType Interactive: tras el reinicio
# programado del 2026-11-08 estuvieron caidas 00:03-15:08 (15 h) y los
# CRITICAL "puente DOWN" no se pudieron entregar porque el que los entrega es
# el propio puente.
#
# Que hace:
#   1. Exporta el XML actual de cada tarea a C:\ProgramData\ServerSofia\tasks-backup-<ts>\
#   2. Re-registra con LogonType S4U (corre sin sesion interactiva), RunLevel
#      Highest solo para el grupo local (ElenaTec debe seguir NO elevado),
#      triggers AtStartup + AtLogOn y reinicio automatico si el proceso falla.
#   3. Arranca ambas tareas y espera /health.
#   4. Si alguna no responde, restaura los XML y relanza las tareas originales.
#
# Uso (CONSOLA ELEVADA):
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup_bridge_24_7.ps1
#   powershell ... -File setup_bridge_24_7.ps1 -DryRun
#
# Requiere autorizacion expresa: reinicia los dos watchers (los workers en
# curso se cierran y el watcher los reabre con la sesion guardada).

param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$User = "SERVER-SOFIA\ahmed"
$Python = "C:\Users\ahmed\apps\sofia\backend\.venv\Scripts\pythonw.exe"
$Script = "C:\Users\ahmed\apps\sofia\backend\app\scripts\sofia_devin_bridge.py"
$PolicyDir = "C:\Users\ahmed\apps\sofia\.devin\sofia-cli"
$WorkDir = "C:\Users\ahmed\apps\sofia\backend"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupDir = "C:\ProgramData\ServerSofia\tasks-backup-$Stamp"

$Tasks = @(
    @{ Name = "Sofia-Devin-AppsPC";  Group = "local";    Port = 5185; RunLevel = "Highest" },
    @{ Name = "Sofia-Devin-ElenaTec"; Group = "elenatec"; Port = 5186; RunLevel = "Limited" }
)

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCtx = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $principalCtx.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: este script necesita una consola elevada (Administrador)." -ForegroundColor Red
    exit 2
}

function Wait-BridgeHealth([int]$Port, [int]$TimeoutSeconds = 120) {
    $url = "http://127.0.0.1:$Port/health"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 5
    }
    return $false
}

Write-Host "[$(Get-Date)] Puentes Sofia-Devin -> 24/7 (S4U + AtStartup)"
if ($DryRun) {
    Write-Host "DRY RUN: no se cambia nada. Se exportarian los XML a $BackupDir"
    foreach ($t in $Tasks) {
        $info = Get-ScheduledTask -TaskName $t.Name
        Write-Host ("  {0}: principal={1} logon={2} runlevel={3} triggers={4}" -f `
            $t.Name, $info.Principal.UserId, $info.Principal.LogonType, `
            $info.Principal.RunLevel, (($info.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ','))
        Write-Host ("     nuevo: logon=S4U runlevel={0} triggers=AtStartup,AtLogOn" -f $t.RunLevel)
    }
    exit 0
}

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
foreach ($t in $Tasks) {
    $xmlPath = Join-Path $BackupDir ("{0}.xml" -f $t.Name)
    Export-ScheduledTask -TaskName $t.Name | Set-Content -Path $xmlPath -Encoding Unicode
    Write-Host "[$(Get-Date)] Backup $($t.Name) -> $xmlPath"
}

$startup = New-ScheduledTaskTrigger -AtStartup
$logon = New-ScheduledTaskTrigger -AtLogOn -User $User
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowHardTerminate

foreach ($t in $Tasks) {
    $action = New-ScheduledTaskAction -Execute $Python `
        -Argument ('"{0}" watch --group {1} --policy-dir "{2}"' -f $Script, $t.Group, $PolicyDir) `
        -WorkingDirectory $WorkDir
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel $t.RunLevel

    Write-Host "[$(Get-Date)] Re-registrando $($t.Name) (S4U, $($t.RunLevel))..."
    schtasks /End /TN $t.Name 2>$null | Out-Null
    Start-Sleep -Seconds 2
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger @($startup, $logon) `
        -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $t.Name
}

$failed = @()
foreach ($t in $Tasks) {
    Write-Host "[$(Get-Date)] Esperando health de $($t.Name) en $($t.Port)..."
    if (-not (Wait-BridgeHealth -Port $t.Port -TimeoutSeconds 150)) {
        $failed += $t
    } else {
        Write-Host "[$(Get-Date)] OK: $($t.Name) responde en 127.0.0.1:$($t.Port)" -ForegroundColor Green
    }
}

if ($failed.Count -gt 0) {
    Write-Host "[$(Get-Date)] FALLO: $($failed.Name -join ', ') no respondio. Restaurando XML original..." -ForegroundColor Red
    foreach ($t in $failed) {
        $xmlPath = Join-Path $BackupDir ("{0}.xml" -f $t.Name)
        schtasks /End /TN $t.Name 2>$null | Out-Null
        schtasks /Create /TN $t.Name /XML $xmlPath /F | Out-Null
        schtasks /Run /TN $t.Name 2>$null | Out-Null
    }
    Write-Host "Rollback aplicado. XML original en $BackupDir"
    exit 1
}

Write-Host "[$(Get-Date)] Listo: ambos puentes arrancan al encender y corren sin login." -ForegroundColor Green
Write-Host "Reversion: schtasks /Create /TN <tarea> /XML $BackupDir\<tarea>.xml /F"
exit 0
