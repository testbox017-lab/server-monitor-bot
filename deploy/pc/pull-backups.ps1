<#
  Забрать резервные копии с серверов на этот ПК.

  Сервер по специальному ключу отдаёт только один tar: по одной последней копии из каждой папки с бэкапами
  (на главном сервере ещё и копию бота), плюс SHA256SUMS. Ключ ничего другого на сервере делать не может
  (в authorized_keys для него стоят command= и restrict). На главном сервере поток отдаёт помощник
  monitor-bot-action, на других — агент monitor-agent (command="/usr/local/sbin/monitor-agent pull-stream").

  Куда класть копии: pull-backups.json рядом со скриптом (меняется через set-backup-path.ps1), по умолчанию
  папка Backups\server в корне проекта. Другие серверы — список Extra в том же файле:
      "Extra": [ { "Name": "ovh", "Server": "root@vps-....ovh.net" } ]
  (необязательно "Dest" и "Key"; по умолчанию папка Backups\<Name> рядом с основной и тот же ключ).
  Каждый запуск создаёт в папке сервера подпапку ГГГГ-ММ-ДД_ЧЧММ, проверяет контрольные суммы и оставляет <Keep>
  последних. Результат пишет в pull.log в папке сервера. Код выхода 0 — со всех серверов забрано.

  Ручной запуск:  powershell -ExecutionPolicy Bypass -File pull-backups.ps1
                  (сервер берётся из pull-backups.json, его записывает install-task.ps1; можно -Server root@IP)
  По расписанию:  install-task.ps1
#>
param(
    [string]$Server,   # например root@1.2.3.4
    [string]$Key,
    [string]$Dest,
    [int]$Keep
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "pull-common.ps1")
$cfg = Read-PullConfig
$Server = Pick $Server $cfg.Server $null
$Key = Pick $Key $cfg.Key "$env:USERPROFILE\.ssh\monitor_bot_pull"
$Dest = Pick $Dest $cfg.Dest $DefaultDest
$Keep = [int](Pick $Keep $cfg.Keep 30)
if (-not $Server) { Write-Output "Не указан сервер: запустите install-task.ps1 -Server root@IP"; exit 1 }

$ssh = "$env:SystemRoot\System32\OpenSSH\ssh.exe"
$tar = "$env:SystemRoot\System32\tar.exe"

function Invoke-Pull([string]$Server, [string]$Key, [string]$Dest, [int]$Keep) {
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    $logFile = Join-Path $Dest "pull.log"
    function Log([string]$msg) {
        $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
        Add-Content -Path $logFile -Value $line -Encoding UTF8
        Write-Host $line
    }
    $stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
    $tmp = Join-Path $Dest ".incoming-$stamp.tar"
    $dir = Join-Path $Dest $stamp
    try {
        if (-not (Test-Path $Key)) { throw "нет ключа $Key (см. README, «Копии на ПК»)" }
        # cmd /c: перенаправление в файл без перекодировки, PowerShell 5 портит двоичный поток
        cmd /c "`"$ssh`" -T -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=20 -i `"$Key`" $Server pull-stream > `"$tmp`""
        if ($LASTEXITCODE -ne 0) { throw "ssh завершился с кодом $LASTEXITCODE" }
        if ((Get-Item $tmp).Length -lt 1024) { throw "сервер прислал пустой ответ" }

        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        & $tar -xf $tmp -C $dir
        if ($LASTEXITCODE -ne 0) { throw "архив не распаковался (код $LASTEXITCODE)" }

        $sums = Join-Path $dir "SHA256SUMS"
        if (-not (Test-Path $sums)) { throw "в архиве нет SHA256SUMS, передача оборвалась" }
        $count = 0; $bytes = 0
        foreach ($line in Get-Content $sums) {
            if (-not $line.Trim()) { continue }
            $hash, $name = $line -split "  ", 2
            $file = Join-Path $dir ($name -replace "/", "\")
            if (-not (Test-Path $file)) { throw "нет файла $name" }
            $actual = (Get-FileHash -Algorithm SHA256 -Path $file).Hash.ToLower()
            if ($actual -ne $hash) { throw "контрольная сумма не совпала: $name" }
            $count++; $bytes += (Get-Item $file).Length
        }
        Remove-Item $tmp -Force
        Log ("OK  {0} {1}: {2} файл(ов), {3:N1} МБ" -f $Server, $stamp, $count, ($bytes / 1MB))

        $old = Get-ChildItem $Dest -Directory | Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}_\d{4}$' } |
            Sort-Object Name -Descending | Select-Object -Skip $Keep
        foreach ($d in $old) { Remove-Item $d.FullName -Recurse -Force; Log "удалена старая папка $($d.Name)" }
        # неудачные попытки тоже не копим: оставляем 5 последних для разбора
        $failed = Get-ChildItem $Dest -Directory | Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}_\d{4}-FAILED$' } |
            Sort-Object Name -Descending | Select-Object -Skip 5
        foreach ($d in $failed) { Remove-Item $d.FullName -Recurse -Force; Log "удалена старая папка $($d.Name)" }
        return $true
    }
    catch {
        Log "ОШИБКА  $Server  $_"
        if (Test-Path $tmp) { Remove-Item $tmp -Force }
        if (Test-Path $dir) { Rename-Item $dir "$stamp-FAILED" -ErrorAction SilentlyContinue }
        return $false
    }
}

$ok = Invoke-Pull $Server $Key $Dest $Keep
foreach ($x in @($cfg.Extra)) {
    if (-not $x -or -not $x.Server) { continue }
    $name = Pick $x.Name $null "server2"
    $xDest = Pick $x.Dest $null (Join-Path (Split-Path $Dest -Parent) $name)
    $xKey = Pick $x.Key $null $Key
    if (-not (Invoke-Pull $x.Server $xKey $xDest $Keep)) { $ok = $false }
}
if ($ok) { exit 0 } else { exit 1 }
