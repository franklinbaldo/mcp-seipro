#!/usr/bin/env python3
"""
setup_claude.py - Configura o MCP do SEI no Claude Desktop.

Uso:
    python3 setup_claude.py

Este script:
  1. Pergunta suas credenciais do SEI (URL opcional, usuario, senha)
  2. Cria um ambiente virtual e instala o todos
  3. Configura o Claude Desktop para usar o MCP do SEI

Nenhuma dependencia externa necessaria - usa apenas a stdlib do Python.
"""

import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
MIN_PYTHON = (3, 11)
MCP_SERVER_NAME = "todos"
VENV_HOME = Path.home() / ".todos"
VENV_DIR = VENV_HOME / ".venv"

# ---------------------------------------------------------------------------
# Helpers de UI
# ---------------------------------------------------------------------------


def banner():
    print()
    print("=" * 60)
    print("  todos  -  Instalador para Claude Desktop")
    print("=" * 60)
    print()
    print("  Este script configura o servidor MCP do SEI no")
    print("  aplicativo Claude Desktop (Claude Chat / Cowork).")
    print()
    print("  O que sera feito:")
    print("    1. Coletar suas credenciais do SEI")
    print("    2. Criar ambiente virtual e instalar o todos")
    print("    3. Configurar o Claude Desktop automaticamente")
    print()


def info(msg: str):
    print(f"  [*] {msg}")


def warn(msg: str):
    print(f"  [!] {msg}")


def error(msg: str):
    print(f"  [ERRO] {msg}")


def confirm(msg: str, default_yes: bool = True) -> bool:
    suffix = "[S/n]" if default_yes else "[s/N]"
    resp = input(f"  {msg} {suffix} ").strip().lower()
    if not resp:
        return default_yes
    return resp in ("s", "sim", "y", "yes")


# ---------------------------------------------------------------------------
# Fase 0: Deteccao de ambiente
# ---------------------------------------------------------------------------


