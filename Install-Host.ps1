[CmdletBinding()]
param(
    [string]$PythonPath = '',
    [string]$TaskName = 'TelegramBot-Host',
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath
$requiredFiles = @('bot.py','host_runner.py','requirements.txt','.env')
foreach ($name in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $name))) { throw "Missing file: $name" }
}
if ($ValidateOnly) {
    Write-Output 'OK: installer files are present. No Windows settings changed.'
    return
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run Install-Host.cmd using Run as administrator.'
}
Import-Module ScheduledTasks
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $existingRoot = [IO.Path]::GetFullPath([string]$existing.Actions[0].WorkingDirectory).TrimEnd('\')
    if ($existingRoot -ine $projectRoot.TrimEnd('\')) { throw 'This task name belongs to another folder. Choose a different -TaskName.' }
}
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    if (-not $PythonPath) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            foreach ($version in @('3.14','3.13','3.12')) {
                try {
                    # ASCII JSON avoids legacy console decoding of Cyrillic profile paths.
                    $candidateJson = (& $launcher.Source "-$version" -c 'import sys,json; print(json.dumps(sys.executable,ensure_ascii=True))' 2>$null | Select-Object -Last 1)
                    $candidate = $null
                    if ($LASTEXITCODE -eq 0 -and $candidateJson) {
                        $candidate = $candidateJson | ConvertFrom-Json
                    }
                    if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate)) {
                        $PythonPath = $candidate
                        break
                    }
                }
                catch { continue }
            }
        }
        if (-not $PythonPath) {
            $command = Get-Command python -ErrorAction SilentlyContinue
            if ($command) { $PythonPath = $command.Source }
        }
    }
    if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
        throw 'Install Python 3.12, 3.13 or 3.14 from python.org, then run this installer again.'
    }
    & $PythonPath -c 'import sys; assert (3,12) <= sys.version_info[:2] <= (3,14)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12-3.14 is required.' }
    & $PythonPath -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the Python environment.' }
}
Write-Output 'Checking the Python environment:'
& $venvPython -c 'import sys; assert (3,12) <= sys.version_info[:2] <= (3,14); print(sys.version.split()[0])'
if ($LASTEXITCODE -ne 0) { throw 'The .venv folder does not work on this PC. Rename it and rerun this installer.' }
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the internet connection and rerun the installer.' }
& $venvPython (Join-Path $projectRoot 'host_runner.py') --check
if ($LASTEXITCODE -ne 0) { throw 'Bot configuration check failed. No autostart was installed.' }
if (-not $Credential) {
    $Credential = Get-Credential -UserName $identity.Name -Message 'Windows account for the bot. Enter the Windows password, not the PIN. The bot will run even before desktop login.'
}
if (-not $Credential) { throw 'Installation cancelled: Windows credentials were not provided.' }
# Give only the selected account the access needed to maintain its database and logs.
$accountSid = ([Security.Principal.NTAccount]::new($Credential.UserName)).Translate([Security.Principal.SecurityIdentifier]).Value
& icacls.exe $projectRoot /grant "*${accountSid}:(OI)(CI)M" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot grant the selected account access to the bot folder. Use a local NTFS folder.' }
$pythonWindowless = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'Missing pythonw.exe. Recreate the Python environment.' }
$runner = Join-Path $projectRoot 'host_runner.py'
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('"{0}"' -f $runner) -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval ([TimeSpan]::FromMinutes(1))
if ($existing) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
$passwordText = $Credential.GetNetworkCredential().Password
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -User $Credential.UserName -Password $passwordText -RunLevel Limited -Description 'Telegram bot: startup, reconnect and crash recovery.' -Force | Out-Null
}
finally { $passwordText = $null; $Credential = $null }
$runtime = Join-Path $projectRoot '.runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
@{task_name=$TaskName; project_root=$projectRoot} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime 'install.json') -Encoding UTF8
Start-ScheduledTask -TaskName $TaskName
Write-Output 'DONE: autostart is installed and the bot task has started.'
Write-Output 'Open Host-Control.cmd to check status, stop or restart the bot.'
Write-Output 'Logs: logs\bot.log. Reinstall if the Windows account password changes.'
