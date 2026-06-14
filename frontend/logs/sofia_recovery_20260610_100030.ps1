$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath 'D:\sofia\frontend'
npm run dev *>&1 | Tee-Object -FilePath 'D:\sofia\frontend\logs\sofia_recovery_20260610_100030.log'
