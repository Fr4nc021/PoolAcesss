@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  AcessoPiscinas - Instalacao
echo ============================================

where python >nul 2>nul
if errorlevel 1 (
  echo ERRO: Python nao encontrado. Instale Python 3.10+ e marque "Add to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Criando ambiente virtual...
  python -m venv .venv
)

echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Arquivo .env criado. Confira IP/login/senha do equipamento.
)

echo.
echo Instalacao concluida.
echo Para iniciar: dê dois cliques em INICIAR.bat
echo.
pause
