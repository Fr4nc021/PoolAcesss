@echo off
setlocal
cd /d "%~dp0"

echo Gerando pasta para o Chromebook...
set DIST=dist\AcessoPiscinas_ChromeOS
if exist "%DIST%" rmdir /s /q "%DIST%"
mkdir "%DIST%"

xcopy /E /I /Y "app" "%DIST%\app\" >nul
xcopy /E /I /Y "templates" "%DIST%\templates\" >nul
xcopy /E /I /Y "static" "%DIST%\static\" >nul
if exist "assets" xcopy /E /I /Y "assets" "%DIST%\assets\" >nul
copy /Y "requirements.txt" "%DIST%\requirements.txt" >nul
copy /Y "iniciar.sh" "%DIST%\iniciar.sh" >nul
copy /Y "run_app.py" "%DIST%\run_app.py" >nul
copy /Y ".env.example" "%DIST%\env.exemplo.txt" >nul
if exist ".env" (
  copy /Y ".env" "%DIST%\env.txt" >nul
) else if exist "env.txt" (
  copy /Y "env.txt" "%DIST%\env.txt" >nul
) else (
  copy /Y ".env.example" "%DIST%\env.txt" >nul
)

for /d /r "%DIST%" %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"

(
echo Acesso Piscinas no Chromebook
echo =============================
echo.
echo 1. Edite env.txt com o IP do Wi-Fi deste Chromebook em SERVER_LAN_IP.
echo    O IP esta em Configuracoes, Rede. Nao use o IP da sua casa.
echo 2. Ative o Linux: Configuracoes, Sobre o ChromeOS, Ambiente de desenvolvimento Linux.
echo 3. No app Arquivos, copie esta pasta para Arquivos do Linux.
echo 4. Abra o app Terminal e rode:
echo    cd AcessoPiscinas_ChromeOS
echo    bash iniciar.sh
echo 5. O Chrome abre em http://127.0.0.1:5000
echo 6. Para o facial baixar as fotos, encaminhe a porta 5000:
echo    Configuracoes, Sobre o ChromeOS, Linux, Encaminhamento de porta.
echo    Porta 5000, tipo TCP.
echo.
echo Para fechar, feche a janela do Terminal.
echo.
) > "%DIST%\LEIA-ME.txt"

echo.
echo Pasta pronta em: %DIST%
echo Envie essa pasta ao Chromebook. O env.txt entra nela e nao comeca com ponto.
echo.
pause
