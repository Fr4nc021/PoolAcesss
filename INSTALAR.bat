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
echo Criando atalho na area de trabalho...
".venv\Scripts\python.exe" -c "from app.shortcut import ensure_desktop_shortcut; ensure_desktop_shortcut()"

echo.
echo Instalacao concluida.
echo Atalho "Acesso Piscinas" na area de trabalho.
echo Para iniciar: dê dois cliques nesse atalho, ou em INICIAR.bat
echo.
pause
