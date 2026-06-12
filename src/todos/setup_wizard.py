"""Wizard de configuração interativo para o MCP SEI (todos)."""

import getpass
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


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
    web_url_input = input(
        "Digite ou cole a URL do seu SEI (ex: https://sei.sistemas.ro.gov.br): "
    ).strip()
    if not web_url_input:
        web_url_input = "https://sei.sistemas.ro.gov.br"

    if not web_url_input.startswith(("http://", "https://")):
        web_url_input = "https://" + web_url_input

    # Tentar detectar órgãos da página de login automaticamente
    parsed = urlparse(web_url_input)
    sei_root = f"{parsed.scheme}://{parsed.netloc}"

    # Extrair parâmetros iniciais da query
    query = parse_qs(parsed.query)

    # Dedução inteligente da sigla_orgao_sistema a partir do hostname
    sigla_orgao_sistema = None
    if "sigla_orgao_sistema" in query:
        sigla_orgao_sistema = query["sigla_orgao_sistema"][0]
    else:
        hostname = parsed.netloc.lower()
        if "ro.gov.br" in hostname:
            sigla_orgao_sistema = "RO"
        else:
            parts = hostname.split(".")
            if len(parts) >= 2:
                if parts[0] in ("sip", "sei") and parts[1] not in (
                    "gov",
                    "com",
                    "org",
                    "net",
                    "edu",
                ):
                    sigla_orgao_sistema = parts[1].upper()
                elif parts[0] not in ("gov", "com", "org", "net", "edu"):
                    sigla_orgao_sistema = parts[0].upper()

            if not sigla_orgao_sistema:
                sigla_orgao_sistema = "SEI"

    sigla_sistema = query.get("sigla_sistema", ["SEI"])[0]

    # Resolver URL de login
    login_url = f"{sei_root}/sip/login.php?sigla_orgao_sistema={sigla_orgao_sistema}&sigla_sistema={sigla_sistema}"

    organs = []
    verify_ssl_disabled = False
    print_yellow("[*] Tentando detectar os órgãos disponíveis no SEI...")
    try:
        import httpx
        from bs4 import BeautifulSoup

        resp = None
        try:
            with httpx.Client(verify=True, follow_redirects=True, timeout=10.0) as client:
                resp = client.get(login_url)
                resp.raise_for_status()
        except httpx.RequestError:
            print_yellow(
                "[!] Alerta de Segurança: Falha ao estabelecer conexão SSL segura com o SEI."
            )
            print_yellow(
                "    Isso ocorre comumente em redes governamentais com proxies ou certificados internos."
            )
            confirm_ssl = (
                input(
                    "Deseja tentar a conexão desativando a verificação de certificado SSL? (s/n): "
                )
                .strip()
                .lower()
            )
            if confirm_ssl == "s":
                verify_ssl_disabled = True
                with httpx.Client(verify=False, follow_redirects=True, timeout=10.0) as client:  # nosec B501
                    resp = client.get(login_url)
                    resp.raise_for_status()
            else:
                raise

        # Executa fora do except para abranger o caminho feliz (SSL verificado com sucesso)
        # e o caminho de fallback (SSL desativado com bypass confirmado pelo usuário).
        if resp is not None:
            # Detectar parâmetros de query após possíveis redirecionamentos
            parsed_final = urlparse(str(resp.url))
            query_final = parse_qs(parsed_final.query)
            sigla_orgao_sistema = query_final.get("sigla_orgao_sistema", [sigla_orgao_sistema])[0]
            sigla_sistema = query_final.get("sigla_sistema", [sigla_sistema])[0]

            soup = BeautifulSoup(resp.text, "html.parser")
            sel = soup.find("select", attrs={"name": "selOrgao"}) or soup.find(
                "select", id="selOrgao"
            )

            if sel:
                for opt in sel.find_all("option"):
                    val = opt.get("value")
                    text = opt.get_text(strip=True)
                    if val and val != "null" and not text.startswith(("-", "Selecione")):
                        organs.append((val, text))
    except Exception as e:
        print_yellow(f"[!] Não foi possível conectar ao login do SEI para listar órgãos: {e}")

    # Seleção de órgão
    if organs:
        print_green("[+] Órgãos detectados com sucesso no seu SEI:")
        for idx, (val, name) in enumerate(organs, 1):
            print(f"  [{idx}] {name} (ID: {val})")

        selection = input(f"Selecione o seu órgão [1-{len(organs)}] (padrão: 1): ").strip()
        if selection.isdigit() and 1 <= int(selection) <= len(organs):
            selected_idx = int(selection) - 1
        else:
            selected_idx = 0

        orgao_id, sigla_orgao = organs[selected_idx]
        # Limpar espaços ou traços do nome para obter apenas a sigla limpa (ex: "PGE-RO" -> "PGE")
        # 1. Divide por hífen circundado ou não por espaços (cobre " - " e "-")
        parts = [p.strip() for p in re.split(r"\s*-\s*", sigla_orgao) if p.strip()]

        # 2. Ignora abreviações de estado (UFs com 2 letras maiúsculas) se houver outros segmentos
        ufs = {
            "AC",
            "AL",
            "AP",
            "AM",
            "BA",
            "CE",
            "DF",
            "ES",
            "GO",
            "MA",
            "MT",
            "MS",
            "MG",
            "PA",
            "PB",
            "PR",
            "PE",
            "PI",
            "RJ",
            "RN",
            "RS",
            "RO",
            "RR",
            "SC",
            "SP",
            "SE",
            "TO",
        }
        if len(parts) > 1:
            parts_without_uf = [p for p in parts if p.upper() not in ufs]
            if parts_without_uf:
                parts = parts_without_uf

        # 3. Se houver mais de um segmento sobressalente, prefere siglas (até 6 letras maiúsculas)
        # Preserva a ordem original: o acrônimo do órgão vem antes da UF/sufixo
        if len(parts) > 1:
            acronyms = [p for p in parts if p.isupper() and len(p) <= 6]
            sigla_orgao = acronyms[0] if acronyms else parts[0]
        else:
            sigla_orgao = parts[0] if parts else sigla_orgao
    else:
        default_sigla = sigla_orgao_sistema or "PGE"
        sigla_orgao = (
            input(f"Digite a sigla do seu órgão no SEI (padrão: {default_sigla}): ").strip()
            or default_sigla
        )
        default_sigla_sistema = sigla_orgao_sistema or "RO"
        sigla_orgao_sistema = (
            input(f"Digite a sigla do órgão no sistema (padrão: {default_sigla_sistema}): ").strip()
            or default_sigla_sistema
        )
        default_id = "9" if default_sigla_sistema == "RO" else "0"
        orgao_id = input(f"Digite o ID do órgão (padrão: {default_id}): ").strip() or default_id

    print_green(f"[+] Configurado para o órgão: {sigla_orgao} (ID: {orgao_id})")

    rest_url = input(
        "Digite a URL REST do mod-wssei (deixe em branco se a instância não tiver mod-wssei): "
    ).strip()

    # 2. Obter usuário e senha do SEI
    print()
    print_yellow("[*] Configuração de Usuário e Senha")
    usuario = input("Digite seu usuário do SEI (geralmente CPF ou iniciais): ").strip()
    if not usuario:
        print_red("[ERRO] Usuário é obrigatório.")
        sys.exit(1)

    senha = getpass.getpass("Digite sua senha do SEI (entrada oculta): ")
    if not senha:
        print_red("[ERRO] Senha é obrigatória.")
        sys.exit(1)

    # 2.5 Salvar senha no Keyring do Sistema (serviço: todos-mcp, chave: usuario@host)
    print()
    print_yellow("[*] Gravando senha com segurança no Keyring do Sistema...")

    instance_url = (
        sei_root.replace("https://", "").replace("http://", "").strip().rstrip("/").lower()
    )
    keyring_user = f"{usuario}@{instance_url}" if instance_url else usuario

    senha_validacao = ""
    try:
        import keyring

        keyring.set_password("todos-mcp", keyring_user, senha)
        print_green("[+] Senha armazenada com sucesso no Keyring do Sistema!")

        # Testar a leitura de volta para garantir que o Keyring está funcional e usá-lo para validação
        senha_validacao = keyring.get_password("todos-mcp", keyring_user)
        if senha_validacao:
            print_green("[+] Validação do Keyring concluída com sucesso (leitura OK)!")
            # Se salvou no keyring com sucesso, não gravamos a senha no arquivo de configuração
            senha = ""
        else:
            print_yellow(
                "[!] Alerta: O Keyring confirmou a gravação, mas retornou vazio na leitura de teste."
            )
            print_yellow("    Usando senha temporária local para a validação.")
            senha_validacao = senha
    except Exception as e:
        print_red(f"[ERRO] Falha ao acessar o Keyring do Sistema: {e}")
        print_yellow("[!] A senha não pôde ser salva de forma segura no Keyring nativo.")
        confirm = (
            input("Deseja salvar a senha em texto limpo nas configurações? (s/n): ").strip().lower()
        )
        if confirm != "s":
            print_red("[ERRO] Cancelado pelo usuário.")
            sys.exit(1)
        senha_validacao = senha

    # 3. Validar as credenciais efetuando um login de teste
    print()
    print_yellow("[*] Validando credenciais com o SEI...")
    try:
        import asyncio
        import logging

        from todos.sei_web_client import SEIWebClient

        # Desativar temporariamente logs verbosos durante a validação
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("todos").setLevel(logging.WARNING)

        web_client = SEIWebClient(
            sei_web_url=sei_root,
            sei_usuario=usuario,
            sei_senha=senha_validacao,
            sei_sigla_orgao=sigla_orgao,
            sei_sigla_orgao_sistema=sigla_orgao_sistema,
            sei_sigla_sistema=sigla_sistema,
            sei_verify_ssl=not verify_ssl_disabled,
        )

        async def do_test_login() -> dict:
            try:
                await web_client.ensure_authenticated()
                unidade = await web_client.unidade_atual()
                return {
                    "nome": web_client._nome_usuario,  # noqa: SLF001
                    "id": web_client._id_usuario,  # noqa: SLF001
                    "orgao": web_client._orgao_usuario,  # noqa: SLF001
                    "unidade": unidade,
                }
            finally:
                await web_client.close()

        client_info = asyncio.run(do_test_login())

        # Limpar a senha do cliente web imediatamente após o login de teste
        web_client._senha = ""  # noqa: SLF001

        print_green("[+] Credenciais validadas com sucesso no SEI!")
        if client_info:
            nome_usuario = client_info.get("nome") or usuario
            id_usuario = client_info.get("id") or "desconhecido"
            orgao_usuario = client_info.get("orgao") or sigla_orgao
            unidade = client_info.get("unidade") or {}

            sigla_unid = unidade.get("sigla") or "N/A"
            nome_unid = unidade.get("nome") or "N/A"
            id_unid = unidade.get("id_unidade") or "N/A"

            print_green(f"    Usuário: {nome_usuario} (ID: {id_usuario}, Órgão: {orgao_usuario})")
            print_green(f"    Unidade Ativa: {sigla_unid} - {nome_unid} (ID: {id_unid})")
    except Exception as e:
        if "web_client" in locals():
            web_client._senha = ""  # noqa: SLF001
        print_red(f"[ERRO] Falha na validação das credenciais no SEI: {e}")
        print_yellow(
            "[!] O login no SEI falhou. Pode ser que o usuário, senha ou órgão estejam incorretos."
        )
        confirm_proceed = (
            input("Deseja gravar as configurações mesmo assim? (s/n): ").strip().lower()
        )
        if confirm_proceed != "s":
            print_red("[ERRO] Configuração cancelada pelo usuário.")
            sys.exit(1)

    # Limpar a senha de validação temporária da memória
    if "senha_validacao" in locals():
        senha_validacao = ""
        del senha_validacao

    # 4. Configurar caminhos dos arquivos MCP por plataforma
    home = Path.home()
    configs_to_update = []

    # Antigravity IDE (apenas se a pasta do gemini existir)
    if (home / ".gemini").exists():
        antigravity_config = home / ".gemini" / "antigravity-ide" / "mcp_config.json"
        configs_to_update.append(antigravity_config)

    # Claude Desktop (apenas se o Claude Desktop estiver instalado/diretório existir)
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        claude_desktop = appdata / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        claude_desktop = (
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )
    else:  # Linux
        claude_desktop = home / ".config" / "Claude" / "claude_desktop_config.json"

    if claude_desktop.parent.exists():
        configs_to_update.append(claude_desktop)

    # Claude Code (Global) - sempre atualizado na home
    claude_code = home / ".claude.json"
    configs_to_update.append(claude_code)

    # Workspace Local (.mcp.json) - apenas se já existir localmente para evitar lixo no cwd
    local_mcp = Path(".mcp.json")
    if local_mcp.exists():
        configs_to_update.append(local_mcp)

    # Definição do MCP todos
    todos_mcp_config = {
        "command": "todos",
        "args": [],
        "env": {
            "SEI_URL": rest_url,
            "SEI_WEB_URL": sei_root,
            "SEI_SIGLA_ORGAO": sigla_orgao,
            "SEI_SIGLA_ORGAO_SISTEMA": sigla_orgao_sistema,
            "SEI_SIGLA_SISTEMA": sigla_sistema,
            "SEI_USUARIO": usuario,
            "SEI_SENHA": senha,  # Fica vazio se usamos o keyring, ou texto limpo caso contrário
            "SEI_ORGAO": orgao_id,
        },
    }

    # Limpar a variável local de senha da memória; a referência no dicionário
    # todos_mcp_config será zerada/sobrescrita logo após a escrita nos arquivos de configuração.
    if "senha" in locals():
        senha = ""
        del senha

    # 5. Atualizar as configurações
    print()
    print_yellow("[*] Atualizando arquivos de configuração MCP...")
    for config_path in configs_to_update:
        try:
            # Garantir que o diretório pai existe
            config_path.parent.mkdir(parents=True, exist_ok=True)

            config_data = {"mcpServers": {}}
            skip_write = False
            if config_path.exists():
                try:
                    with config_path.open(encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            config_data = json.loads(content)
                            if not isinstance(config_data, dict):
                                config_data = {"mcpServers": {}}
                            if "mcpServers" not in config_data or not isinstance(
                                config_data["mcpServers"], dict
                            ):
                                config_data["mcpServers"] = {}
                except Exception as e_read:
                    print_yellow(
                        f"[!] Não foi possível ler o arquivo {config_path.name}: {e_read}. "
                        "Pulando este arquivo para evitar a perda de dados existentes."
                    )
                    skip_write = True

            if skip_write:
                continue

            # Mesclar
            config_data["mcpServers"]["todos"] = todos_mcp_config

            # Salvar
            with config_path.open("w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
            print_green(f"[+] Atualizado: {config_path}")
        except Exception as e_write:
            print_yellow(f"[!] Não foi possível atualizar {config_path}: {e_write}")

    # Limpar senha das variáveis de configuração em memória após a escrita
    if "todos_mcp_config" in locals() and "env" in todos_mcp_config:
        todos_mcp_config["env"]["SEI_SENHA"] = ""

    print()
    print_cyan("=====================================================")
    print_green("  Configuração concluída com sucesso!")
    print_green("  Agora você já pode iniciar o Antigravity ou Claude.")
    print_cyan("=====================================================")
