"""Wizard de configuração interativo para o MCP SEI (todos)."""

import contextlib
import getpass
import json
import os
import re
import shutil
import subprocess as _sp
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
    if not sys.stdin.isatty():
        print_red("[ERRO] 'todos setup' requer um terminal interativo (stdin não é um TTY).")
        sys.exit(1)
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
    if not parsed.netloc or ":" in parsed.netloc:
        print_red("[ERRO] URL inválida. Use o formato: https://sei.exemplo.gov.br")
        sys.exit(1)
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
        except httpx.HTTPStatusError as e:
            print_yellow(
                f"[!] SEI retornou HTTP {e.response.status_code} na página de login. "
                "Continuando sem detecção automática de órgãos."
            )

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

    senha_validacao = senha  # fallback: usa senha local se keyring não estiver disponível
    try:
        import concurrent.futures

        import keyring

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(keyring.set_password, "todos-mcp", keyring_user, senha)
            try:
                future.result(timeout=10)
            except concurrent.futures.TimeoutError as exc:
                msg = (
                    "Keyring bloqueou por mais de 10 segundos. "
                    "Tente: PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring todos setup"
                )
                raise RuntimeError(msg) from exc
        print_green("[+] Senha armazenada com sucesso no Keyring do Sistema!")
    except Exception as e:
        print_red(f"[ERRO] Falha ao acessar o Keyring do Sistema: {e}")
        print_yellow("[!] A senha não pôde ser salva de forma segura no Keyring nativo.")
        confirm = (
            input("Deseja salvar a senha em texto limpo nas configurações? (s/n): ").strip().lower()
        )
        if confirm != "s":
            print_red("[ERRO] Cancelado pelo usuário.")
            sys.exit(1)
    else:
        # set_password funcionou — tentar ler de volta para confirmar e obter a cópia do keyring
        try:
            lida = keyring.get_password("todos-mcp", keyring_user)
            if lida:
                print_green("[+] Validação do Keyring concluída com sucesso (leitura OK)!")
                senha_validacao = lida
                senha = ""  # não precisamos gravar a senha no arquivo de config
            else:
                print_yellow(
                    "[!] Alerta: O Keyring confirmou a gravação, mas retornou vazio na leitura de teste."
                )
                print_yellow("    Usando senha local para a validação.")
                senha = ""  # keyring tem a senha; não precisamos dela no config
        except Exception:
            # leitura falhou, mas a gravação foi bem-sucedida — usar senha local só para validação
            print_yellow(
                "[!] Não foi possível ler de volta do Keyring. Usando senha local para validação."
            )
            senha = ""  # keyring tem a senha; não precisamos dela no config

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
                info: dict = {
                    "nome": web_client._nome_usuario,  # noqa: SLF001
                    "id": web_client._id_usuario,  # noqa: SLF001
                    "orgao": web_client._orgao_usuario,  # noqa: SLF001
                    "unidade": {},
                }
                # unidade_atual é informação de display — não deve bloquear a validação
                with contextlib.suppress(Exception):
                    info["unidade"] = await web_client.unidade_atual()
                return info
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
    senha_validacao = ""
    del senha_validacao

    # 4. Preparar variáveis de ambiente MCP
    mcp_env = {
        "SEI_URL": rest_url,
        "SEI_WEB_URL": sei_root,
        "SEI_SIGLA_ORGAO": sigla_orgao,
        "SEI_SIGLA_ORGAO_SISTEMA": sigla_orgao_sistema,
        "SEI_SIGLA_SISTEMA": sigla_sistema,
        "SEI_USUARIO": usuario,
        "SEI_SENHA": senha,  # Vazio se keyring foi usado
        "SEI_ORGAO": orgao_id,
    }

    # Limpar senha local imediatamente após montar o dicionário
    senha = ""
    del senha

    # 5. Atualizar as configurações
    print()
    print_yellow("[*] Atualizando arquivos de configuração MCP...")

    home = Path.home()
    claude_cli = shutil.which("claude")

    def _mcp_add_via_cli(scope: str, cwd: Path | None = None) -> bool:
        """Usa `claude mcp add` para registrar o servidor. Retorna True se ok."""
        if not claude_cli:
            return False
        env_args = [item for k, v in mcp_env.items() for item in ("-e", f"{k}={v}")]
        cmd = [claude_cli, "mcp", "add", "-s", scope, *env_args, "todos", "todos"]
        run_kw = {"capture_output": True, "text": True, "cwd": str(cwd or Path.cwd())}
        try:
            _sp.run(cmd, check=True, **run_kw)  # noqa: S603
        except _sp.CalledProcessError as e:
            if "already exists" not in (e.stderr or "") and "already exists" not in (e.stdout or ""):
                return False
            with contextlib.suppress(Exception):
                _sp.run([claude_cli, "mcp", "remove", "-s", scope, "todos"], check=True, **run_kw)  # noqa: S603
                _sp.run(cmd, check=True, **run_kw)  # noqa: S603
                return True
            return False
        except Exception:
            return False
        else:
            return True

    def _mcp_add_via_json(config_path: Path) -> bool:
        """Fallback: edita o JSON diretamente. Retorna True se ok."""
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_data: dict = {"mcpServers": {}}
            if config_path.exists():
                content = config_path.read_text(encoding="utf-8").strip()
                if content:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        config_data = parsed
                    if not isinstance(config_data.get("mcpServers"), dict):
                        config_data["mcpServers"] = {}
            config_data["mcpServers"]["todos"] = {
                "command": "todos",
                "args": [],
                "env": dict(mcp_env),
            }
            with config_path.open("w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
        except Exception:
            return False
        else:
            return True

    # Antigravity IDE — atualiza todos os caminhos conhecidos que existirem
    # ~/.gemini/antigravity-ide/mcp_config.json  (versão atual)
    # ~/.gemini/config/mcp_config.json           (caminho documentado / versões futuras)
    for _ag_path in [
        home / ".gemini" / "antigravity-ide" / "mcp_config.json",
        home / ".gemini" / "config" / "mcp_config.json",
    ]:
        if _ag_path.exists() or _ag_path.parent.exists():
            if _mcp_add_via_json(_ag_path):
                print_green(f"[+] Atualizado: {_ag_path}")
            else:
                print_yellow(f"[!] Não foi possível atualizar {_ag_path}")

    # Claude Desktop (apenas se o diretório existir)
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        claude_desktop = appdata / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        claude_desktop = (
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )
    else:
        claude_desktop = home / ".config" / "Claude" / "claude_desktop_config.json"

    if claude_desktop.parent.exists():
        if _mcp_add_via_json(claude_desktop):
            print_green(f"[+] Atualizado: {claude_desktop}")
        else:
            print_yellow(f"[!] Não foi possível atualizar {claude_desktop}")

    # Claude Code (Global) — usa `claude mcp add -s user` se disponível
    if claude_cli and _mcp_add_via_cli("user"):
        print_green("[+] Atualizado: Claude Code (global) via `claude mcp add -s user`")
    elif _mcp_add_via_json(home / ".claude.json"):
        print_green(f"[+] Atualizado: {home / '.claude.json'}")
    else:
        print_yellow(f"[!] Não foi possível atualizar {home / '.claude.json'}")

    # Workspace Local (.mcp.json) — usa `claude mcp add -s project` se disponível
    # Procura o .mcp.json no CWD e em diretórios pais (funciona quando rodado de subdiretório)
    _mcp_search = Path.cwd()
    _workspace_dir: Path | None = None
    for _ in range(5):
        if (_mcp_search / ".mcp.json").exists():
            _workspace_dir = _mcp_search
            break
        parent = _mcp_search.parent
        if parent == _mcp_search:
            break
        _mcp_search = parent

    if _workspace_dir is not None:
        if claude_cli and _mcp_add_via_cli("project", cwd=_workspace_dir):
            print_green(f"[+] Atualizado: {_workspace_dir / '.mcp.json'} via `claude mcp add -s project`")
        elif _mcp_add_via_json(_workspace_dir / ".mcp.json"):
            print_green(f"[+] Atualizado: {_workspace_dir / '.mcp.json'}")
        else:
            print_yellow(f"[!] Não foi possível atualizar {_workspace_dir / '.mcp.json'}")

    # Codex CLI — usa `codex mcp add` se disponível, senão edita ~/.codex/config.toml
    codex_cli = shutil.which("codex")
    codex_config = home / ".codex" / "config.toml"
    if codex_cli or codex_config.exists():
        added_codex = False
        if codex_cli:
            env_args = [item for k, v in mcp_env.items() for item in ("-e", f"{k}={v}")]
            cmd_codex = [codex_cli, "mcp", "add", "todos", "--", "todos", *env_args]
            try:
                _sp.run(cmd_codex, check=True, capture_output=True, text=True)  # noqa: S603
            except _sp.CalledProcessError as e:
                if "already" in (e.stderr or "") or "already" in (e.stdout or ""):
                    with contextlib.suppress(Exception):
                        _sp.run([codex_cli, "mcp", "remove", "todos"], check=True, capture_output=True, text=True)  # noqa: S603
                        _sp.run(cmd_codex, check=True, capture_output=True, text=True)  # noqa: S603
                        added_codex = True
                        print_green("[+] Atualizado: Codex (global) via `codex mcp add`")
            except Exception:
                pass
            else:
                added_codex = True
                print_green("[+] Atualizado: Codex (global) via `codex mcp add`")
        if not added_codex:
            # Fallback: edita config.toml diretamente com a seção [mcpServers.todos]
            try:
                codex_config.parent.mkdir(parents=True, exist_ok=True)
                existing = codex_config.read_text(encoding="utf-8") if codex_config.exists() else ""
                existing = re.sub(
                    r"\[mcpServers\.todos\][^\[]*",
                    "",
                    existing,
                    flags=re.DOTALL,
                ).rstrip()
                env_lines = "\n".join(f"  {k} = {json.dumps(v)}" for k, v in mcp_env.items())
                block = f'\n\n[mcpServers.todos]\ncommand = "todos"\nargs = []\n[mcpServers.todos.env]\n{env_lines}\n'
                codex_config.write_text(existing + block, encoding="utf-8")
                print_green(f"[+] Atualizado: {codex_config}")
            except Exception as e_codex:
                print_yellow(f"[!] Não foi possível atualizar {codex_config}: {e_codex}")

    # Limpar senha do dicionário em memória
    mcp_env["SEI_SENHA"] = ""

    print()
    print_cyan("=====================================================")
    print_green("  Configuração concluída com sucesso!")
    print_green("  Agora você já pode iniciar o Antigravity, Claude ou Codex.")
    print_cyan("=====================================================")
