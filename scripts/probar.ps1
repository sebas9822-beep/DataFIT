# Corre las pruebas automaticas (no salen a internet).
# Uso:   .\scripts\probar.ps1
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = Join-Path $raiz "src"
$py = Join-Path $env:USERPROFILE "anaconda3\envs\venture-one\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py -m pytest @args
