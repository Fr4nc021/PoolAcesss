@echo off
setlocal
cd /d "%~dp0"

echo Gerando pacote executavel AcessoPiscinas...
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
".venv\Scripts\pyinstaller.exe" --noconfirm --clean AcessoPiscinas.spec

if errorlevel 1 (
  echo Falha no PyInstaller.
  pause
  exit /b 1
)

REM Monta pasta de distribuicao pronta para copiar
set DIST=dist\AcessoPiscinas_Pacote
if exist "%DIST%" rmdir /s /q "%DIST%"
mkdir "%DIST%"
xcopy /E /I /Y "dist\AcessoPiscinas\*" "%DIST%\" >nul

copy /Y "assets\aquaaccess.ico" "%DIST%\aquaaccess.ico" >nul
copy /Y ".env.example" "%DIST%\.env.example" >nul
if exist ".env" (
  copy /Y ".env" "%DIST%\.env" >nul
) else (
  copy /Y ".env.example" "%DIST%\.env" >nul
)

(
echo AcessoPiscinas - como usar neste PC
echo ==================================
echo.
echo 1. Edite o arquivo .env se precisar ^(IP do XPE, senha^).
echo 2. Dê dois cliques em AcessoPiscinas.exe
echo    Na primeira abertura surge o atalho Acesso Piscinas na area de trabalho.
echo 3. O Chrome abre em http://127.0.0.1:5000
echo 4. Em outro PC da rede: http://IP-DESTE-PC:5000
echo.
echo Nao ha janela preta. Para fechar, clique no icone ao lado do relogio
echo e confirme "Fechar programa". O sistema sincroniza antes de sair.
echo.
) > "%DIST%\LEIA-ME.txt"

echo.
echo Pacote pronto em: %DIST%
echo.
pause
