# Venture One - Aplicacion unica: captura, almacen y ficha del inmueble
# Uso (PowerShell normal, no administrador):   .\scripts\iniciar.ps1
# Archivo sin tildes a proposito (PowerShell 5 lee los .ps1 en ANSI).
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = Join-Path $raiz "src"
$py = Join-Path $env:USERPROFILE "anaconda3\envs\venture-one\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py -m captura.api @args
