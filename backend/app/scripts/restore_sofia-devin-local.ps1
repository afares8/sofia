# restore_sofia-devin-local.ps1
# Restaura el watcher del puente Sofia-Devin de las apps de esta PC.
# El puente no es un servicio NSSM: corre como tarea programada
# `Sofia-Devin-AppsPC` (pythonw + sofia_devin_bridge.py watch --group local).
#   1. Termina la tarea y el proceso que escucha en 5185
#   2. Relanza la tarea
#   3. Espera a que http://127.0.0.1:5185/health responda (max 3 minutos)
#   4. Exit 0 = levanto, Exit 1 = no levanto
# Salida esperada por restore_service: RESTORE_SUCCESS / RESTORE_FAILED.

$ErrorActionPreference = "Continue"
$TaskName = "Sofia-Devin-AppsPC"
$Port = 5185
$HealthUrl = "http://127.0.0.1:5185/health"
$TimeoutSeconds = 180
$CheckInterval = 5

Write-Host "[$(Get-Date)] Restaurando puente Sofia-Devin local (tarea $TaskName)..."

Write-Host "[$(Get-Date)] Terminando tarea..."
schtasks /End /TN $TaskName 2>$null | Out-Null

Write-Host "[$(Get-Date)] Limpiando procesos en puerto $Port..."
$pids = (netstat -ano | Select-String ":$Port\s") |
    ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
foreach ($procId in $pids) {
    if ($procId -match '^\d+$') {
        Write-Host "  Terminando PID $procId (puerto $Port)..."
        taskkill /F /PID $procId 2>$null | Out-Null
    }
}

Start-Sleep -Seconds 2
Write-Host "[$(Get-Date)] Iniciando tarea $TaskName..."
schtasks /Run /TN $TaskName 2>$null | Out-Null

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $CheckInterval
    try {
        $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            Write-Host "RESTORE_SUCCESS: puente local responde en $HealthUrl"
            exit 0
        }
    } catch {
        Write-Host "  health aun no responde: $($_.Exception.Message)"
    }
}

Write-Host "RESTORE_FAILED: puente local no respondio en $TimeoutSeconds s ($HealthUrl)"
exit 1
