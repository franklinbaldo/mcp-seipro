#!/usr/bin/env python3
"""
Bootstrap para o todos MCP (Desktop Extension).

Na primeira execucao, cria um venv em ~/.todos/.venv e instala
as dependencias. Nas execucoes seguintes, apenas executa o servidor.

Este script usa apenas a stdlib do Python.
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

VENV_HOME = Path.home() / ".todos"
VENV_DIR = VENV_HOME / ".venv"
IS_WINDOWS = platform.system() == "Windows"
PYTHON = VENV_DIR / "Scripts" / "python.exe" if IS_WINDOWS else VENV_DIR / "bin" / "python"
TODOS = VENV_DIR / "Scripts" / "todos.exe" if IS_WINDOWS else VENV_DIR / "bin" / "todos"
SRC_DIR = Path(__file__).resolve().parent


def setup():
    """Cria venv e instala o pacote na primeira execucao."""
    print("todos: configurando ambiente (primeira execucao)...", file=sys.stderr)
    VENV_HOME.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        check=True,
    )
    subprocess.run(
        [str(PYTHON), "-m", "pip", "install", "--quiet", str(SRC_DIR)],
        check=True,
    )
    print("todos: ambiente configurado.", file=sys.stderr)


def main():
    if not TODOS.exists():
        setup()

    if IS_WINDOWS:
        # No Windows, os.execv cria um processo novo e mata o atual.
        # O cliente MCP monitora o PID original e fecha ao detectar a saída.
        # subprocess.call mantém o processo-pai vivo enquanto o filho roda.
        sys.exit(subprocess.call([str(TODOS)] + sys.argv[1:]))
    else:
        os.execv(str(TODOS), [str(TODOS)] + sys.argv[1:])


if __name__ == "__main__":
    main()
