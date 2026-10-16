#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Start,
    [string]$User = "ahmed",
    [string]$PolicyDir = ""
)

$ErrorActionPreference = "Stop"
if (-not $PolicyDir) { $PolicyDir = $PSScriptRoot }
if (-not $PolicyDir) { $PolicyDir = (Get-Location).Path }

$policyPath = Join-Path $PolicyDir "policy.json"
if (-not (Test-Path $policyPath)) { throw "policy.json no encontrado en $PolicyDir" }
$policy = Get-Content $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json

$repoRoot = Split-Path -Parent (Split-Path -Parent $PolicyDir)
$bridgeScript = Join-Path $repoRoot "backend\app\scripts\sofia_devin_bridge.py"

$pythonExe = [string]$policy.python_executable
$pythonwExe = Join-Path (Split-Path -Parent $pythonExe) "pythonw.exe"
$devinExe = [string]$policy.devin_executable
$sourceDb = [string]$policy.source_db
$stateRoot = [string]$policy.state_root

$problems = @()
foreach ($p in @($bridgeScript, $pythonExe, $devinExe, $sourceDb)) {
    if (-not (Test-Path $p)) { $problems += "falta ruta: $p" }
}
foreach ($name in @("local", "elenatec")) {
    $g = $policy.groups.$name
    if (-not $g) { $problems += "policy sin grupo $name"; continue }
    if (-not (Test-Path ([string]$g.cwd))) { $problems += "cwd inexistente para ${name}: $($g.cwd)" }
    $tpl = Join-Path $PolicyDir ([string]$g.initial_prompt)
    if (-not (Test-Path $tpl)) { $problems += "prompt inicial inexistente para ${name}: $tpl" }
    if (-not $g.task_name) { $problems += "grupo $name sin task_name" }
}
foreach ($f in @("occurrences.sql", "highwater.sql")) {
    if (-not (Test-Path (Join-Path $PolicyDir $f))) { $problems += "falta $f en $PolicyDir" }
}
if (-not (Test-Path $stateRoot)) {
    Write-Host "info: state_root aun no existe (se creara en ejecucion): $stateRoot"
}
$watcherExe = if (Test-Path $pythonwExe) { $pythonwExe } else {
    Write-Host "info: pythonw.exe ausente; se usara python.exe"
    $pythonExe
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-Host ("contexto actual admin: {0}" -f $isAdmin)

$auth = "desconocido"
if (Test-Path $devinExe) {
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $devinExe
        $psi.Arguments = "auth status"
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $proc = New-Object System.Diagnostics.Process
        $proc.StartInfo = $psi
        [void]$proc.Start()
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        if ($proc.WaitForExit(30000)) {
            $auth = if ($proc.ExitCode -eq 0) { "autenticado" } else { "NO autenticado" }
        } else {
            try { $proc.Kill() } catch {}
            $auth = "timeout"
        }
        $proc.Dispose()
    } catch { $auth = "error consultando" }
}
Write-Host "devin auth status: $auth"
if ($auth -ne "autenticado") {
    Write-Host "ALTO: el CLI no esta autenticado; no activar hasta que el usuario inicie sesion."
}

$tasks = @()
foreach ($name in @("local", "elenatec")) {
    $g = $policy.groups.$name
    $tasks += [pscustomobject]@{
        TaskName  = [string]$g.task_name
        Group     = $name
        Elevated  = [bool]$g.elevated
        Arguments = ('"{0}" watch --group {1} --policy-dir "{2}"' -f $bridgeScript, $name, $PolicyDir)
    }
}

