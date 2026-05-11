# restore_pantalla.ps1
$ErrorActionPreference = "Stop"
$Port = 8000
$HealthUrl = "http://192.168.0.123:8000/health"
$WorkDir = "D:\Pantalla\backend"
$TimeoutSeconds = 180
$CheckInterval = 5

Write-Host "[$(Get-Date)] Restaurando Pantalla Backend (puerto $Port)..." -ForegroundColor Cyan

# 1. Matar huérfanos en puerto 8000
Write-Host "[$(Get-Date)] Limpiando procesos en puerto $Port..."
$pidsPort = (netstat -ano | Select-String ":$Port\s") |
    ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
foreach ($procId in $pidsPort) {
    if ($procId -match '^\d+$') {
        Write-Host "  Terminando PID $procId (puerto $Port)..." -ForegroundColor Yellow
        taskkill /F /PID $procId 2>$null
    }
}

Start-Sleep -Seconds 3

# 2. Lanzar Pantalla Backend (usa poetry)
Write-Host "[$(Get-Date)] Lanzando Pantalla Backend..." -ForegroundColor Green
Set-Location $WorkDir
$proc = Start-Process -FilePath "poetry" -ArgumentList "run", "python", "run.py" -WorkingDirectory $WorkDir -PassThru -NoNewWindow
Write-Host "[$(Get-Date)] Proceso lanzado con PID $($proc.Id)"

# 3. Esperar que el health check responda
Write-Host "[$(Get-Date)] Esperando que Pantalla responda (máx ${TimeoutSeconds}s)..."
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
    Write-Host "[$(Get-Date)] ✅ Pantalla Backend levantó correctamente." -ForegroundColor Green
    exit 0
} else {
    Write-Host "[$(Get-Date)] ❌ Pantalla Backend no respondió en ${TimeoutSeconds}s." -ForegroundColor Red
    exit 1
}