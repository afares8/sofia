# restore_sofiatunnel_e2e.ps1 - Restore for the 'sofiatunnel_e2e' monitor.
#
# That service probes the PUBLIC ingest path (elenatec.com/sofia-ingest ->
# droplet nginx -> reverse tunnel -> Sofia). When it fails while the local
# tunnel check is still green, the usual cause is a dropped remote forward
# on the droplet sshd. Re-establishing the tunnel fixes it, so we delegate
# to the same repair script used by the 'sofiatunnel' monitor.

$inner = Join-Path $PSScriptRoot 'restore_sofiatunnel.ps1'
& $inner
exit $LASTEXITCODE