Write-Host "=== Plan de tareas programadas Sofia-Devin ==="
$drift = @()
foreach ($t in $tasks) {
    $level = if ($t.Elevated) { "Highest (elevado)" } else { "Limited" }
    Write-Host ("- {0}  grupo={1}  RunLevel={2}" -f $t.TaskName, $t.Group, $level)
    Write-Host ("    exe: {0}" -f $watcherExe)
    Write-Host ("    args: {0}" -f $t.Arguments)
    Write-Host ("    trigger: al iniciar sesion de {0}, InteractiveToken" -f $User)
    $existing = Get-ScheduledTask -TaskName $t.TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        $exAction = $existing.Actions | Select-Object -First 1
        $exArgs = [string]$exAction.Arguments
        $exRun = [string]$existing.Principal.RunLevel
        $exUser = [string]$existing.Principal.UserId
        if ($exArgs -notlike "*sofia_devin_bridge.py*") {
            $drift += "$($t.TaskName): existe y NO pertenece al puente; no se sobrescribe"
        } elseif ($exArgs -ne $t.Arguments -or [string]$exAction.Execute -ne $watcherExe `
                  -or $exUser -ne $User `
                  -or ($t.Elevated -and $exRun -ne "Highest") `
                  -or (-not $t.Elevated -and $exRun -eq "Highest")) {
            $drift += "$($t.TaskName): existe del puente pero difiere (args/exe/user/runlevel); reportado, no se sobrescribe"
        } else {
            Write-Host "    ya registrada y coincide con el plan."
        }
    }
}
foreach ($d in $drift) { Write-Host "DRIFT: $d" }

if ($problems.Count -gt 0) {
    $problems | ForEach-Object { Write-Host "ERROR: $_" }
    throw "Validacion de rutas fallida; no se instala nada."
}
if ($Start -and -not $Install) {
    throw "-Start requiere -Install."
}
if ($Install) {
    if (-not $isAdmin) { throw "-Install requiere una consola elevada." }
    if ($auth -ne "autenticado") {
        throw "devin auth status no es 'autenticado' ($auth); no se registra nada."
    }
    try {
        [void](New-Object System.Security.Principal.NTAccount($User)).Translate(
            [System.Security.Principal.SecurityIdentifier])
    } catch {
        throw "La cuenta solicitada no resuelve en Windows: $User"
    }
}
if ($drift.Count -gt 0 -and $Install) {
    throw "Hay tareas existentes que difieren o son ajenas; revisar manualmente. No se sobrescribe nada."
}

if (-not $Install) {
    Write-Host ""
    Write-Host "DRY RUN: no se registro ni inicio nada. Reintentar con -Install para registrar."
    Write-Host "Para desinstalar tras instalar:"
    foreach ($t in $tasks) {
        Write-Host ("  Disable-ScheduledTask -TaskName {0}" -f $t.TaskName)
        Write-Host ("  Unregister-ScheduledTask -TaskName {0} -Confirm:`$false" -f $t.TaskName)
    }
    exit 0
}

foreach ($t in $tasks) {
    $existing = Get-ScheduledTask -TaskName $t.TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($Start) {
            Start-ScheduledTask -TaskName $t.TaskName
            Write-Host "Iniciada (ya registrada e identica): $($t.TaskName)"
        }
        continue
    }
    $action = New-ScheduledTaskAction -Execute $watcherExe -Argument $t.Arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $runLevel = if ($t.Elevated) { "Highest" } else { "Limited" }
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel $runLevel
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $t.TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description "Watcher oculto Sofia-Devin ($($t.Group)); abre consola visible solo con reportes del dia." | Out-Null
    Write-Host "Registrada: $($t.TaskName) (RunLevel $runLevel)"
    if ($Start) {
        Start-ScheduledTask -TaskName $t.TaskName
        Write-Host "Iniciada: $($t.TaskName)"
    }
}

Write-Host ""
Write-Host "Listo. Para revertir:"
foreach ($t in $tasks) {
    Write-Host ("  Disable-ScheduledTask -TaskName {0}" -f $t.TaskName)
    Write-Host ("  Unregister-ScheduledTask -TaskName {0} -Confirm:`$false" -f $t.TaskName)
}
