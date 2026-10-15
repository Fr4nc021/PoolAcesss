"""Ponto de entrada do AcessoPiscinas-Atualizador.exe (PyInstaller)."""
import sys

from app.updater_main import main

if __name__ == "__main__":
    sys.exit(main())
