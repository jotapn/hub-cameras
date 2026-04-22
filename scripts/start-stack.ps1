$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot

Start-Process powershell -ArgumentList @(
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $root "scripts\\start-go2rtc.ps1")
) -WorkingDirectory $root

Start-Sleep -Seconds 2

& (Join-Path $root ".venv\\Scripts\\uvicorn.exe") app.main:app --host 0.0.0.0 --port 8000 --reload
