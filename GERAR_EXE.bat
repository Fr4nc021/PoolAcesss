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
".venv\Scripts\pyinstaller.exe" --noconfirm --clean AcessoPiscinas-Atualizador.spec
if errorlevel 1 (
  echo Falha no PyInstaller do atualizador.
  pause
  exit /b 1
)

REM Monta dist\AcessoPiscinas_Pacote, o .zip e o manifest em dist\release.
REM O .env deste PC nao entra no pacote: senha do facial e chave da nuvem ficam aqui.
".venv\Scripts\python.exe" scripts\build_release.py
if errorlevel 1 (
  echo Falha ao montar o pacote.
  pause
  exit /b 1
)

echo.
echo Pacote pronto em: dist\AcessoPiscinas_Pacote
echo Na primeira instalacao, copie env.exemplo.txt para env.txt e preencha.
echo.
pause
