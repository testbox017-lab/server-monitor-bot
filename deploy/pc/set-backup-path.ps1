<#
  Поменять папку, куда ПК складывает копии с сервера. Задачу пересоздавать не нужно.

  powershell -ExecutionPolicy Bypass -File set-backup-path.ps1 -Dest D:\новая\папка [-Move]
  -Move  перенести уже скачанные копии и pull.log из старой папки в новую
  Без -Dest — показать текущую папку.
#>
param(
    [string]$Dest,
    [switch]$Move
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "pull-common.ps1")
$cfg = Read-PullConfig
$old = Pick $null $cfg.Dest $DefaultDest
if (-not $Dest) { Write-Output "Копии сейчас сохраняются в $old"; exit 0 }

$new = [IO.Path]::GetFullPath($Dest)
New-Item -ItemType Directory -Force -Path $new | Out-Null
$cfg | Add-Member -NotePropertyName Dest -NotePropertyValue $new -Force
Save-PullConfig $cfg

if ($Move -and (Test-Path $old) -and ($old.TrimEnd("\") -ne $new.TrimEnd("\"))) {
    $items = Get-ChildItem $old | Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}_\d{4}(-FAILED)?$' -or $_.Name -eq "pull.log" }
    foreach ($i in $items) {
        $target = Join-Path $new $i.Name
        if ($i.Name -eq "pull.log" -and (Test-Path $target)) {
            Get-Content $i.FullName -Encoding UTF8 | Add-Content $target -Encoding UTF8
            Remove-Item $i.FullName
        } elseif (-not (Test-Path $target)) {
            Move-Item $i.FullName $target
        }
    }
    if (-not (Get-ChildItem $old -Force)) { Remove-Item $old }
    Write-Output "Перенесено: $($items.Count) шт. из $old"
}
Write-Output "Теперь копии сохраняются в $new"
