<#
  Создать задачу Windows «monitor-bot-pull»: каждый день в 05:00 забирать копии с сервера.
  Если ПК в это время выключен, задача выполнится, когда он включится.
  Задача работает от текущего пользователя и только когда он вошёл в систему (пароль не сохраняется).
  Сервер и папка сохраняются в pull-backups.json, задача берёт их оттуда, поэтому папку потом можно
  менять через set-backup-path.ps1 без пересоздания задачи.

  powershell -ExecutionPolicy Bypass -File install-task.ps1 -Server root@IP [-Dest D:\папка] [-Time 05:00]
  Удалить:  Unregister-ScheduledTask -TaskName monitor-bot-pull
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,   # например root@1.2.3.4
    [string]$Time = "05:00",
    [string]$Dest
)
. (Join-Path $PSScriptRoot "pull-common.ps1")
$cfg = Read-PullConfig
$cfg | Add-Member -NotePropertyName Server -NotePropertyValue $Server -Force
if ($Dest) { $cfg | Add-Member -NotePropertyName Dest -NotePropertyValue ([IO.Path]::GetFullPath($Dest)) -Force }
Save-PullConfig $cfg
$finalDest = Pick $null $cfg.Dest $DefaultDest

$script = Join-Path $PSScriptRoot "pull-backups.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "monitor-bot-pull" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Забирать резервные копии с сервера мониторинга" -Force | Out-Null
Write-Output "Задача monitor-bot-pull создана: каждый день в $Time, копии в $finalDest"
