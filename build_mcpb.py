#!/usr/bin/env python3
"""build_mcpb.py - Gera o arquivo todos.mcpb (Desktop Extension para Claude).

Uso:
    python3 build_mcpb.py

Produz: dist/todos.mcpb
"""

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DIST_DIR = PROJECT_ROOT / "dist"
OUTPUT_NAME = "todos.mcpb"

# Icone do projeto (ajuste o caminho se necessario)
ICON_SOURCES = [
    PROJECT_ROOT / "icon.png",
    Path.home() / "Documents/Git/Lab2Code/SEI Pro/sei-pro/dist/icons/sei_pro.png",
]

# Arquivos e pastas a incluir no .mcpb
INCLUDE = [
    "manifest.json",
    "pyproject.toml",
    "bootstrap.py",
    "README.md",
    "icon.png",
    "src/todos/",
]

# Padroes a ignorar
IGNORE_PATTERNS = {
    "__pycache__",
    ".pyc",
    ".egg-info",
    ".DS_Store",
    ".git",
    ".env",
}


def should_ignore(path: Path) -> bool:
    """Return True if path matches any ignore pattern."""
    for part in path.parts:
        for pattern in IGNORE_PATTERNS:
            if pattern in part:
                return True
    return False


def ensure_icon() -> Path | None:
    """Copy icon to project root; return the path if found, else None."""
    dest = PROJECT_ROOT / "icon.png"
    if dest.exists():
        return dest
    for src in ICON_SOURCES:
        if src.exists():
            shutil.copy2(src, dest)
            sys.stdout.write(f"  [*] Icone copiado de {src}\n")
            return dest
    sys.stdout.write("  [!] Icone nao encontrado. O .mcpb sera criado sem icone.\n")
    return None


def build() -> None:
    """Package the project into dist/todos.mcpb."""
    sys.stdout.write("\n")
    sys.stdout.write("=" * 50 + "\n")
    sys.stdout.write("  Build: todos.mcpb\n")
    sys.stdout.write("=" * 50 + "\n")
    sys.stdout.write("\n")

    # Validar manifest.json
    manifest_path = PROJECT_ROOT / "manifest.json"
    if not manifest_path.exists():
        sys.stdout.write("  [ERRO] manifest.json nao encontrado.\n")
        return
    manifest = json.loads(manifest_path.read_text())
    sys.stdout.write(f"  [*] {manifest['display_name']} v{manifest['version']}\n")
    sys.stdout.flush()

    # Garantir icone
    icon = ensure_icon()

    # Criar dist/
    DIST_DIR.mkdir(exist_ok=True)
    output = DIST_DIR / OUTPUT_NAME

    # Montar o ZIP
    count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for item_name in INCLUDE:
            item = PROJECT_ROOT / item_name
            if not item.exists():
                if item_name == "icon.png" and not icon:
                    continue
                sys.stdout.write(f"  [!] Pulando {item_name} (nao existe)\n")
                continue

            if item.is_file():
                zf.write(item, item_name)
                count += 1
            elif item.is_dir():
                for root, _dirs, files in os.walk(item):
                    root_path = Path(root)
                    for f in files:
                        file_path = root_path / f
                        if should_ignore(file_path):
                            continue
                        arcname = str(file_path.relative_to(PROJECT_ROOT))
                        zf.write(file_path, arcname)
                        count += 1

    size_kb = output.stat().st_size / 1024
    sys.stdout.write(f"  [*] {count} arquivos empacotados\n")
    sys.stdout.write(f"  [*] Gerado: {output} ({size_kb:.0f} KB)\n")
    sys.stdout.write("\n")
    sys.stdout.write("  Para instalar no Claude Desktop:\n")
    sys.stdout.write(f"    Abra {output} com duplo-clique\n")
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    build()
