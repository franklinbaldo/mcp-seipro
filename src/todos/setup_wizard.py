"""Wizard de configuração interativo para o MCP SEI (todos)."""

import os
import sys
import json
import getpass
from pathlib import Path

# Suporte a cores no terminal
def print_cyan(text):
    print(f"\033[96m{text}\033[0m")

def print_green(text):
    print(f"\033[92m{text}\033[0m")

def print_yellow(text):
    print(f"\033[93m{text}\033[0m")

def print_red(text):
    print(f"\033[91m{text}\033[0m")

def run_setup_wizard():
    print_cyan("=====================================================")
    print_cyan("  Configurador do MCP SEI (todos)")
    print_cyan("=====================================================")
    print()

    # 1. Configurar URL e parâmetros do SEI
    print_yellow("[*] Configuração da URL e Instância do SEI")
    web_url = input("Digite ou cole a URL Web do seu SEI (padrão: https://sei.sistemas.ro.gov.br): ").strip()
    if not web_url:
        web_url = "https://sei.sistemas.ro.gov.br"

    sigla_orgao = input("Digite a sigla do seu órgão no SEI (padrão: PGE): ").strip()
    if not sigla_orgao:
        sigla_orgao = "PGE"

    sigla_orgao_sistema = input("Digite a sigla do órgão no sistema (padrão: RO): ").strip()
    if not sigla_orgao_sistema:
        sigla_orgao_sistema = "RO"

    orgao_id = input("Digite o ID do órgão (padrão: 9 para PGE-RO, 0 para outros): ").strip()
    if not orgao_id:
        orgao_id = "9" if sigla_orgao_sistema == "RO" else "0"

    rest_url = input("Digite a URL REST do mod-wssei (deixe em branco se a instância não tiver mod-wssei): ").strip()

    # 2. Obter usuário e senha do SEI
    print()
    print_yellow("[*] Configuração de Usuário e Senha")
    usuario = input("Digite seu usuário do SEI (geralmente CPF ou iniciais): ").strip()
    if not usuario:
        print_red("[ERRO] Usuário é obrigatório.")
        sys.exit(1)

    senha = getpass.getpass("Digite sua senha do SEI (entrada oculta): ").strip()
    if not senha:
        print_red("[ERRO] Senha é obrigatória.")
        sys.exit(1)

    # 3. Salvar senha no Keyring do Sistema (serviço: todos-mcp, chave: usuario@host)
    print()
    print_yellow("[*] Gravando senha com segurança no Keyring do Sistema...")
    
    instance_url = (
        web_url.replace("https://", "")
        .replace("http://", "")
        .strip()
        .rstrip("/")
        .lower()
    )
    keyring_user = f"{usuario}@{instance_url}" if instance_url else usuario

    try:
        import keyring
        keyring.set_password("todos-mcp", keyring_user, senha)
        print_green("[+] Senha armazenada com sucesso no Keyring do Sistema!")
    except Exception as e:
        print_red(f"[ERRO] Falha ao acessar o Keyring do Sistema: {e}")
        print_yellow("[!] A senha não pôde ser salva de forma segura no Keyring nativo.")
        confirm = input("Deseja salvar a senha em texto limpo nas configurações? (s/n): ").strip().lower()
        if confirm != 's':
            print_red("[ERRO] Cancelado pelo usuário.")
            sys.exit(1)
    else:
        # Se salvou no keyring com sucesso, não gravamos a senha no arquivo de configuração
        senha = ""

    # 4. Configurar caminhos dos arquivos MCP por plataforma
    home = Path.home()
    configs_to_update = []

    # Antigravity IDE
    antigravity_config = home / ".gemini" / "antigravity-ide" / "mcp_config.json"
    configs_to_update.append(antigravity_config)

    # Claude Desktop
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        claude_desktop = appdata / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        claude_desktop = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:  # Linux
        claude_desktop = home / ".config" / "Claude" / "claude_desktop_config.json"
    configs_to_update.append(claude_desktop)

    # Claude Code (Global)
    claude_code = home / ".claude.json"
    configs_to_update.append(claude_code)

    # Workspace Local (.mcp.json)
    local_mcp = Path(".") / ".mcp.json"
    configs_to_update.append(local_mcp)

    # Definição do MCP todos
    todos_mcp_config = {
        "command": "todos",
        "env": {
            "SEI_URL": rest_url,
            "SEI_WEB_URL": web_url,
            "SEI_SIGLA_ORGAO": sigla_orgao,
            "SEI_SIGLA_ORGAO_SISTEMA": sigla_orgao_sistema,
            "SEI_USUARIO": usuario,
            "SEI_SENHA": senha,  # Fica vazio se usamos o keyring, ou texto limpo caso contrário
            "SEI_ORGAO": orgao_id
        }
    }

    # Limpar variáveis sensíveis
    if "senha" in locals():
        del senha

    # 5. Atualizar as configurações
    print()
    print_yellow("[*] Atualizando arquivos de configuração MCP...")
    for config_path in configs_to_update:
        try:
            # Garantir que o diretório pai existe
            config_path.parent.mkdir(parents=True, exist_ok=True)

            config_data = {"mcpServers": {}}
            if config_path.exists():
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        config_data = json.load(f)
                        if not isinstance(config_data, dict):
                            config_data = {"mcpServers": {}}
                        if "mcpServers" not in config_data or not isinstance(config_data["mcpServers"], dict):
                            config_data["mcpServers"] = {}
                except Exception as e_read:
                    print_yellow(f"[!] Não foi possível ler o arquivo {config_path.name}: {e_read}. Criando um novo.")

            # Mesclar
            config_data["mcpServers"]["todos"] = todos_mcp_config

            # Salvar
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
            print_green(f"[+] Atualizado: {config_path}")
        except Exception as e_write:
            print_yellow(f"[!] Não foi possível atualizar {config_path}: {e_write}")

    print()
    print_cyan("=====================================================")
    print_green("  Configuração concluída com sucesso!")
    print_green("  Agora você já pode iniciar o Antigravity ou Claude.")
    print_cyan("=====================================================")
