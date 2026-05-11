# restore_mayor.ps1
# Restaura el backend de Mayor:
#   1. Mata huérfanos en puerto 8075 y 9000 (DIAPI)
#   2. Lanza el proceso una sola vez (sin loop)
#   3. Espera que el health check responda (máx 3 minutos)
#   4. Exit 0 = levantó, Exit 1 = no levantó

$ErrorActionPreference = "Stop"
$Port = 8075
$HealthUrl = "http://192.168.0.123:8075/health"
$WorkDir = "D:\mayor\backend"
$PythonExe = "C:\Users\ahmed\AppData\Local\Programs\Python\Python312\python.exe"
$TimeoutSeconds = 180
$CheckInterval = 5

Write-Host "[$(Get-Date)] Restaurando Mayor Backend (puerto $Port)..." -ForegroundColor Cyan

# 1. Matar huérfanos en puerto 8075
Write-Host "[$(Get-Date)] Limpiando procesos en puerto $Port..."
$pids8075 = (netstat -ano | Select-String ":$Port\s") |
    ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
foreach ($procId in $pids8075) {
    if ($procId -match '^\d+$') {
        Write-Host "  Terminando PID $procId (puerto $Port)..." -ForegroundColor Yellow
        taskkill /F /PID $procId 2>$null
    }
}

# 2. Matar huérfanos en puerto 9000 (DIAPI middleware)
Write-Host "[$(Get-Date)] Limpiando procesos en puerto 9000 (DIAPI)..."
$pids9000 = (netstat -ano | Select-String ":9000\s") |
    ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
foreach ($procId in $pids9000) {
    if ($procId -match '^\d+$') {
        Write-Host "  Terminando PID $procId (puerto 9000)..." -ForegroundColor Yellow
        taskkill /F /PID $procId 2>$null
    }
}

Start-Sleep -Seconds 3

# 3. Lanzar Mayor Backend (una sola vez, sin loop)
Write-Host "[$(Get-Date)] Lanzando Mayor Backend..." -ForegroundColor Green
Set-Location $WorkDir
$proc = Start-Process -FilePath $PythonExe -ArgumentList "run.py" -WorkingDirectory $WorkDir -PassThru -NoNewWindow
Write-Host "[$(Get-Date)] Proceso lanzado con PID $($proc.Id)"

# 4. Esperar que el health check responda
Write-Host "[$(Get-Date)] Esperando que Mayor responda en $HealthUrl (máx ${TimeoutSeconds}s)..."
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$up = $false

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $CheckInterval
    try {
        $resp = Invoke-WebRequest -Uri $HealthUrl -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -lt 500) {
            $up = $true
            break
        }
    } catch {
        Write-Host "  [$(Get-Date)] Aún no responde..." -ForegroundColor DarkGray
    }
}

if ($up) {
    Write-Host "[$(Get-Date)] ✅ Mayor Backend levantó correctamente." -ForegroundColor Green
    exit 0
} else {
    Write-Host "[$(Get-Date)] ❌ Mayor Backend no respondió en ${TimeoutSeconds}s." -ForegroundColor Red
    exit 1
}