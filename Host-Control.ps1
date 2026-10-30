[CmdletBinding()]
param([ValidateSet('Menu','Status','Start','Stop','Restart','Remove')][string]$Action='Menu')
$ErrorActionPreference='Stop'
$projectRoot=(Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath
$configFile=Join-Path $projectRoot '.runtime\install.json'
if (-not (Test-Path -LiteralPath $configFile)) { throw 'Autostart is not installed. Run Install-Host.cmd first.' }
$config=Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
Import-Module ScheduledTasks
function Invoke-HostAction([string]$Command) {
    $task=Get-ScheduledTask -TaskName $config.task_name -ErrorAction Stop
    $taskRoot=[IO.Path]::GetFullPath([string]$task.Actions[0].WorkingDirectory).TrimEnd('\')
    if ($taskRoot -ine $projectRoot.TrimEnd('\')) { throw 'This task belongs to another folder. No changes made.' }
    switch ($Command) {
        'Status' {
            $info=Get-ScheduledTaskInfo -TaskName $config.task_name
            Write-Output "Task: $($task.TaskName) | State: $($task.State)"
            Write-Output "Last start: $($info.LastRunTime) | Result: $($info.LastTaskResult)"
            $panelStatus=Join-Path $projectRoot 'panel_status.py'
            if (Test-Path -LiteralPath $panelStatus) { & (Join-Path $projectRoot '.venv\Scripts\python.exe') $panelStatus }
            $log=Join-Path $projectRoot 'logs\bot.log'
            if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 15 }
        }
        'Start' { Enable-ScheduledTask -TaskName $config.task_name | Out-Null; Start-ScheduledTask -TaskName $config.task_name; Write-Output 'Started.' }
        'Stop' { Disable-ScheduledTask -TaskName $config.task_name | Out-Null; Stop-ScheduledTask -TaskName $config.task_name; Write-Output 'Stopped. Autostart is disabled until Start.' }
        'Restart' { Stop-ScheduledTask -TaskName $config.task_name; Enable-ScheduledTask -TaskName $config.task_name | Out-Null; Start-ScheduledTask -TaskName $config.task_name; Write-Output 'Restarted.' }
        'Remove' { Disable-ScheduledTask -TaskName $config.task_name | Out-Null; Stop-ScheduledTask -TaskName $config.task_name; Unregister-ScheduledTask -TaskName $config.task_name -Confirm:$false; Write-Output 'Autostart removed. Bot files and database are preserved.' }
    }
}
if ($Action -ne 'Menu') { Invoke-HostAction $Action; return }
while ($true) {
    Write-Host "`nTelegramBot Host: 1 Status | 2 Start | 3 Stop | 4 Restart | 5 Remove autostart | 0 Exit"
    $choice=Read-Host 'Choose'
    switch ($choice) {
        '0' { return }
        '1' { Invoke-HostAction 'Status' }
        '2' { Invoke-HostAction 'Start' }
        '3' { Invoke-HostAction 'Stop' }
        '4' { Invoke-HostAction 'Restart' }
        '5' { if ((Read-Host 'Remove autostart? Type YES') -ceq 'YES') { Invoke-HostAction 'Remove'; return } }
        default { Write-Output 'Choose a number from 0 to 5.' }
    }
}