def check_python():
    if sys.version_info < MIN_PYTHON:
        error(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ e necessario.")
        error(f"Versao atual: {sys.version}")
        sys.exit(1)
    info(f"Python {sys.version_info.major}.{sys.version_info.minor} detectado")


def get_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        p = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            error("Variavel APPDATA nao encontrada.")
            sys.exit(1)
        p = Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:  # Linux
        p = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    info(f"Config do Claude Desktop: {p}")
    return p


def detect_uv() -> str | None:
    uv = shutil.which("uv")
    if uv:
        info(f"uv encontrado: {uv} (instalacao sera mais rapida)")
    return uv


def detect_repo_root() -> Path | None:
    script_dir = Path(__file__).resolve().parent
    pyproject = script_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
            if 'name = "todos"' in text:
                info(f"Repositorio todos detectado: {script_dir}")
                return script_dir
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Fase 1: Prompts interativos
# ---------------------------------------------------------------------------


def prompt_url() -> str:
    print()
    print("  [1/5] URL da API do SEI (opcional)")
    print("        Exemplo: https://sei.orgao.gov.br/sei/modulos/wssei/controlador_ws.php/api/v2")
    print("        Deixe em branco se sua instancia nao tiver mod-wssei instalado.")
    print()
    while True:
        url = input("        URL [Enter para pular]: ").strip()
        if not url:
            return ""
        parsed = urlparse(url)
        if not parsed.scheme:
            warn("URL deve comecar com https:// ou http://")
            continue
        if parsed.scheme == "http":
            warn("Voce esta usando http:// (sem criptografia).")
            if not confirm("Continuar mesmo assim?", default_yes=False):
                continue
        if "/api/v2" not in parsed.path and "/api/v1" not in parsed.path:
            warn("URL nao parece conter /api/v2. Verifique se esta correta.")
            if not confirm("Usar essa URL mesmo assim?", default_yes=False):
                continue
        return url.rstrip("/")


def prompt_web_url(default: str = "") -> str:
    print()
    print("  [1b/5] URL base do SEI (usada pelo scraper web)")
    print("         Exemplo: https://sei.orgao.gov.br")
    if default:
        print(f"         Derivada da URL da API: {default}")
    else:
        print("         Obrigatoria para modo web-only (sem mod-wssei).")
    print()
    hint = f"[Enter para usar {default}]" if default else "URL base do SEI"
    while True:
        url = input(f"         {hint}: ").strip()
        if not url:
            if default:
                return default
            warn("URL base obrigatoria para modo web-only (sem mod-wssei).")
            continue
        parsed = urlparse(url)
        if not parsed.scheme:
            warn("URL deve comecar com https:// ou http://")
            continue
        if parsed.scheme == "http":
            warn("Voce esta usando http:// (sem criptografia).")
            if not confirm("Continuar mesmo assim?", default_yes=False):
                continue
        return url.rstrip("/")


def prompt_usuario() -> str:
    print()
    print("  [2/5] Usuario do SEI")
    print()
    while True:
        user = input("        Usuario: ").strip()
        if user:
            return user
        warn("Usuario nao pode ser vazio.")


def prompt_senha() -> str:
    print()
    print("  [3/5] Senha do SEI")
    print("        (a senha nao sera exibida enquanto voce digita)")
    print()
    while True:
        pwd = getpass.getpass("        Senha: ")
        if pwd:
            return pwd
        warn("Senha nao pode ser vazia.")


def prompt_orgao() -> str:
    print()
    print("  [4/5] Codigo do orgao no SEI")
    print("        Use 0 para o orgao principal (padrao)")
    print()
    orgao = input("        Orgao [0]: ").strip()
    return orgao if orgao else "0"


def prompt_ssl() -> str:
    print()
    print("  [5/5] Verificar certificado SSL?")
    print("        Desabilite apenas se o servidor usa certificado autoassinado.")
    print()
    if confirm("Verificar SSL?", default_yes=True):
        return "true"
    return "false"


# ---------------------------------------------------------------------------
# Fase 2: Instalacao
# ---------------------------------------------------------------------------


def create_venv(uv_path: str | None):
    if VENV_DIR.exists():
        info(f"Ambiente virtual ja existe: {VENV_DIR}")
        if confirm("Recriar o ambiente virtual?", default_yes=False):
            shutil.rmtree(VENV_DIR)
        else:
            return

    info(f"Criando ambiente virtual em {VENV_DIR} ...")
    VENV_HOME.mkdir(parents=True, exist_ok=True)

    if uv_path:
        subprocess.run(
            [uv_path, "venv", str(VENV_DIR), "--python", f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"],
            check=True,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True,
        )
    info("Ambiente virtual criado.")


def get_pip(uv_path: str | None) -> list[str]:
    if uv_path:
        return [uv_path, "pip", "install", "--python", str(venv_python())]
    return [str(venv_python()), "-m", "pip", "install"]


def venv_python() -> Path:
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def todos_command() -> Path:
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "todos.exe"
    return VENV_DIR / "bin" / "todos"


def install_package(repo_root: Path | None, uv_path: str | None):
    pip_cmd = get_pip(uv_path)

    if repo_root:
        info(f"Instalando todos do repositorio local ({repo_root}) ...")
        subprocess.run(
            [*pip_cmd, "-e", str(repo_root)],
            check=True,
        )
    else:
        info("Instalando todos do GitHub ...")
        subprocess.run(
            [*pip_cmd, "git+https://github.com/franklinbaldo/todos.git"],
            check=True,
        )

    cmd = todos_command()
    if not cmd.exists():
        error(f"Comando todos nao encontrado em {cmd}")
        error("A instalacao pode ter falhado. Verifique os erros acima.")
        sys.exit(1)

    info(f"todos instalado: {cmd}")


# ---------------------------------------------------------------------------
# Fase 3: Resumo
# ---------------------------------------------------------------------------


def print_summary(config_path: Path, command: str, env: dict, usar_keyring: bool):
    masked_env = {**env}
    if "SEI_SENHA" in masked_env:
        masked_env["SEI_SENHA"] = "********"

    print()
    print("  " + "=" * 56)
    print("              Resumo da configuracao")
    print("  " + "=" * 56)
    print()
    print(f"    Arquivo:      {config_path}")
    print(f"    Servidor:     {MCP_SERVER_NAME}")
    print(f"    Comando:      {command}")
    print()
    for k, v in masked_env.items():
        print(f"    {k}: {v}")
    if usar_keyring:
        print("    SEI_SENHA: [Salva com segurança no Keyring do sistema]")
    print()
    print("  " + "-" * 56)
    if usar_keyring:
        info("A senha será armazenada de forma criptografada")
        info("no cofre de credenciais seguro do seu sistema operacional.")
    else:
        warn("A senha sera armazenada em texto plano no arquivo")
        warn("de configuracao. Isso e o padrao do Claude Desktop")
        warn("para variaveis de ambiente de servidores MCP.")
    print("  " + "-" * 56)
    print()


# ---------------------------------------------------------------------------
# Fase 4: Escrever configuracao
# ---------------------------------------------------------------------------


def read_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        text = config_path.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        warn(f"Arquivo de configuracao corrompido: {config_path}")
        backup_config(config_path)
        warn("Backup criado. Iniciando com configuracao limpa.")
        return {}


def backup_config(config_path: Path):
    if not config_path.exists():
        return
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = config_path.with_suffix(f".json.bak.{ts}")
    shutil.copy2(config_path, bak)
    info(f"Backup: {bak}")


def write_config(config_path: Path, config: dict):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    info(f"Configuracao salva em {config_path}")


def merge_sei_server(config: dict, command: str, env: dict) -> dict:
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    if MCP_SERVER_NAME in config["mcpServers"]:
        warn(f'Servidor "{MCP_SERVER_NAME}" ja existe na configuracao.')
        if not confirm("Sobrescrever?", default_yes=True):
            info("Mantendo configuracao existente.")
            sys.exit(0)

    config["mcpServers"][MCP_SERVER_NAME] = {
        "command": command,
        "env": env,
    }
    return config


# ---------------------------------------------------------------------------
# Fase 5: Sucesso
# ---------------------------------------------------------------------------


def print_success(config_path: Path):
    print()
    print("  " + "=" * 56)
    print("              Instalacao concluida!")
    print("  " + "=" * 56)
    print()
    print("  Reinicie o Claude Desktop para ativar o todos.")
    print()
    print("  Para testar, pergunte ao Claude:")
    print('    "Liste as unidades do SEI"')
    print()
    print("  Para reconfigurar:")
    print("    python3 setup_claude.py")
    print()
    print("  Para remover:")
    print(f'    Apague a entrada "todos" de {config_path}')
    print(f"    E delete a pasta {VENV_HOME}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    banner()

    # Fase 0
    check_python()
    config_path = get_config_path()
    uv_path = detect_uv()
    repo_root = detect_repo_root()
    print()

    # Fase 1
    sei_url = prompt_url()
    # Derive web root from REST URL when possible (split on /sei/).
    # Always prompt so hybrid deployments (separate API hostname) can override.
    derived_web = sei_url.split("/sei/", 1)[0] if sei_url and "/sei/" in sei_url else ""
    sei_web_url = prompt_web_url(default=derived_web)
    sei_usuario = prompt_usuario()
    sei_senha = prompt_senha()
    sei_orgao = prompt_orgao()
    sei_ssl = prompt_ssl()

    print()
    print("  [5b/5] Guardar senha de forma segura no cofre do sistema (Keyring)?")
    print("         Se ativado, a senha não será salva em texto plano no arquivo do Claude.")
    print()
    usar_keyring = confirm("Usar Keyring seguro do sistema?", default_yes=True)

    env: dict = {
        "SEI_USUARIO": sei_usuario,
        "SEI_ORGAO": sei_orgao,
        "SEI_VERIFY_SSL": sei_ssl,
    }
    if sei_url:
        env["SEI_URL"] = sei_url
    if sei_web_url:
        env["SEI_WEB_URL"] = sei_web_url
    if not usar_keyring:
        env["SEI_SENHA"] = sei_senha

    # Fase 2
    print()
    create_venv(uv_path)
    install_package(repo_root, uv_path)

    command = str(todos_command())

    # Fase 3
    print_summary(config_path, command, env, usar_keyring)

    if not confirm("Confirmar e salvar?"):
        print("  Cancelado.")
        sys.exit(0)

    # Fase 4
    print()
    if usar_keyring:
        info("Salvando senha com segurança no chaveiro do sistema...")
        try:
            if sei_web_url:
                sei_root = sei_web_url.rstrip("/")
            elif sei_url and "/sei/" in sei_url:
                sei_root = sei_url.split("/sei/", 1)[0]
            elif sei_url:
                sei_root = sei_url.rstrip("/")
            else:
                sei_root = ""

            instance_url = (
                sei_root.replace("https://", "").replace("http://", "").strip().rstrip("/").lower()
            )
            keyring_user = f"{sei_usuario}@{instance_url}" if instance_url else sei_usuario

            # Chama o python do venv para registrar a senha no keyring do sistema
            # Passa a senha via stdin para evitar expor a credencial em processos/logs
            subprocess.run(
                [
                    str(venv_python()),
                    "-c",
                    "import sys, keyring; keyring.set_password('todos-mcp', sys.argv[1], sys.stdin.read())",
                    keyring_user,
                ],
                input=sei_senha,
                check=True,
                capture_output=True,
                text=True,
            )
            info("Senha salva com sucesso no cofre do sistema.")
        except (subprocess.CalledProcessError, OSError) as e:
            error(f"Erro ao salvar senha no cofre do sistema: {getattr(e, 'stderr', None) or getattr(e, 'stdout', None) or str(e)}")
            warn("A senha será armazenada em texto plano no arquivo de configuração como fallback.")
            env["SEI_SENHA"] = sei_senha

    config = read_config(config_path)
    backup_config(config_path)
    config = merge_sei_server(config, command, env)
    write_config(config_path, config)

    # Fase 5
    print_success(config_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Cancelado pelo usuario.")
        sys.exit(1)
