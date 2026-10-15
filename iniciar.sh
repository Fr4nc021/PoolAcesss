#!/bin/bash
# Sobe o Acesso Piscinas no Linux do Chromebook.
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || realpath "$0")")"
ROOT="$(pwd)"

echo "Acesso Piscinas"
echo "Pasta: $ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Ative o Linux neste Chromebook:"
  echo "Configuracoes > Sobre o ChromeOS > Ambiente de desenvolvimento Linux."
  echo "Abra o app Terminal e rode de novo: bash iniciar.sh"
  exit 1
fi

if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  echo "Instalando o Python. O Linux pode pedir uma senha."
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip
fi

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv "$ROOT/.venv"
fi

STAMP="$ROOT/.venv/.deps-stamp"
if [ ! -f "$STAMP" ] || [ "$ROOT/requirements.txt" -nt "$STAMP" ]; then
  "$ROOT/.venv/bin/python" -m pip install --upgrade pip
  "$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
  touch "$STAMP"
fi

if ! command -v zenity >/dev/null 2>&1; then
  sudo apt-get install -y zenity || true
fi

if [ ! -f "$ROOT/env.txt" ] && [ ! -f "$ROOT/.env" ]; then
  echo "Falta o env.txt nesta pasta."
  echo "Copie o env.txt para junto do iniciar.sh e rode de novo."
  exit 1
fi

mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/acesso-piscinas.desktop" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Acesso Piscinas
Comment=Cadastro de acesso as piscinas
Exec=bash "$ROOT/iniciar.sh"
Path=$ROOT
Terminal=true
Categories=Utility;
EOF

echo
echo "Na primeira vez, o Acesso Piscinas passa a aparecer no lancador."
echo "Para o facial baixar as fotos, encaminhe a porta 5000:"
echo "Configuracoes > Sobre o ChromeOS > Linux > Encaminhamento de porta."
echo "SERVER_LAN_IP no env.txt e o IP do Wi-Fi: Configuracoes > Rede."
echo

exec "$ROOT/.venv/bin/python" -m app.main
