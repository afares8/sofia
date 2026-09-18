# restore_tec.ps1 - Repair the SofiaTunnel reverse SSH tunnel for ElenaTec (tec).
#
# Executed by Sofia's restore engine, which runs under SofiaBackend (LocalSystem),
# so it can fix key ownership/ACLs and restart the NSSM service that a normal
# user session cannot. Idempotent: safe to run on every "tec is down" restore.
#
# Why this exists: the tunnel service (SofiaTunnel) runs ssh.exe as LocalSystem.
# OpenSSH rejects the private key unless it is owned by Administrators/SYSTEM and
# readable only by SYSTEM + Administrators. A key created by a normal user fails
# that check ("bad permissions"), which pauses the service in a throttle loop.

$ErrorActionPreference = 'Continue'
$dir        = 'C:\ProgramData\sofia-tunnel'
$key        = Join-Path $dir 'id_ed25519'
$stderrLog  = Join-Path $dir 'stderr.log'
$nssm       = 'C:\Program Files\NSSM\nssm.exe'
$service    = 'SofiaTunnel'

try {
    if (-not (Test-Path $key)) { throw "key file missing: $key" }

    # 1) Ownership: OpenSSH requires the key be owned by Administrators/SYSTEM
    #    when the ssh client runs as LocalSystem.
    & icacls $key /setowner 'Administrators' | Out-Null

    # 2) DACL: only SYSTEM and Administrators, no inheritance.
    #    FullControl (not Read): a read-only DACL locks everyone but the owner
    #    out of WRITE_DAC/WRITE_OWNER and makes the key unrepairable later.
    $fs = New-Object System.Security.AccessControl.FileSecurity
    $fs.SetAccessRuleProtection($true, $false)
    $fs.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule('SYSTEM','FullControl','Allow')))
    $fs.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule('Administrators','FullControl','Allow')))
    [System.IO.File]::SetAccessControl($key, $fs)

    # 3) Restart the tunnel service (clears nssm throttle/paused state).
    if (Test-Path $nssm) {
        & $nssm stop $service  | Out-Null
        Start-Sleep -Seconds 2
        & $nssm start $service | Out-Null
    } else {
        & sc.exe stop $service  | Out-Null
        Start-Sleep -Seconds 2
        & sc.exe start $service | Out-Null
    }
    Start-Sleep -Seconds 8

    $svc = Get-Service $service -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        $tail = ''
        if (Test-Path $stderrLog) { $tail = (Get-Content $stderrLog -Tail 3) -join ' | ' }
        Write-Output "RESTORE_FAILED: $service state=$($svc.Status) log=$tail"
        exit 1
    }

    # ssh exiting immediately would drop the service out of Running again;
    # a second state check after a short delay catches fast-fail retries.
    Start-Sleep -Seconds 5
    $svc.Refresh()
    if ($svc.Status -ne 'Running') {
        Write-Output "RESTORE_FAILED: $service dropped out of Running (ssh still failing)"
        exit 1
    }

    Write-Output "RESTORE_SUCCESS: key ACL repaired, $service running"
    exit 0
} catch {
    Write-Output "RESTORE_FAILED: $($_.Exception.Message)"
    exit 1
}
