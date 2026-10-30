$ErrorActionPreference='Stop'
$projectRoot=(Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath
$python=Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Install-Host.cmd first.' }
$installed=Test-Path -LiteralPath (Join-Path $projectRoot '.runtime\install.json')
if ($installed) { & (Join-Path $projectRoot 'Host-Control.ps1') -Action Stop }
& $python -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. The bot is stopped; rerun Update-Host.cmd.' }
& $python (Join-Path $projectRoot 'host_runner.py') --check
if ($LASTEXITCODE -ne 0) { throw 'Configuration check failed. The bot is stopped.' }
if ($installed) { & (Join-Path $projectRoot 'Host-Control.ps1') -Action Start }
Write-Output 'Bot and web panel updated. Open Host-Control.cmd and choose Status.'
