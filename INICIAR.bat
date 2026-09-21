@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente nao encontrado. Execute INSTALAR.bat primeiro.
  pause
  exit /b 1
)

echo Iniciando AcessoPiscinas...
echo Abra no Chrome: http://127.0.0.1:5000
echo Na rede local: http://IP-DESTE-PC:5000
echo.
".venv\Scripts\python.exe" -m app.main
pause
