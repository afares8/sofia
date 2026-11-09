# setup_bridge_24_7.ps1
# Deja los puentes Sofia-Devin arrancando solos al encender la PC y
# sobreviviendo al cierre de sesion, sin depender de que alguien haga login.
#
# Hoy ambas tareas son LogonTrigger + LogonType Interactive: tras el reinicio
# programado del 2026-11-08 estuvieron caidas 00:03-15:08 (15 h) y los
# CRITICAL "puente DOWN" no se pudieron entregar porque el que los entrega es
# el propio puente.
#
# Que hace:
#   1. Preflight: espera a que el puente no tenga un turno en vuelo
#      (`inflight`) ni estado `uncertain`. Reiniciar con un turno enviado deja
#      el puente en 503 transitorio y el script lo confundiria con una caida.
#   2. Exporta el XML actual de cada tarea a C:\ProgramData\ServerSofia\tasks-backup-<ts>\
#   3. Re-registra con LogonType S4U (corre sin sesion interactiva), RunLevel
#      Highest solo para el grupo local (ElenaTec debe seguir NO elevado),
#      triggers AtStartup + AtLogOn y reinicio automatico si el proceso falla.
#   4. Arranca ambas tareas y espera /health.
#   5. Si alguna no responde, guarda diagnostico (estado, query de la tarea y
#      log de salida del puente) y restaura los XML originales.
#
# Uso (CONSOLA ELEVADA):
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup_bridge_24_7.ps1
#   ... -DryRun                      # solo muestra que haria
#   ... -Only Sofia-Devin-ElenaTec   # solo una tarea (o "local"/"elenatec")
#   ... -WaitIdleMinutes 10          # espera maxima a que no haya turno en vuelo
#
# Requiere autorizacion expresa: reinicia los watchers (los workers en curso
# se cierran y el watcher los reabre con la sesion guardada).

param(
    [switch]$DryRun,
    [string]$Only = "",
    [int]$WaitIdleMinutes = 5
)

$ErrorActionPreference = "Stop"
$User = "SERVER-SOFIA\ahmed"
$Python = "C:\Users\ahmed\apps\sofia\backend\.venv\Scripts\pythonw.exe"
$Script = "C:\Users\ahmed\apps\sofia\backend\app\scripts\sofia_devin_bridge.py"
$PolicyDir = "C:\Users\ahmed\apps\sofia\.devin\sofia-cli"
$WorkDir = "C:\Users\ahmed\apps\sofia\backend"
$StateRoot = "C:\Users\ahmed\apps\sofia\backend\data\devin-desktop"
$ExitLog = Join-Path $env:TEMP "sofia_devin_bridge-exit.log"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupDir = "C:\ProgramData\ServerSofia\tasks-backup-$Stamp"

$AllTasks = @(
    @{ Name = "Sofia-Devin-AppsPC";  Group = "local";    Port = 5185; RunLevel = "Highest" },
    @{ Name = "Sofia-Devin-ElenaTec"; Group = "elenatec"; Port = 5186; RunLevel = "Limited" }
)

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCtx = New-Object Security.Principal.WindowsPrincipal($id)
# -DryRun solo lee: se permite sin elevacion para previsualizar.
if (-not $DryRun -and -not $principalCtx.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: este script necesita una consola elevada (Administrador)." -ForegroundColor Red
    exit 2
}

$Tasks = $AllTasks | Where-Object {
    -not $Only -or $_.Name -eq $Only -or $_.Group -eq $Only
}
if ($Tasks.Count -eq 0) {
    Write-Host "ERROR: -Only '$Only' no coincide con ninguna tarea." -ForegroundColor Red
    exit 2
}

