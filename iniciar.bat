@echo off
rem ============================================================================
rem  Venture One - Aplicacion unica: captura, almacen y ficha del inmueble
rem
rem    iniciar.bat            abre la aplicacion en el navegador
rem    iniciar.bat verificar  revisa equipo, almacen, Claude y cada fuente (1 minuto)
rem    iniciar.bat etl        corre el ETL completo y deja el almacen al dia
rem    iniciar.bat reto       genera las 11 fichas oficiales desde el almacen y las valida
rem    iniciar.bat fichas     genera las fichas de las cotizaciones que no la tienen
rem    iniciar.bat rebuscar   busca de nuevo en las inmobiliarias y rehace las fichas anteriores
rem    iniciar.bat inmobiliarias   prueba que sitios del catalogo se dejan leer
rem    iniciar.bat probar     corre las pruebas automaticas y revisa las fuentes
rem    iniciar.bat clave X    cambia la clave del usuario X (comercial o supervisor)
rem
rem  Archivo sin tildes a proposito.
rem ============================================================================
setlocal EnableExtensions
chcp 65001 >nul
title Venture One - Captura, almacen y ficha del inmueble
cd /d "%~dp0"
set "RAIZ=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONPATH=%RAIZ%src"

if not exist "%RAIZ%src\captura\api.py" (
  echo.
  echo   La aplicacion no esta completa en esta carpeta: falta src\captura\api.py
  echo.
  pause
  exit /b 1
)

rem --- Python del entorno venture-one --------------------------------------
set "PY="
for %%P in ("%USERPROFILE%\anaconda3\envs\venture-one\python.exe" "%USERPROFILE%\miniforge3\envs\venture-one\python.exe" "%USERPROFILE%\miniconda3\envs\venture-one\python.exe") do if not defined PY if exist "%%~P" set "PY=%%~P"
if not defined PY set "PY=python"
"%PY%" -c "import httpx, yaml, rapidfuzz, pandas, pyproj" 2>nul
if errorlevel 1 (
  echo.
  echo   Faltan paquetes de Python o no se encontro el entorno venture-one.
  echo   Con el entorno activo ejecuta:   pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)
if /i "%~1"=="probar" goto :probar
if /i "%~1"=="verificar" goto :verificar
if /i "%~1"=="clave" goto :clave
if /i "%~1"=="etl" goto :etl
if /i "%~1"=="reto" goto :reto
if /i "%~1"=="fichas" goto :fichas
if /i "%~1"=="rebuscar" goto :rebuscar
if /i "%~1"=="inmobiliarias" goto :inmobiliarias

rem --- Abrir la aplicacion -------------------------------------------------
echo.
echo   Abriendo la aplicacion en el navegador ...
echo   Para cerrarla: cierra esta ventana o pulsa Ctrl+C.
echo.
"%PY%" -m captura.api %*
if errorlevel 1 pause
exit /b 0

:verificar
echo.
"%PY%" -m captura.verificacion %2 %3
echo.
pause
exit /b 0

:clave
echo.
"%PY%" scripts\cambiar_clave.py %2
echo.
pause
exit /b 0

:etl
echo.
echo   ETL completo: base operativa e insumos controlados al esquema estrella
echo.
"%PY%" -m almacen.etl %2 %3
echo.
pause
exit /b 0

:reto
echo.
echo   Fichas de la muestra oficial, leidas del almacen, y validador oficial
echo.
"%PY%" -m ficha.servicio reto
echo.
pause
exit /b 0

:fichas
echo.
echo   Fichas de las cotizaciones que todavia no la tienen
echo.
"%PY%" -m ficha.servicio pendientes
echo.
pause
exit /b 0

:rebuscar
echo.
echo   Busca de nuevo en las inmobiliarias y rehace las fichas de las cotizaciones ya hechas
echo.
"%PY%" -m captura.rebuscar
echo.
pause
exit /b 0

:inmobiliarias
echo.
echo   Prueba de lectura de las inmobiliarias del catalogo (usa la sesion de Claude Code)
echo.
"%PY%" scripts\probar_inmobiliarias.py %2 %3 %4 %5 %6 %7
echo.
pause
exit /b 0

:probar
echo.
echo   1 de 2 - Pruebas automaticas (no salen a internet)
echo.
"%PY%" -m pytest
echo.
echo   2 de 2 - Fuentes en vivo (servidor de mapas de la Alcaldia y OpenStreetMap)
echo.
"%PY%" scripts\probar_fuentes.py
echo.
pause
exit /b 0
