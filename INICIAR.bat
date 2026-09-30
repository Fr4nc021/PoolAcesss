@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo Ambiente nao encontrado. Execute INSTALAR.bat primeiro.
  pause
  exit /b 1
)

REM Sem janela preta. Para fechar: icone ao lado do relogio, "Fechar programa".
start "" ".venv\Scripts\pythonw.exe" -m app.main