function Get-BridgeState([string]$Group) {
    $path = Join-Path $StateRoot "$Group\state.json"
    if (-not (Test-Path $path)) { return $null }
    try { return (Get-Content $path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Test-BridgeIdle([string]$Group) {
    $s = Get-BridgeState $Group
    if ($null -eq $s) { return $true }
    return (-not $s.inflight) -and (-not $s.uncertain)
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

function Save-Diagnostics($Task) {
    try {
        $name = $Task.Name
        $group = $Task.Group
        schtasks /Query /TN $name /V /FO LIST 2>&1 |
            Out-File -FilePath (Join-Path $BackupDir "$name-query.txt") -Encoding utf8
        $statePath = Join-Path $StateRoot "$group\state.json"
        if (Test-Path $statePath) {
            Copy-Item $statePath (Join-Path $BackupDir "$group-state.json") -Force
        }
        if (Test-Path $ExitLog) {
            Get-Content $ExitLog -Tail 20 |
                Out-File -FilePath (Join-Path $BackupDir "bridge-exit.log") -Encoding utf8
        }
        Write-Host "  diagnostico guardado en $BackupDir"
    } catch {
        Write-Host ("  no se pudo guardar diagnostico: " + $_.Exception.Message)
    }
}

function Restore-OriginalTasks([string[]]$Names) {
    foreach ($name in $Names) {
        $xmlPath = Join-Path $BackupDir ("{0}.xml" -f $name)
        if (-not (Test-Path $xmlPath)) { continue }
        schtasks /End /TN $name 2>$null | Out-Null
        schtasks /Create /TN $name /XML $xmlPath /F | Out-Null
        schtasks /Run /TN $name 2>$null | Out-Null
        Write-Host "[$(Get-Date)] Restaurada $name desde $xmlPath"
    }
}

Write-Host "[$(Get-Date)] Puentes Sofia-Devin -> 24/7 (S4U + AtStartup)"
Write-Host ("Tareas seleccionadas: {0}" -f (($Tasks | ForEach-Object { $_.Name }) -join ', '))

if ($DryRun) {
    Write-Host "DRY RUN: no se cambia nada. Se exportarian los XML a $BackupDir"
    foreach ($t in $Tasks) {
        $info = Get-ScheduledTask -TaskName $t.Name
        Write-Host ("  {0}: principal={1} logon={2} runlevel={3} triggers={4}" -f `
            $t.Name, $info.Principal.UserId, $info.Principal.LogonType, `
            $info.Principal.RunLevel, (($info.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ','))
        Write-Host ("     nuevo: logon=S4U runlevel={0} triggers=AtStartup,AtLogOn" -f $t.RunLevel)
        Write-Host ("     idle={0}" -f (Test-BridgeIdle $t.Group))
    }
    exit 0
}

# --- Preflight: no reiniciar un puente con un turno enviado -----------------
$deadline = (Get-Date).AddMinutes($WaitIdleMinutes)
while ((Get-Date) -lt $deadline) {
    $busy = $Tasks | Where-Object { -not (Test-BridgeIdle $_.Group) }
    if ($busy.Count -eq 0) { break }
    Write-Host ("[$(Get-Date)] esperando turno en vuelo de: {0}..." -f (($busy | ForEach-Object { $_.Name }) -join ', '))
    Start-Sleep -Seconds 10
}
$busy = $Tasks | Where-Object { -not (Test-BridgeIdle $_.Group) }
if ($busy.Count -gt 0) {
    Write-Host ("ERROR: {0} sigue con un turno en vuelo. No se toca nada; " -f (($busy | ForEach-Object { $_.Name }) -join ', ')) -ForegroundColor Red
    Write-Host "reintenta en unos minutos o sube -WaitIdleMinutes." -ForegroundColor Red
    exit 3
}

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
foreach ($t in $Tasks) {
    $xmlPath = Join-Path $BackupDir ("{0}.xml" -f $t.Name)
    Export-ScheduledTask -TaskName $t.Name | Set-Content -Path $xmlPath -Encoding Unicode
    Write-Host "[$(Get-Date)] Backup $($t.Name) -> $xmlPath"
}

$startup = New-ScheduledTaskTrigger -AtStartup
$logon = New-ScheduledTaskTrigger -AtLogOn -User $User
# OJO: -AllowHardTerminate NO existe en New-ScheduledTaskSettingsSet (es una
# propiedad del objeto Settings, no un switch). El valor por defecto ya es
# True, que es lo que necesitamos para que /End pueda terminar el proceso.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

try {
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
} catch {
    Write-Host ("[$(Get-Date)] ERROR al re-registrar: " + $_.Exception.Message) -ForegroundColor Red
    Restore-OriginalTasks ($Tasks | ForEach-Object { $_.Name })
    Write-Host "Rollback aplicado. XML original en $BackupDir"
    exit 1
}

$failed = @()
foreach ($t in $Tasks) {
    Start-Sleep -Seconds 8
    $state = (Get-ScheduledTask -TaskName $t.Name).State
    if ($state -ne "Running") {
        Write-Host ("[$(Get-Date)] ATENCION: $($t.Name) no quedo Running (state=$state).") -ForegroundColor Red
        Save-Diagnostics $t
    }
    Write-Host "[$(Get-Date)] Esperando health de $($t.Name) en $($t.Port)..."
    if (-not (Wait-BridgeHealth -Port $t.Port -TimeoutSeconds 150)) {
        Write-Host ("[$(Get-Date)] SIN HEALTH: $($t.Name)") -ForegroundColor Red
        Save-Diagnostics $t
        $failed += $t
    } else {
        Write-Host "[$(Get-Date)] OK: $($t.Name) responde en 127.0.0.1:$($t.Port)" -ForegroundColor Green
    }
}

if ($failed.Count -gt 0) {
    Write-Host "[$(Get-Date)] FALLO: $($failed.Name -join ', ') no respondio. Restaurando XML original..." -ForegroundColor Red
    Restore-OriginalTasks ($failed | ForEach-Object { $_.Name })
    Write-Host "Rollback aplicado. Diagnostico y XML original en $BackupDir"
    exit 1
}

Write-Host "[$(Get-Date)] Listo: los puentes seleccionados arrancan al encender y corren sin login." -ForegroundColor Green
Write-Host "Reversion: schtasks /Create /TN <tarea> /XML $BackupDir\<tarea>.xml /F"
exit 0
