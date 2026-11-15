# Общие функции для скриптов копий на ПК.
# Настройки лежат в pull-backups.json рядом со скриптами (в git не попадают).
# Порядок: параметр командной строки, потом pull-backups.json, потом значение по умолчанию.
$ConfigFile = Join-Path $PSScriptRoot "pull-backups.json"
$DefaultDest = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\Backups\server"))

function Read-PullConfig {
    if (Test-Path $ConfigFile) {
        try { return Get-Content $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
    }
    return [pscustomobject]@{}
}

function Save-PullConfig($cfg) {
    $cfg | ConvertTo-Json | Set-Content -Path $ConfigFile -Encoding UTF8
}

function Pick($explicit, $fromConfig, $default) {
    if ($explicit) { return $explicit }
    if ($fromConfig) { return $fromConfig }
    return $default
}
