$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "tools\\go2rtc\\go2rtc.exe"
$config = Join-Path $root "go2rtc.yaml"

if (-not (Test-Path $exe)) {
    throw "go2rtc.exe nao encontrado em $exe"
}

if (-not (Test-Path $config)) {
    throw "go2rtc.yaml nao encontrado em $config"
}

Start-Process -FilePath $exe -ArgumentList @("-config", $config) -WorkingDirectory $root
