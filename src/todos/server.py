"""MCP Server genérico para o SEI (Sistema Eletrônico de Informações)."""

import asyncio
import base64
import json
import logging
import os
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from typing import Literal, cast

import httpx
from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from todos import access_control
from todos.catalog_cache import get_catalog_cache
from todos.html_utils import (
    html_to_markdown,
    html_to_text,
    pdf_to_markdown,
    pdf_to_text,
    sanitize_iso8859,
)
from todos.sei_backend import SEIBackend
from todos.sei_client import SEIClient
from todos.sei_styles import (
    SEI_STYLES,
    STYLE_SHORTCUTS,
    html_referencia_sei,
)
from todos.sei_web_client import SEIWebClient

logger = logging.getLogger(__name__)

MAX_BINARY_SIZE = 10 * 1024 * 1024  # 10 MB

# Detecta modo HTTP (Railway injeta PORT)
_http_mode = bool(os.environ.get("PORT"))
_http_port = int(os.environ.get("PORT", 8000))  # noqa: PLW1508


@asynccontextmanager
async def lifespan(_server: FastMCP):  # noqa: ANN201, D103
    try:
        if _http_mode:
            clients: dict[str, SEIClient] = {}
            web_clients: dict[str, SEIWebClient] = {}
            try:
                yield {"sei_by_session": clients, "sei_web_by_session": web_clients}
            finally:
                await asyncio.gather(
                    *(client.close() for client in clients.values()),
                    *(client.close() for client in web_clients.values()),
                    return_exceptions=True,
                )
        else:
            # Modo stdio: clients com credenciais das env vars
            client = SEIClient()
            web_client = SEIWebClient()
            try:
                # Login eager com detalhada=True: popula _unidade_atual e
                # hdnDetalhadoNroItens (total real de processos abertos na unidade)
                with suppress(Exception):
                    await web_client.fetch_inbox(detalhada=True)
                yield {"sei": client, "sei_web": web_client}
            finally:
                await client.close()
                await web_client.close()
    finally:
        await get_catalog_cache().close()


def _store_session_client(clients: dict, session_id: str, client: object) -> None:
    """Store a session-scoped client, evicting the oldest entry if the pool is full."""
    if session_id in clients:
        return
    max_sessions = int(os.environ.get("SEI_MAX_SESSIONS", "100"))
    if len(clients) >= max_sessions:
        oldest = next(iter(clients))
        clients.pop(oldest)
        logger.warning("session pool at limit (%d); evicted oldest session", max_sessions)
    clients[session_id] = client


def _get_client(ctx: Context | None) -> SEIClient:
    """Obtém o SEIClient REST, criando sob demanda em modo HTTP."""
    if ctx is None:
        raise ValueError("Contexto MCP nao disponivel.")  # noqa: EM101, TRY003

    if _http_mode:
        from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

        from todos.auth import get_sei_credentials_from_token  # noqa: PLC0415

        access_token = get_access_token()
        if not access_token:
            raise ValueError("Autenticacao necessaria. Reconecte o MCP.")  # noqa: EM101, TRY003
        creds = get_sei_credentials_from_token(access_token.token)
        if not creds:
            raise ValueError("Token invalido ou expirado. Reconecte o MCP.")  # noqa: EM101, TRY003

        clients = ctx.lifespan_context["sei_by_session"]
        client = clients.get(ctx.session_id)
        if client is not None:
            return client
        client = SEIClient(**creds)
        _store_session_client(clients, ctx.session_id, client)
        return client

    client = ctx.lifespan_context.get("sei")
    if client is not None:
        return client
    raise ValueError("SEIClient nao configurado. Verifique as variaveis de ambiente.")  # noqa: EM101, TRY003


def _get_web_client(ctx: Context | None) -> SEIWebClient:
    """Obtém o SEIWebClient (scraper), criando sob demanda em modo HTTP.

    O scraper mantém estado de sessão (cookies + infra_hash) e por isso é
    instanciado uma vez por contexto, não por chamada.
    """
    if ctx is None:
        raise ValueError("Contexto MCP nao disponivel.")  # noqa: EM101, TRY003

    if _http_mode:
        from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

        from todos.auth import get_sei_credentials_from_token  # noqa: PLC0415

        access_token = get_access_token()
        if not access_token:
            raise ValueError("Autenticacao necessaria. Reconecte o MCP.")  # noqa: EM101, TRY003
        creds = get_sei_credentials_from_token(access_token.token)
        if not creds:
            raise ValueError("Token invalido ou expirado. Reconecte o MCP.")  # noqa: EM101, TRY003

        clients = ctx.lifespan_context["sei_web_by_session"]
        client = clients.get(ctx.session_id)
        if client is not None:
            return client
        client = SEIWebClient(**creds)
        _store_session_client(clients, ctx.session_id, client)
        return client

    client = ctx.lifespan_context.get("sei_web")
    if client is not None:
        return client
    raise ValueError("SEIWebClient nao configurado.")  # noqa: EM101, TRY003


def _get_backend(ctx: Context | None) -> SEIBackend:
    """Retorna SEIBackend com REST + web para o contexto atual.

    SEIWebClient é criado de forma lazy: se não estiver configurado (ex: modo
    REST-only sem SEI_WEB_URL), usa instância vazia que nunca será invocada
    enquanto has_rest=True, evitando ValueError em instalações REST-only.
    """
    rest = _get_client(ctx)
    try:
        web = _get_web_client(ctx)
    except ValueError:
        web = SEIWebClient()  # instância vazia; só usada se has_rest = False
    return SEIBackend(rest, web)


mcp = FastMCP(
    "sei",
    instructions=(
        "MCP Server para o SEI (Sistema Eletrônico de Informações). "
        "Permite gerenciar processos, documentos, tramitação e assinatura. "
        "CONTEXTO: leia o resource sei://status antes de qualquer operação — "
        "ele mostra a instância SEI conectada e a unidade ativa do usuário. "
        "ASSINATURA: as credenciais do usuário já estão configuradas no servidor. "
        "NUNCA peça login ou senha ao usuário para assinar. Basta chamar "
        "sei_assinar_documento com o id do documento e o cargo. Se não souber "
        "o cargo, chame sem cargo para obter a lista e pergunte ao usuário. "
        "Fluxo típico: sei_unidade_atual → sei_trocar_unidade (se necessario) → "
        "sei_listar_processos → "
        "sei_consultar_processo (obter IdProcedimento) → sei_arvore_processo → "
        "sei_ler_documento. Para criar docs: sei_pesquisar_tipos_documento → "
        "sei_criar_documento → sei_listar_secoes → sei_editar_secao. "
        "Ao gerar HTML para documentos, use as classes CSS padronizadas do SEI. "
        "DESPACHOS: Texto_Alinhado_Esquerda com âncora SEI no destinatário "
        "(<span class='ancoraSei interessadoSeiPro' data-id='ID_UNIDADE'>SIGLA - Nome</span>) "
        "para vincular à unidade na tramitação. "
        "Texto_Justificado+<strong> (assunto), "
        "Paragrafo_Numerado_Nivel1 (corpo, autonumera 1. 2. 3.), "
        "Texto_Justificado_Recuo_Primeira_Linha (fecho), "
        "Texto_Centralizado_Maiusculas (signatário), Texto_Centralizado (cargo). "
        "NOTAS TÉCNICAS e PARECERES: Item_Nivel1/2/3/4 para títulos de seção "
        "(equivalem a H1/H2/H3/H4 ou #/##/###/####, autonumeram 1. 1.1. 1.1.1.), "
        "Paragrafo_Numerado_Nivel1 para parágrafos do corpo, "
        "Item_Alinea_Letra para alíneas (autonumera a, b, c — NUNCA escrever a) b) no texto), "
        "Item_Inciso_Romano para incisos (autonumera I, II, III — NUNCA escrever I - II - no texto). "
        "REGRA: toda numeração/enumeração deve usar as classes CSS, nunca texto manual. "
        "Use sei_estilos para consultar todos os estilos disponíveis. "
        "Ao citar documentos SEI no texto, use sei_gerar_referencia para "
        "gerar hiperlinks dinâmicos (<a class='ancoraSei'>) que o SEI "
        "renderiza como links clicáveis na interface web. "
        "IMPORTANTE: Quando o usuário mencionar 'SEI XXXX', 'SEI nº XXXX' ou "
        "'número SEI XXXX', use sei_ler_documento diretamente com o número — "
        "a tool resolve automaticamente o id interno via pesquisa Solr. "
        "Para buscar sem ler, use sei_buscar_documento. "
        "Quando o usuário pedir para ver documentos/árvore de um processo, "
        "use sei_arvore_processo e apresente como tabela markdown. Use emojis "
        "para tipo de documento: 📄 = Interno (HTML), 📎 = Externo (PDF). "
        "Colunas: #, 📄/📎, Tipo do Documento, Protocolo, Unidade, Tamanho, "
        "✍️ Assinado, 🚫 Cancelado, 👁 Visualizar, 🔒 Bloqueado. "
        "Use ✅ para sim e · para não. Se houver múltiplos volumes "
        "(campo total_volumes > 1), separe visualmente por volume. "
        "VERSÃO: Todos os endpoints funcionam com mod-wssei 2.0.0+ (SEI 4.0.x+), "
        "exceto sei_listar_relacionamentos que requer mod-wssei 3.0.2+ (SEI 5.0.x). "
        "Compatibilidade: SEI 4.0.x→mod-wssei 2.0.x | SEI 4.1.1→2.2.0 | SEI 5.0.x→3.0.x. "
        "Se um endpoint falhar com erro inesperado (404, método não encontrado), "
        "use sei_versao para verificar a versão e informe ao usuário qual versão "
        "do SEI/mod-wssei é necessária. Pergunte a versão do SEI ao usuário caso precise."
    ),
    lifespan=lifespan,
)


@mcp.resource("sei://status")
async def sei_status_resource(ctx: Context) -> str:
    """Unidade SEI ativa, usuário logado, instância e unidades disponíveis. Leia ao iniciar."""
    web = _get_web_client(ctx)
    try:
        unidade, unidades = await asyncio.gather(
            web.unidade_atual(),
            web.listar_unidades(),
        )
        sigla = unidade.get("sigla", "?")
        nome = unidade.get("nome", "?")
        web_url = os.environ.get("SEI_WEB_URL") or os.environ.get("SEI_URL", "?")
        nome_usuario = web._nome_usuario  # noqa: SLF001
        id_usuario = web._id_usuario or web._usuario  # noqa: SLF001
        orgao_usuario = web._orgao_usuario  # noqa: SLF001
        if nome_usuario:
            usuario_str = f"{nome_usuario} (id: {id_usuario}" + (
                f", órgão: {orgao_usuario})" if orgao_usuario else ")"
            )
        else:
            usuario_str = id_usuario
        # hdnDetalhadoNroItens reflete o cap da página (500), não o total global.
        # Se retornou 500, há múltiplas páginas — exibir como "500+".
        total = int(web._form_hidden.get("hdnDetalhadoNroItens", "0") or "0")  # noqa: SLF001
        if total == 0:
            total_str = "não disponível"
        elif total >= 500:  # noqa: PLR2004
            total_str = "500+ (múltiplas páginas — use sei_listar_processos para listar)"
        else:
            total_str = str(total)

        linhas = [
            f"Instância SEI: {web_url}",
            f"Usuário: {usuario_str}",
            f"Unidade ativa: {sigla} — {nome}",
            f"Processos abertos na unidade: {total_str}",
            "",
            "Unidades disponíveis:",
        ]
        for u in unidades:
            marker = "▶" if u.get("sigla") == sigla else " "
            linhas.append(f"  {marker} {u['sigla']} — {u['nome']} (id: {u.get('id_unidade', '?')})")
        return "\n".join(linhas)
    except Exception as exc:  # noqa: BLE001
        return f"Status: erro ao obter sessão — {exc}"


class _ConsentimentoRestrito(BaseModel):
    """Schema de elicitInput para consentimento de acesso a documento restrito."""

    autorizo_acesso: bool = Field(
        default=False,
        description=(
            "Marque para autorizar a leitura do conteúdo restrito. Ao marcar, "
            "você declara ciência dos riscos de LGPD/LAI/sigilo e assume "
            "responsabilidade pelo compartilhamento da informação fora do SEI."
        ),
    )


_ELICIT_TIMEOUT_S = float(os.environ.get("SEI_ELICIT_TIMEOUT_S", "30"))


def _cliente_suporta_elicit(ctx: Context | None) -> bool:
    """Verifica via MCP capabilities se o cliente declarou suporte a elicit."""
    if ctx is None:
        return False
    try:
        client_params = ctx.session.client_params
        if client_params is None:
            return False
        caps = client_params.capabilities
    except Exception:  # noqa: BLE001
        return False
    return getattr(caps, "elicitation", None) is not None


async def _solicitar_consentimento_via_elicit(
    ctx: Context | None,
    nivel: str | None,  # noqa: ARG001
    rotulo: str,
    hipotese: str | None,
    alvo: dict,
) -> str:
    """Solicita consentimento ao usuário via MCP elicitInput.

    Retorna:
      - "aceitou": usuário marcou autorizo_acesso=True
      - "recusou": usuário rejeitou ou desmarcou
      - "nao_suportado": cliente MCP não implementa elicitInput, ou não
        respondeu dentro de SEI_ELICIT_TIMEOUT_S — cair no fallback JSON
    """
    if not _cliente_suporta_elicit(ctx):
        return "nao_suportado"

    riscos_txt = "\n".join(f"• {r}" for r in access_control.riscos_padrao())
    hl_txt = f"\nHipótese legal: {hipotese}" if hipotese else ""
    alvo_txt = ""
    if alvo.get("tipo") == "documento":
        alvo_txt = f"\nDocumento: id {alvo.get('id')} (tipo {alvo.get('tipo_documento', '?')})"
    elif alvo.get("tipo") == "processo":
        alvo_txt = f"\nProcesso: {alvo.get('protocolo')}"

    message = (
        f"⚠ Documento/processo classificado como {rotulo} no SEI.{hl_txt}{alvo_txt}\n\n"
        f"Riscos:\n{riscos_txt}\n\n"
        "Marque a opção abaixo para autorizar a leitura do conteúdo bruto. "
        "Se não autorizar, o MCP retornará apenas um aviso ao modelo."
    )

    if ctx is None:
        return "nao_suportado"
    try:
        result = await asyncio.wait_for(
            ctx.elicit(message=message, response_type=_ConsentimentoRestrito),
            timeout=_ELICIT_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning(
            f"elicit timeout após {_ELICIT_TIMEOUT_S}s — cliente não respondeu, "  # noqa: G004
            "caindo no fallback JSON"
        )
        return "nao_suportado"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"elicit falhou ({type(e).__name__}: {e}) — fallback JSON")  # noqa: G004
        return "nao_suportado"

    if result.action == "accept" and result.data and result.data.autorizo_acesso:
        return "aceitou"
    return "recusou"


def _gate_bloqueio(
    nivel: str | None,
    tipo: str,
    id_doc: str,
    tipo_documento: str,
    processo: str,
) -> dict | None:
    """Return the block payload if nivel requires a disclaimer, else None."""
    if not access_control.precisa_disclaimer(nivel):
        return None
    alvo = {"tipo": tipo, "id": str(id_doc), "tipo_documento": tipo_documento, "processo": processo}
    return access_control.construir_aviso_bloqueio(nivel, None, alvo)


async def _aplicar_gate_documento_web(
    web: SEIWebClient,
    processo: str,
    id_documento: str,
    tipo_documento: str,
    confirmou: bool,  # noqa: FBT001
) -> dict | None:
    """Gate de acesso restrito para os fallbacks web-only.

    Consulta os metadados scrapeados de documento_consultar e, se o documento
    for restrito/sigiloso sem consentimento, retorna o payload de bloqueio.
    Retorna None quando o acesso está liberado ou quando a consulta falha
    (fail-open: sem metadados não bloqueia).
    """
    if confirmou or access_control.env_permite_restritos():
        return None
    try:
        meta = await web.consultar_documento_web(processo, id_documento)
    except Exception:  # noqa: BLE001
        logger.warning("gate web-only: consulta de metadados falhou — prossegue fail-open")
        return None
    nivel = access_control.extrair_nivel_web(meta)
    return _gate_bloqueio(nivel, "documento", id_documento, tipo_documento, processo)


async def _aplicar_gate_documento(  # noqa: PLR0911
    ctx: Context | None,
    client: SEIClient,
    id_documento: str,
    tipo_documento: str,
    confirmou: bool,  # noqa: FBT001
) -> tuple[str, dict | None, str]:
    """Resolve metadados e aplica o gate de acesso para um documento.

    Retorna (acao, payload, erro):
      - acao="liberar": prossiga; payload é o disclaimer acompanhante (ou None
        se público)
      - acao="bloquear": retorne payload (JSON de bloqueio) ao caller
      - acao="recusou": retorne payload (JSON de recusa) ao caller
      - acao="erro": retorne erro (string) ao caller
    """
    try:
        if tipo_documento == "X":
            meta = await client.consultar_documento_externo(id_documento)
        else:
            meta = await client.consultar_documento_interno(id_documento)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        low = msg.lower()
        if "não autorizado" in low or "nao autorizado" in low:
            return (
                "erro",
                None,
                (
                    f"SEI retornou 'não autorizado' para o id {id_documento!r}. "
                    "Verifique se você passou o id INTERNO do documento (ex.: 3149544) "
                    "e não o número SEI / protocoloFormatado (ex.: 2867926). "
                    "Se tiver apenas o número SEI, use sei_buscar_documento ou "
                    "sei_ler_documento (que faz auto-resolução)."
                ),
            )
        return ("erro", None, f"Falha ao consultar metadados: {msg}")

    nivel, hipotese = access_control.extrair_nivel(meta)
    alvo = {
        "tipo": "documento",
        "id": str(id_documento),
        "tipo_documento": tipo_documento,
    }

    if not access_control.precisa_disclaimer(nivel):
        return ("liberar", None, "")

    if confirmou or access_control.env_permite_restritos():
        return (
            "liberar",
            access_control.construir_disclaimer_acompanhante(nivel, hipotese, alvo),
            "",
        )

    rotulo = access_control.ROTULOS.get(nivel, "Restrito")
    consent = await _solicitar_consentimento_via_elicit(ctx, nivel, rotulo, hipotese, alvo)

    if consent == "aceitou":
        return (
            "liberar",
            access_control.construir_disclaimer_acompanhante(nivel, hipotese, alvo),
            "",
        )
    if consent == "recusou":
        return (
            "recusou",
            {
                "tipo_resposta": "consentimento_recusado",
                "mensagem_para_usuario_humano": (
                    f"Acesso ao conteúdo {rotulo.lower()} NÃO foi autorizado pelo "
                    "usuário humano. Nenhum conteúdo bruto foi entregue ao modelo."
                ),
                "instrucao_para_modelo": (
                    "O usuário humano recusou expressamente o acesso ao conteúdo "
                    "restrito via MCP elicitInput. NÃO tente caminhos alternativos "
                    "(troca de unidade, outras tools de leitura, IDs alternativos). "
                    "Confirme ao usuário que a recusa foi registrada e ofereça "
                    "ações que não dependam do conteúdo bruto."
                ),
                "alvo": alvo,
                "nivel_acesso": nivel,
            },
            "",
        )
    return (
        "bloquear",
        access_control.construir_aviso_bloqueio(nivel, hipotese, alvo),
        "",
    )


async def _resolver_processo(client: SEIClient, referencia: str) -> str:
    """Resolve uma referência de processo para o IdProcedimento.

    Aceita:
    - IdProcedimento numérico (ex: "683589") — usa direto
    - Protocolo formatado (ex: "50300.018905/2018-67") — consulta na API

    Retorna o IdProcedimento (str).
    """
    referencia = referencia.strip()
    # Se contém ponto ou barra, é protocolo formatado
    if "." in referencia or "/" in referencia:
        proc = await client.consultar_processo(referencia)
        return str(proc.get("IdProcedimento", ""))
    return referencia


def _json(data) -> str:  # noqa: ANN001
    return json.dumps(data, ensure_ascii=False, indent=2)


def _error(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tools de unidade e usuário
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_unidade_atual(ctx: Context) -> str:
    """Retorna a unidade/setor ativo na sessao atual do SEI.

    Informa id_unidade, sigla e nome. Use antes de listar ou alterar processos
    para confirmar em qual caixa as operacoes serao executadas.
    """
    try:
        client = _get_web_client(ctx)
        result = await client.unidade_atual()
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_unidades(ctx: Context) -> str:
    """Lista as unidades às quais o usuário autenticado tem acesso no SEI.

    Retorna id, sigla e nome de cada unidade. Use o id para trocar
    de unidade com sei_trocar_unidade.
    """
    try:
        client = _get_web_client(ctx)
        units = await client.listar_unidades()
        return _json({"data": units, "total": len(units)})
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_trocar_unidade(id_unidade: str, ctx: Context) -> str:
    """Troca a unidade ativa do usuário no SEI.

    Aceita o ID interno ou a sigla da unidade, por exemplo `PGE-PPI`.
    Após trocar, operações como sei_listar_processos mostrarão
    a caixa da nova unidade. Use sei_listar_unidades para ver
    as unidades disponíveis.
    """
    try:
        web = _get_web_client(ctx)
        result = await web.trocar_unidade(id_unidade)
        # Keep REST client in sync on hybrid installs so unit-sensitive REST
        # tools use the same unit as the web client.
        try:
            rest = _get_client(ctx)
            await rest.trocar_unidade(result.get("id_unidade", id_unidade))
        except Exception as rest_err:  # noqa: BLE001
            logger.debug("REST unit sync failed (best-effort): %s", rest_err)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_unidades(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa unidades disponíveis no SEI por nome ou sigla.

    Útil para encontrar o ID de uma unidade destino ao tramitar processos.
    Paginação: pagina=0 é a primeira página, pagina=1 a segunda, etc.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_unidades(filtro=filtro, limit=limit, start=pagina)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_usuarios(
    filtro: str = "",
    apenas_unidade: bool = True,  # noqa: FBT001, FBT002
    ctx: Context | None = None,
) -> str:
    """Lista usuários no SEI, com filtro por nome ou sigla.

    - apenas_unidade=true (padrão): só usuários com permissão na unidade
      atual — ideal para atribuição de processos
    - apenas_unidade=false: todos os usuários do órgão

    Use o campo id_usuario retornado para sei_atribuir_processo.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_usuarios(filtro=filtro, apenas_unidade=apenas_unidade)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de leitura
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_consultar_processo(protocolo_formatado: str, ctx: Context) -> str:  # noqa: C901
    """Consulta um processo SEI pelo número de protocolo formatado.

    Exemplo de protocolo: 50300.000123/2025-00

    Implementação **híbrida**: combina REST mod-wssei (campos estruturados)
    com scraper do frontend web (lista completa de documentos da árvore).
    As duas fontes rodam em paralelo via asyncio.gather.

    Campos da REST (`/processo/consultar` + `/processo/consultar/{id}`):
    - IdProcedimento, ProtocoloProcedimentoFormatado, NomeTipoProcedimento
    - especificacao, assuntos[], interessados[], observacoes[]
    - nivelAcesso, hipoteseLegal, grauSigilo

    Campos do scraper web (`procedimento_visualizar` / arvore_montar.php):
    - documentos[]: lista completa de documentos com id, label, tipo
    - relacionados[]: processos relacionados (cards na sidebar)

    Se o scraper web falhar (ex: processo não está na inbox da unidade atual),
    a tool ainda retorna os campos REST. Se a REST falhar, retorna pelo menos
    o que o scraper conseguiu extrair.

    Quando o processo é restrito ou sigiloso (nivelAcesso 1 ou 2), a resposta
    inclui o campo `_aviso_acesso` — um aviso INFORMATIVO de privacidade,
    NÃO um erro de permissão. Os metadados foram retornados com sucesso.
    """
    try:
        client = _get_client(ctx)
        web = _get_web_client(ctx)
        if web._inbox_url is None:  # noqa: SLF001
            try:
                await web.login()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"web login falhou, seguindo só com REST: {e}")  # noqa: G004

        # roda REST completo e web em paralelo; suporta falha individual
        rest_task = asyncio.create_task(client.consultar_processo_completo(protocolo_formatado))
        web_task = asyncio.create_task(web.consultar_processo(protocolo_formatado))
        rest_result, web_result = await asyncio.gather(rest_task, web_task, return_exceptions=True)

        merged: dict = {}
        warnings: list[str] = []

        if isinstance(rest_result, Exception):
            warnings.append(f"REST falhou: {rest_result}")
        elif isinstance(rest_result, dict):
            merged.update(rest_result)

        if isinstance(web_result, Exception):
            warnings.append(f"Web scraper falhou: {web_result}")
        elif isinstance(web_result, dict):
            # Web traz documentos[], relacionados[] e id_procedimento (snake_case).
            # Não sobrescreve campos da REST que tenham nomes parecidos —
            # REST é a fonte canônica para metadata; web complementa com docs.
            for k, v in web_result.items():
                if k not in merged:
                    merged[k] = v

        if not merged:
            return _error("Ambas as fontes (REST e Web) falharam: " + " | ".join(warnings))

        if warnings:
            merged["_warnings"] = warnings

        nivel, hipotese = access_control.extrair_nivel(merged)
        if access_control.precisa_disclaimer(nivel):
            merged["_aviso_acesso"] = access_control.construir_disclaimer_acompanhante(
                nivel,
                hipotese,
                alvo={"tipo": "processo", "protocolo": protocolo_formatado},
            )

        return _json(merged)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_arvore_processo(
    protocolo_formatado: str,
    ctx: Context | None = None,
) -> str:
    """Mostra a árvore completa de documentos de um processo SEI.

    Implementação via scraper web (~10× mais rápido que REST: ~1 s vs ~12 s).
    Parseia arvore_montar.php para extrair id, tipo, sigla da unidade geradora
    e número SEI de cada documento.

    Aceita o protocolo formatado (ex: 50300.000123/2025-00).

    Para ler o conteúdo de um documento, use sei_ler_documento com o id.
    """
    try:
        web = _get_web_client(ctx)
        result = await web.listar_documentos(protocolo_formatado)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_documentos(
    protocolo_formatado: str,
    ctx: Context | None = None,
) -> str:
    """Lista todos os documentos de um processo SEI.

    Implementação via scraper web (~10× mais rápido que REST).
    Aceita o protocolo formatado (ex: 50300.000123/2025-00).

    Cada documento tem: id, nome_composto, tipo_documento, sigla_unidade,
    numero_sei. Para ler o conteúdo, use sei_ler_documento com o id.
    """
    try:
        web = _get_web_client(ctx)
        result = await web.listar_documentos(protocolo_formatado)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_buscar_documento(  # noqa: C901
    numero_sei: str,
    processo: str = "",
    ctx: Context | None = None,
) -> str:
    """Busca um documento pelo número SEI (ex: SEI 2843449, SEI nº 2843449).

    O número SEI é o protocoloFormatado que o usuário vê no sistema.
    A API do SEI não busca documentos diretamente por esse número,
    então esta tool usa a estratégia:

    1. Se processo informado: busca direto nesse processo (rápido).
       Aceita protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento.
    2. Se não: pesquisa o número via busca textual (Solr) para encontrar
       o processo, depois lista os documentos para localizar o id interno

    Retorna o documento com seu id interno (necessário para sei_ler_documento),
    tipo, metadados e o processo onde está.
    """
    try:
        client = _get_client(ctx)
        numero_sei = numero_sei.strip()

        def _match(proto: str) -> bool:
            return proto == numero_sei or proto.lstrip("0") == numero_sei.lstrip("0")

        # Estratégia 1: processo conhecido → busca direto
        if processo:
            id_procedimento = await _resolver_processo(client, processo)
            docs = await client.listar_documentos(id_procedimento, limit=200)
            for d in docs:
                proto = d.get("atributos", {}).get("protocoloFormatado", "")
                if _match(proto):
                    return _json(
                        {
                            "encontrado": True,
                            "id_procedimento": id_procedimento,
                            "documento": d,
                        }
                    )
            return _json(
                {
                    "encontrado": False,
                    "mensagem": f"SEI {numero_sei} não encontrado no processo {id_procedimento}",
                }
            )

        # Estratégia 2: pesquisa textual (Solr) para achar o processo
        result = await client.pesquisar_processos(palavras_chave=numero_sei, limit=20)
        processos_candidatos = result.get("processos", [])

        for p in processos_candidatos:
            id_proc = str(p.get("idProcedimento", ""))
            if not id_proc:
                continue
            try:
                docs = await client.listar_documentos(id_proc, limit=200)
                for d in docs:
                    proto = d.get("atributos", {}).get("protocoloFormatado", "")
                    if _match(proto):
                        return _json(
                            {
                                "encontrado": True,
                                "processo": p.get("protocoloFormatadoProcedimento", ""),
                                "id_procedimento": id_proc,
                                "documento": d,
                            }
                        )
            except Exception:  # noqa: BLE001, S112
                continue

        return _json(
            {
                "encontrado": False,
                "processos_pesquisados": len(processos_candidatos),
                "mensagem": f"SEI {numero_sei} não encontrado via pesquisa textual",
                "dica": "A pesquisa Solr pode não indexar esse documento. "
                "Informe o número do processo (id_procedimento) para busca direta, "
                "ou use sei_arvore_processo com o protocolo do processo.",
            }
        )
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


async def _resolver_documento(client: SEIClient, referencia: str) -> tuple[str, str]:
    """Resolve uma referência de documento para (id_interno, tipo_documento).

    Aceita:
    - id interno numérico (ex: "3121831") — usa direto
    - número SEI / protocoloFormatado (ex: "2843449") — pesquisa via Solr

    Estratégia otimizada:
    1. Pesquisa Solr primeiro (encontra pelo protocoloFormatado na maioria dos casos)
    2. Se Solr não encontrar, tenta como id direto (interno → externo)

    Retorna (id_documento, tipo_documento) ou levanta exceção.
    """
    referencia = referencia.strip()

    # Estratégia 1: Pesquisa Solr (mais confiável, evita confusão id/proto)
    try:
        result = await client.pesquisar_processos(palavras_chave=referencia, limit=20)
        processos = result.get("processos", [])

        for p in processos:
            id_proc = str(p.get("idProcedimento", ""))
            if not id_proc:
                continue
            try:
                docs = await client.listar_documentos(id_proc, limit=200)
                for d in docs:
                    proto = d.get("atributos", {}).get("protocoloFormatado", "")
                    if proto == referencia or proto.lstrip("0") == referencia.lstrip("0"):
                        doc_id = str(d["id"])
                        tipo = d.get("atributos", {}).get("tipoDocumento", "I")
                        return doc_id, tipo
            except Exception:  # noqa: BLE001, S112
                continue
    except Exception:  # noqa: BLE001, S110
        pass

    # Estratégia 2: Tentar como id direto (para quando o usuário informa o id interno)
    # Só tenta se o Solr não encontrou nada — para evitar confusão
    # entre protocoloFormatado e id (são números diferentes no SEI)
    try:
        raw = await client.visualizar_documento_interno(referencia)
        # Validar que realmente retornou conteúdo (não erro mascarado)
        if raw and len(raw) > 10:  # noqa: PLR2004
            return referencia, "I"
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        # "não autorizado" pode significar que o id existe mas sem permissão
        # OU que o protocoloFormatado coincidiu com outro id — não confiável
        if "não autorizado" not in msg.lower() and "nao autorizado" not in msg.lower():
            pass  # Erro diferente, tentar externo

    # Não tentar como externo automaticamente — risco alto de confusão id/proto
    # O fallback para externo só deve ser usado com id_procedimento conhecido

    raise Exception(  # noqa: TRY002, TRY003
        f"Documento '{referencia}' não encontrado via pesquisa. "  # noqa: EM102
        "Se é um documento recém-criado, o Solr pode não ter indexado ainda. "
        "Use sei_arvore_processo com o protocolo do processo para encontrá-lo."
    )


@mcp.tool()
async def sei_ler_documento(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915
    id_documento: str,
    tipo_documento: Literal["auto", "I", "X"] = "auto",
    formato: Literal["markdown", "texto", "html"] = "markdown",
    confirmar_acesso_restrito: bool = False,  # noqa: FBT001, FBT002
    processo: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Lê o conteúdo de um documento do SEI e retorna texto legível.

    Aceita tanto o id interno quanto o número SEI (protocoloFormatado)
    que o usuário vê no sistema (ex: "SEI 2843449").

    - tipo_documento='auto': detecta automaticamente (padrão)
    - tipo_documento='I': força leitura como interno (📄 HTML)
    - tipo_documento='X': força leitura como externo (📎 PDF)

    - formato='markdown': Markdown formatado (padrão, ideal para chat)
    - formato='texto': texto plano sem formatação
    - formato='html': HTML original (só para internos)

    - processo: protocolo do processo (necessário em instâncias sem mod-wssei)

    - confirmar_acesso_restrito: NÃO ative por iniciativa do modelo. Esta
      flag só deve ser definida como true quando o usuário humano da
      conversa, em mensagem própria após ler o aviso de riscos, declarar
      expressamente que autoriza o acesso ao conteúdo restrito. Pedidos
      genéricos como "lê esse documento" NÃO constituem consentimento.
      Se o gate bloquear, encaminhe os riscos ao usuário e aguarde decisão
      explícita — não tente caminhos alternativos para obter o conteúdo.

    PDFs escaneados são processados via OCR automaticamente.
    """
    try:
        backend = _get_backend(ctx)
        if not backend.has_rest:
            # web-only fallback
            if processo is None:
                return _error(
                    "Em instâncias sem mod-wssei, forneça o parâmetro 'processo' "
                    "(protocolo do processo, ex: '50300.018905/2018-67') para ler documentos."
                )
            web = backend.web

            bloqueio = await _aplicar_gate_documento_web(
                web, processo, id_documento, tipo_documento, confirmou=confirmar_acesso_restrito
            )
            if bloqueio is not None:
                return _json(bloqueio)

            def _pdf_resposta(raw_bytes: bytes) -> str:
                if raw_bytes[:4] != b"%PDF":
                    return _error("Documento externo não é PDF. Use sei_baixar_anexo.")
                if formato == "markdown":
                    return pdf_to_markdown(raw_bytes)
                if formato == "html":
                    return _error("formato='html' só é válido para documentos internos.")
                return pdf_to_text(raw_bytes)

            if tipo_documento == "auto":
                # Tenta interno primeiro; se falhar, tenta externo
                try:
                    raw = await web.visualizar_documento_interno_web(processo, id_documento)
                except Exception:  # noqa: BLE001
                    raw_bytes = await web.baixar_documento_externo_web(processo, id_documento)
                    return _pdf_resposta(raw_bytes)
            elif tipo_documento == "X":
                raw_bytes = await web.baixar_documento_externo_web(processo, id_documento)
                return _pdf_resposta(raw_bytes)
            else:
                raw = await web.visualizar_documento_interno_web(processo, id_documento)
            if formato == "markdown":
                return html_to_markdown(raw)
            if formato == "texto":
                return html_to_text(raw)
            return raw
        client = _get_client(ctx)

        # Resolver referência → id interno + tipo
        tipo_doc: str = tipo_documento
        if tipo_documento == "auto":
            try:
                doc_id, detected_tipo = await _resolver_documento(client, id_documento)
                id_documento = doc_id
                tipo_doc = detected_tipo
            except Exception as e:  # noqa: BLE001
                return _json(
                    {
                        "error": str(e),
                        "dica": "Use sei_arvore_processo para ver os documentos "
                        "do processo e seus IDs.",
                    }
                )

        acao, payload, erro = await _aplicar_gate_documento(
            ctx,
            client,
            str(id_documento),
            tipo_doc,
            confirmou=confirmar_acesso_restrito,
        )
        if acao == "erro":
            return _error(erro)
        if acao in ("bloquear", "recusou"):
            return _json(payload)
        disclaimer = payload  # liberar (None se público, dict se restrito autorizado)

        if tipo_doc == "X":
            content = await client.baixar_anexo(id_documento)
            if len(content) > MAX_BINARY_SIZE:
                return _error(
                    f"Documento muito grande ({len(content)} bytes). "
                    "Use sei_baixar_anexo para obter o base64."
                )
            if content[:4] != b"%PDF":
                return _error(
                    "Documento externo não é PDF. Use sei_baixar_anexo "
                    "para obter o arquivo em base64."
                )
            if formato == "markdown":
                resultado = pdf_to_markdown(content)
                if disclaimer:
                    resultado = access_control.prefixar_markdown(disclaimer, resultado)
                return resultado
            resultado = pdf_to_text(content)
            if disclaimer:
                resultado = access_control.prefixar_texto(disclaimer, resultado)
            return resultado

        # Documento interno (I)
        raw = await client.visualizar_documento_interno(id_documento)
        if formato == "markdown":
            resultado = html_to_markdown(raw)
            if disclaimer:
                resultado = access_control.prefixar_markdown(disclaimer, resultado)
            return resultado
        if formato == "texto":
            resultado = html_to_text(raw)
            if disclaimer:
                resultado = access_control.prefixar_texto(disclaimer, resultado)
            return resultado
        if disclaimer:
            return access_control.envelopar_html(disclaimer, raw)
        return raw  # noqa: TRY300
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "não autorizado" in msg.lower() or "nao autorizado" in msg.lower():
            return _json(
                {
                    "error": msg,
                    "dica": "Acesso negado. Troque para a unidade geradora com sei_trocar_unidade.",
                }
            )
        return _error(msg)


@mcp.tool()
async def sei_baixar_anexo(  # noqa: C901, PLR0911
    id_documento: str,
    confirmar_acesso_restrito: bool = False,  # noqa: FBT001, FBT002
    processo: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Baixa um documento externo (anexo) do SEI em base64.

    Aceita tanto o id interno (ex: "3149544") quanto o número SEI /
    protocoloFormatado (ex: "2867926") — auto-resolve via pesquisa Solr.

    - processo: protocolo do processo (necessário em instâncias sem mod-wssei)

    Use para documentos com tipoDocumento='X' (📎).
    Para PDFs com texto, prefira sei_ler_documento(tipo_documento='X')
    que já extrai o texto legível.

    Retorna base64 + tamanho. Limite: 10 MB.

    confirmar_acesso_restrito: NÃO ative por iniciativa do modelo. Esta
    flag só deve ser definida como true quando o usuário humano da conversa,
    em mensagem própria após ler o aviso de riscos, declarar expressamente
    que autoriza o acesso. Se o gate bloquear, encaminhe os riscos ao
    usuário e aguarde decisão explícita — não tente caminhos alternativos.
    """
    try:
        backend = _get_backend(ctx)
        if not backend.has_rest:
            if processo is None:
                return _error(
                    "Em instâncias sem mod-wssei, forneça o parâmetro 'processo' "
                    "para baixar anexos."
                )
            bloqueio = await _aplicar_gate_documento_web(
                backend.web, processo, id_documento, "X", confirmou=confirmar_acesso_restrito
            )
            if bloqueio is not None:
                return _json(bloqueio)
            content = await backend.web.baixar_documento_externo_web(processo, id_documento)
            if len(content) > MAX_BINARY_SIZE:
                return _error(
                    f"Documento muito grande ({len(content)} bytes, limite {MAX_BINARY_SIZE}). "
                    "Baixe manualmente pelo SEI."
                )
            return _json({"base64": base64.b64encode(content).decode(), "size_bytes": len(content)})

        client = _get_client(ctx)

        # Auto-resolver número SEI → id interno (igual a sei_ler_documento)
        try:
            doc_id, _ = await _resolver_documento(client, id_documento)
            id_documento = doc_id
        except Exception as e:  # noqa: BLE001
            return _json(
                {
                    "error": str(e),
                    "dica": "Use sei_arvore_processo ou sei_buscar_documento para "
                    "encontrar o id correto do documento.",
                }
            )

        acao, payload, erro = await _aplicar_gate_documento(
            ctx,
            client,
            str(id_documento),
            "X",
            confirmou=confirmar_acesso_restrito,
        )
        if acao == "erro":
            return _error(erro)
        if acao in ("bloquear", "recusou"):
            return _json(payload)
        disclaimer = payload

        content = await client.baixar_anexo(id_documento)
        if len(content) > MAX_BINARY_SIZE:
            return _error(
                f"Documento muito grande ({len(content)} bytes, limite {MAX_BINARY_SIZE}). "
                "Baixe manualmente pelo SEI."
            )
        resposta: dict = {
            "base64": base64.b64encode(content).decode(),
            "size_bytes": len(content),
        }
        if disclaimer:
            resposta["aviso_acesso"] = disclaimer
        return _json(resposta)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de escrita
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_criar_documento(  # noqa: PLR0913
    processo: str,
    id_serie: str = "",
    descricao: str = "",
    nivel_acesso: str = "0",
    hipotese_legal: str = "",
    id_unidade: str = "",
    ctx: Context | None = None,
) -> str:
    """Cria um novo documento interno (nativo) em um processo SEI.

    Parâmetros:
    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento
    - id_serie: ID do tipo de documento (use sei_pesquisar_tipos_documento).
      Deixe vazio para ver os tipos disponíveis via web.
    - descricao: descrição/título do documento
    - nivel_acesso: 0=público, 1=restrito, 2=sigiloso
    - hipotese_legal: ID da hipótese legal (obrigatório se restrito/sigiloso)
    - id_unidade: ID da unidade geradora (apenas REST, opcional)

    O documento é criado vazio. Use sei_listar_secoes e sei_editar_secao
    para inserir conteúdo. Funciona via REST (mod-wssei) ou via scraper web.
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            if not id_serie:
                return _error(
                    "id_serie é obrigatório no modo REST. "
                    "Use sei_pesquisar_tipos_documento para listar os tipos disponíveis."
                )
            id_procedimento = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.criar_documento_interno(
                id_procedimento=id_procedimento,
                id_serie=id_serie,
                descricao=descricao,
                nivel_acesso=nivel_acesso,
                hipotese_legal=hipotese_legal,
                id_unidade=id_unidade,
            )
            return _json(result)
        result = await backend.web.criar_documento_interno_web(
            protocolo=processo,
            id_serie=id_serie,
            descricao=descricao,
            nivel_acesso=nivel_acesso,
            hipotese_legal=hipotese_legal,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_secoes(id_documento: str, ctx: Context | None = None) -> str:
    """Lista as seções editáveis de um documento interno SEI.

    Retorna as seções com seus IDs, conteúdo atual (HTML),
    e a versão do documento (campo ultimaVersaoDocumento),
    necessária para usar sei_editar_secao.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_secao_documento(id_documento)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_gerar_referencia(
    numero_sei: str,
    ctx: Context | None = None,
) -> str:
    """Gera o HTML de referência (hiperlink dinâmico) para um documento SEI.

    Dado um número SEI (ex: 2599818), resolve o id interno e retorna
    o snippet HTML pronto para inserir no conteúdo de um documento.

    O SEI renderiza isso como link clicável na interface web.
    Use ao citar documentos SEI no texto de Despachos, Notas Técnicas, etc.

    Exemplo: "SEI nº <resultado>" vira link clicável para o documento.
    """
    try:
        client = _get_client(ctx)
        doc_id, _ = await _resolver_documento(client, numero_sei)
        snippet = html_referencia_sei(doc_id, numero_sei)
        return _json(
            {
                "numero_sei": numero_sei,
                "id_documento": doc_id,
                "html": snippet,
                "uso": f"...SEI n&ordm; {snippet}...",
            }
        )
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_estilos(categoria: str = "", ctx: Context | None = None) -> str:  # noqa: ARG001
    """Lista os estilos CSS disponíveis para formatação de documentos no SEI.

    O SEI usa classes CSS padronizadas em todos os documentos governamentais.
    Use esta tool para descobrir a classe correta para cada tipo de parágrafo.

    Categorias: "texto", "titulo", "lista", "tabela", "destaque", "todos"
    Sem parâmetro: retorna os atalhos rápidos (intenção → classe).

    CONVENÇÃO para documentos (Despachos, Notas Técnicas, etc.):
    - Corpo/mérito do texto: usar Paragrafo_Numerado_Nivel1 (autonumera 1. 2. 3.)
    - Endereçamento (À SFC...): usar Texto_Alinhado_Esquerda
    - Assunto: usar Texto_Justificado com <strong> para o título
    - Fecho (Atenciosamente): usar Texto_Justificado_Recuo_Primeira_Linha
    - Nome do signatário: usar Texto_Centralizado_Maiusculas
    - Cargo: usar Texto_Centralizado
    """
    try:
        if not categoria or categoria == "atalhos":
            return _json(
                {
                    "atalhos": STYLE_SHORTCUTS,
                    "dica": "Use sei_estilos('todos') para ver todos os estilos com exemplos.",
                }
            )

        if categoria == "todos":
            return _json(SEI_STYLES)

        filtros = {
            "texto": ["Texto_"],
            "titulo": [
                "Texto_Centralizado_Maiusculas",
                "Texto_Fundo_Cinza",
                "Texto_Espaco_Duplo",
            ],
            "lista": ["Paragrafo_Numerado", "Item_Nivel", "Item_Alinea", "Item_Inciso"],
            "tabela": ["Tabela_"],
            "destaque": ["Citacao", "Tachado", "Texto_Fundo_Cinza", "Texto_Mono"],
        }

        prefixos = filtros.get(categoria, [])
        if not prefixos:
            return _json(
                {
                    "error": f"Categoria '{categoria}' não encontrada",
                    "categorias": list(filtros.keys()) + ["todos", "atalhos"],  # noqa: RUF005
                }
            )

        resultado = {}
        for nome, info in SEI_STYLES.items():
            if any(nome.startswith(p) for p in prefixos):
                resultado[nome] = info  # noqa: PERF403

        return _json(resultado)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_editar_secao(
    id_documento: str,
    secoes: list[dict],
    versao: str = "",
    ctx: Context | None = None,
) -> str:
    """Altera o conteúdo de seções editáveis de um documento interno SEI.

    Parâmetros:
    - id_documento: ID do documento
    - secoes: lista de seções a alterar, cada uma com:
        - idSecaoModelo: ID do modelo da seção (obtido via sei_listar_secoes)
        - conteudo: novo conteúdo HTML da seção
      (não é necessário incluir seções somenteLeitura — são preenchidas
       automaticamente com o conteúdo original)
    - versao: versão do documento (se omitida, obtida automaticamente)

    O conteúdo deve ser HTML com as classes CSS do SEI (ex: Texto_Justificado).
    Caracteres fora do ISO-8859-1 são convertidos automaticamente.

    IMPORTANTE: O SEI exige que TODAS as seções sejam enviadas. Esta tool
    faz isso automaticamente — basta informar as seções que deseja alterar.
    """
    try:
        client = _get_client(ctx)
        import html as html_module  # noqa: PLC0415

        # Buscar todas as seções atuais do documento
        secoes_data = await client.listar_secao_documento(id_documento)
        secoes_atuais = secoes_data.get("secoes", [])
        if not versao:
            versao = str(secoes_data.get("ultimaVersaoDocumento", "1"))

        # Indexar seções novas por idSecaoModelo
        alteracoes = {}
        for s in secoes:
            modelo = s.get("idSecaoModelo", "")
            if modelo:
                alteracoes[modelo] = s.get("conteudo", "")

        # Montar payload completo com TODAS as seções
        secoes_enviar = []
        for s in secoes_atuais:
            if not isinstance(s, dict):
                continue
            sid = s.get("id") or s.get("IdSecaoDocumento")
            modelo = s.get("idSecaoModelo") or s.get("IdSecaoModelo")
            if not sid or not modelo:
                continue

            if str(modelo) in alteracoes:
                # Seção alterada pelo usuário
                conteudo = alteracoes[str(modelo)]
            else:
                # Seção original — fazer unescape do HTML-escaped
                conteudo = html_module.unescape(s.get("conteudo", "") or "")

            secoes_enviar.append(
                {
                    "id": str(sid),
                    "idSecaoModelo": str(modelo),
                    "conteudo": sanitize_iso8859(conteudo),
                }
            )

        result = await client.alterar_secao_documento(
            id_documento=id_documento,
            secoes=secoes_enviar,
            versao=versao,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de processos — listar, pesquisar, criar, tramitar
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_listar_processos(
    pagina: int = 0,
    apenas_meus: str = "",
    tipo: str = "",
    filtro: str = "",
    ctx: Context | None = None,
) -> str:
    """Lista processos da caixa da unidade atual no SEI (Controle de Processos).

    Implementação via scraper do frontend web (~20× mais rápida que a REST API).
    Retorna a página inteira de uma vez (a paginação é controlada pelo SEI;
    para a maioria das unidades todos os processos cabem em poucas páginas).

    Parâmetros:
    - pagina: número da página (0=primeira, 1=segunda, etc.)
    - apenas_meus: "S" para apenas processos atribuídos ao usuário logado
      (filtro server-side via hdnMeusProcessos=M)
    - tipo: substring (case-insensitive) para filtrar pelo nome do tipo processual
      (filtro client-side, sobre a coluna "Tipo")
    - filtro: substring (case-insensitive) aplicada a qualquer campo do processo
      (protocolo, tipo, especificação, interessados — filtro client-side)

    Campos retornados por processo (visualização Detalhada):
    - id_procedimento: id interno do SEI
    - protocolo: número formatado (ex: 50300.007186/2026-69)
    - Tipo: tipo processual
    - atribuicao: usuário ao qual está atribuído
    - Especificação, Interessados, Marcadores, etc. — conforme as colunas
      configuradas no painel da unidade

    NOTAS:
    - Processos sobrestados e concluídos não aparecem nesta listagem.
    - Para agrupamento estatístico (sei_resumo_processos) usa-se a REST API
      diretamente (que tem flags estruturadas como tramitação, sobrestamento,
      acesso, etc.).
    - Login web é executado uma vez por sessão (~3 s); listagens subsequentes
      custam ~600 ms cada, contra ~14 s da REST API.
    """
    try:
        web = _get_web_client(ctx)
        result = await web.listar_processos(
            detalhada=True,
            pagina=pagina,
            apenas_meus=(apenas_meus.upper() == "S"),
            tipo=tipo,
            filtro=filtro,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


_CAMPOS_AGRUPAMENTO = {
    "tipo": {
        "desc": "Tipo processual",
        "extract": lambda a, s: a.get("tipoProcesso", "Sem tipo"),  # noqa: ARG005
    },
    "atribuido": {
        "desc": "Usuário atribuído",
        "extract": lambda a, s: a.get("usuarioAtribuido") or "Sem atribuição",  # noqa: ARG005
    },
    "acesso": {
        "desc": "Nível de acesso",
        "extract": lambda a, s: {"0": "Público", "1": "Restrito", "2": "Sigiloso"}.get(  # noqa: ARG005
            s.get("nivelAcessoGlobal", "0"), "Desconhecido"
        ),
    },
    "tramitacao": {
        "desc": "Em tramitação",
        "extract": lambda a, s: (  # noqa: ARG005
            "Em tramitação" if s.get("processoEmTramitacao") == "S" else "Fora de tramitação"
        ),
    },
    "sobrestado": {
        "desc": "Sobrestamento",
        "extract": lambda a, s: "Sobrestado" if s.get("processoSobrestado") == "S" else "Ativo",  # noqa: ARG005
    },
    "bloqueado": {
        "desc": "Bloqueio",
        "extract": lambda a, s: (  # noqa: ARG005
            "Bloqueado" if s.get("processoBloqueado") == "S" else "Desbloqueado"
        ),
    },
    "novo": {
        "desc": "Documento novo",
        "extract": lambda a, s: (  # noqa: ARG005
            "Com documentos novos" if s.get("documentoNovo") == "S" else "Sem documentos novos"
        ),
    },
    "anotacao": {
        "desc": "Anotação",
        "extract": lambda a, s: (  # noqa: ARG005
            "Anotação prioritária"
            if s.get("anotacaoPrioridade") == "S"
            else "Com anotação"
            if s.get("anotacao") == "S"
            else "Sem anotação"
        ),
    },
    "retorno": {
        "desc": "Retorno programado",
        "extract": lambda a, s: (  # noqa: ARG005
            f"Atrasado ({s.get('retornoData', '')})"
            if s.get("retornoAtrasado") == "S"
            else f"Programado ({s.get('retornoData', '')})"
            if s.get("retornoProgramado") == "S"
            else "Sem retorno"
        ),
    },
    "lido_usuario": {
        "desc": "Acessado pelo usuário",
        "extract": lambda a, s: "Lido" if s.get("processoAcessadoUsuario") == "S" else "Não lido",  # noqa: ARG005
    },
    "lido_unidade": {
        "desc": "Acessado pela unidade",
        "extract": lambda a, s: "Lido" if s.get("processoAcessadoUnidade") == "S" else "Não lido",  # noqa: ARG005
    },
    "origem": {
        "desc": "Gerado/Recebido",
        "extract": lambda a, s: (  # noqa: ARG005
            "Gerado na unidade" if s.get("processoGeradoRecebido") == "G" else "Recebido"
        ),
    },
    "anexado": {
        "desc": "Anexado",
        "extract": lambda a, s: "Anexado" if s.get("processoAnexado") == "S" else "Independente",  # noqa: ARG005
    },
    "unidades": {
        "desc": "Unidades de abertura",
        "extract": lambda a, s: (  # noqa: ARG005
            ", ".join(u.get("sigla", "") for u in a.get("dadosAbertura", {}).get("lista", []))
            or "N/A"
        ),
    },
    "marcador": {
        "desc": "Marcador",
        "extract": lambda a, s: (  # noqa: ARG005
            ", ".join(m.get("nome", "") for m in a.get("marcador", [])) or "Sem marcador"
        ),
    },
    "ciencia": {
        "desc": "Ciência",
        "extract": lambda a, s: "Com ciência" if s.get("ciencia") == "S" else "Sem ciência",  # noqa: ARG005
    },
}


@mcp.tool()
async def sei_resumo_processos(  # noqa: C901, PLR0912
    agrupar_por: str = "tipo",
    agrupar_por_2: str = "",
    apenas_meus: str = "",
    filtro: str = "",
    ctx: Context | None = None,
) -> str:
    """Gera um resumo agrupado dos processos da caixa da unidade atual.

    Busca TODOS os processos e agrupa por um ou dois campos.

    Campos disponíveis para agrupar_por e agrupar_por_2:
    - tipo: Tipo processual
    - atribuido: Usuário atribuído
    - acesso: Nível de acesso (Público/Restrito/Sigiloso)
    - tramitacao: Em tramitação ou não
    - sobrestado: Sobrestado ou ativo
    - bloqueado: Bloqueado ou não
    - novo: Com/sem documentos novos
    - anotacao: Com/sem anotação (inclui prioridade)
    - retorno: Retorno programado (inclui data e atraso)
    - lido_usuario: Acessado pelo usuário
    - lido_unidade: Acessado pela unidade
    - origem: Gerado na unidade ou recebido
    - anexado: Anexado a outro processo
    - unidades: Unidades onde está aberto
    - marcador: Marcador/etiqueta
    - ciencia: Com/sem ciência

    Exemplos:
    - agrupar_por="tipo" → quantidade por tipo processual
    - agrupar_por="atribuido" → distribuição por pessoa
    - agrupar_por="tipo", agrupar_por_2="atribuido" → cruzamento tipo × pessoa
    - agrupar_por="retorno" → processos com prazo vencido
    """
    try:
        campo1 = _CAMPOS_AGRUPAMENTO.get(agrupar_por)
        if not campo1:
            campos = ", ".join(sorted(_CAMPOS_AGRUPAMENTO.keys()))
            return _error(f"Campo '{agrupar_por}' inválido. Disponíveis: {campos}")

        campo2 = None
        if agrupar_por_2:
            campo2 = _CAMPOS_AGRUPAMENTO.get(agrupar_por_2)
            if not campo2:
                campos = ", ".join(sorted(_CAMPOS_AGRUPAMENTO.keys()))
                return _error(f"Campo '{agrupar_por_2}' inválido. Disponíveis: {campos}")

        client = _get_client(ctx)

        # Busca todos os processos
        todos = []
        pg = 0
        while True:
            result = await client.listar_processos(
                limit=200,
                start=pg,
                apenas_meus=apenas_meus,
                filtro=filtro,
            )
            todos.extend(result["processos"])
            if not result.get("tem_proxima"):
                break
            pg += 1

        # Agrupar
        grupos: dict = {}
        for p in todos:
            a = p.get("atributos", {})
            s = a.get("status", {})
            chave1 = cast(Callable[..., str], campo1["extract"])(a, s)  # noqa: TC006

            if campo2:
                chave2 = cast(Callable[..., str], campo2["extract"])(a, s)  # noqa: TC006
                chave = f"{chave1} | {chave2}"
            else:
                chave = chave1

            if chave not in grupos:
                grupos[chave] = {"quantidade": 0, "processos": []}
            grupos[chave]["quantidade"] += 1
            grupos[chave]["processos"].append(a.get("numero", ""))

        # Ordenar por quantidade decrescente
        resumo = []
        for chave in sorted(grupos.keys(), key=lambda k: -grupos[k]["quantidade"]):
            g = grupos[chave]
            item = {"grupo": chave, "quantidade": g["quantidade"]}
            # Incluir lista de processos se grupo pequeno (≤ 20)
            if g["quantidade"] <= 20:  # noqa: PLR2004
                item["processos"] = g["processos"]
            resumo.append(item)

        header = cast(str, campo1["desc"])  # noqa: TC006
        if campo2:
            header += f" × {cast(str, campo2['desc'])}"  # noqa: TC006

        return _json(
            {
                "agrupamento": header,
                "total_processos": len(todos),
                "total_grupos": len(resumo),
                "grupos": resumo,
            }
        )
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_processos(  # noqa: PLR0913
    palavras_chave: str = "",
    descricao: str = "",
    busca_rapida: str = "",
    data_inicio: str = "",
    data_fim: str = "",
    sta_tipo_data: str = "",
    id_unidade_geradora: str = "",
    id_assunto: str = "",
    grupo: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa processos no SEI por texto, descrição, datas, unidade ou assunto.

    Use palavras_chave para busca geral ou busca_rapida para busca simplificada.
    Datas no formato DD/MM/AAAA.

    Filtros adicionais (REST only):
    - sta_tipo_data: tipo de período — "30" (últimos 30 dias), "60" (últimos 60 dias)
      ou "0" (personalizado, requer data_inicio/data_fim)
    - id_unidade_geradora: id da unidade que gerou o processo (use sei_listar_unidades)
    - id_assunto: id do assunto (use sei_pesquisar_assuntos para obter o id)
    - grupo: id do grupo de acompanhamento (use sei_listar_grupos_acompanhamento)

    Paginação: pagina=0 é a primeira página, pagina=1 a segunda, etc.

    Busca via web (instâncias sem mod-wssei, ex: SEI-RO):
    - Quando REST não está disponível, a busca usa o formulário de pesquisa
      avançada do SEI via scraping. O retorno inclui "fonte": "web".
    - Use aspas para frase exata: palavras_chave='"NOME COMPLETO" aposentadoria'
      é muito mais preciso do que palavras soltas.
    - A busca web varre todo o SEI (não filtrada por unidade do usuário).
    - Os filtros estruturais acima são ignorados no caminho web; quando isso
      ocorre, o campo "aviso" no retorno lista os filtros descartados.
    - Máximo de 10 resultados por página no caminho web.
    """
    _rest_unavailable = False
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_processos(
            palavras_chave=palavras_chave,
            descricao=descricao,
            busca_rapida=busca_rapida,
            data_inicio=data_inicio,
            data_fim=data_fim,
            sta_tipo_data=sta_tipo_data,
            id_unidade_geradora=id_unidade_geradora,
            id_assunto=id_assunto,
            grupo=grupo,
            limit=limit,
            start=pagina,
        )
        return _json(result)
    except (ValueError, httpx.UnsupportedProtocol):
        _rest_unavailable = True  # REST não configurado (sem SEI_URL) ou URL inválida
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 501):
            _rest_unavailable = True  # mod-wssei ausente ou endpoint não encontrado
        else:
            return _error(str(exc))
    except Exception as e:  # noqa: BLE001
        return _error(str(e))

    # Fallback via web scraper (instâncias sem mod-wssei)
    q_web = " ".join(filter(None, [palavras_chave, busca_rapida]))
    dropped = [
        n
        for n, v in [
            ("sta_tipo_data", sta_tipo_data),
            ("id_unidade_geradora", id_unidade_geradora),
            ("id_assunto", id_assunto),
            ("grupo", grupo),
        ]
        if v
    ]
    try:
        web = _get_web_client(ctx)
        items = await web.pesquisar_processos_web(
            q=q_web,
            descricao=descricao,
            data_inicio=data_inicio,
            data_fim=data_fim,
            pagina=pagina,
        )
        page_items = items[:limit]
        paged: dict = {
            "processos": page_items,
            "pagina_atual": pagina,
            "itens_pagina": len(page_items),
            "total_itens": len(page_items),
            "tem_proxima": len(items) >= 10,  # noqa: PLR2004
            "fonte": "web",
        }
        avisos: list[str] = []
        if dropped:
            avisos.append(
                f"filtros ignorados (não suportados na pesquisa web): {', '.join(dropped)}"
            )
        if limit < 10 and len(items) > limit:  # noqa: PLR2004
            avisos.append(f"resultados truncados para limit={limit} (página web retorna até 10)")
        if avisos:
            paged["aviso"] = "; ".join(avisos).capitalize()
        return _json(paged)
    except Exception as e2:  # noqa: BLE001
        return _error(f"Web: {e2}")


@mcp.tool()
async def sei_pesquisar_hipoteses_legais(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa hipóteses legais disponíveis no SEI.

    Necessário ao criar processos ou documentos com nível de acesso
    restrito ou sigiloso. Use o 'id' retornado no parâmetro
    hipotese_legal de sei_criar_processo.

    Exemplos: "pessoal", "controle interno", "sigilo fiscal"
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_hipoteses_legais(
            filtro=filtro,
            limit=limit,
            start=pagina,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_tipos_processo(
    filtro: str = "",
    favoritos: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa tipos de processo disponíveis no SEI.

    Parâmetros:
    - filtro: texto para filtrar por nome (ex: "Plano Anual", "Fiscalização")
    - favoritos: "S" para apenas favoritos
    - limit/pagina: paginação

    Use o 'id' retornado como tipo_processo em sei_criar_processo.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_tipos_processo(
            filtro=filtro,
            favoritos=favoritos,
            limit=limit,
            start=pagina,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_alterar_processo(  # noqa: PLR0913
    processo: str,
    especificacao: str = "",
    nivel_acesso: str = "",
    hipotese_legal: str = "",
    observacao: str = "",
    ctx: Context | None = None,
) -> str:
    """Altera metadados de um processo no SEI.

    Parâmetros:
    - processo: protocolo formatado (ex: 50300.009752/2026-77) ou IdProcedimento
    - especificacao: nova descrição/especificação do processo
    - nivel_acesso: 0=público, 1=restrito, 2=sigiloso
    - hipotese_legal: ID da hipótese legal (obrigatório se restrito/sigiloso).
      Use sei_pesquisar_hipoteses_legais para descobrir o ID.
    - observacao: observações adicionais

    Informe apenas os campos que deseja alterar.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.alterar_processo(
            id_procedimento=id_proc,
            especificacao=especificacao,
            nivel_acesso=nivel_acesso,
            hipotese_legal=hipotese_legal,
            observacao=observacao,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_criar_processo(  # noqa: PLR0913
    tipo_processo: str,
    especificacao: str = "",
    assuntos: str = "",
    interessados: str = "",
    observacoes: str = "",
    nivel_acesso: str = "0",
    hipotese_legal: str = "",
    ctx: Context | None = None,
) -> str:
    """Cria um novo processo no SEI.

    Parâmetros:
    - tipo_processo: ID do tipo de processo (use sei_pesquisar_tipos_processo)
    - especificacao: descrição do processo (recomendado para organizar a caixa)
    - assuntos: IDs dos assuntos separados por vírgula
    - interessados: IDs dos interessados separados por vírgula
    - observacoes: observações adicionais (apenas REST)
    - nivel_acesso: 0=público (padrão), 1=restrito, 2=sigiloso
    - hipotese_legal: ID da hipótese legal (obrigatório se restrito/sigiloso).
      Use sei_pesquisar_hipoteses_legais para descobrir o ID.

    Retorna o IdProcedimento e ProtocoloFormatado do processo criado.
    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            result = await backend.rest.criar_processo(
                tipo_processo=tipo_processo,
                especificacao=especificacao,
                assuntos=assuntos,
                interessados=interessados,
                observacoes=observacoes,
                nivel_acesso=nivel_acesso,
                hipotese_legal=hipotese_legal,
            )
            return _json(result)
        assuntos_ids = [a.strip() for a in assuntos.split(",") if a.strip()]
        interessados_ids = [i.strip() for i in interessados.split(",") if i.strip()]
        result = await backend.web.criar_processo_web(
            tipo_processo=tipo_processo,
            especificacao=especificacao,
            assuntos_ids=assuntos_ids,
            interessados_ids=interessados_ids,
            nivel_acesso=nivel_acesso,
            hipotese_legal=hipotese_legal,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_enviar_processo(  # noqa: C901, PLR0913
    numero_processo: str,
    unidades_destino: str,
    manter_aberto: str = "N",
    remover_anotacao: str = "N",
    enviar_email: str = "N",
    data_retorno: str = "",
    dias_retorno: str = "",
    ctx: Context | None = None,
) -> str:
    """Envia (tramita) um processo para outra(s) unidade(s) no SEI.

    Parâmetros:
    - numero_processo: protocolo formatado (ex: 50300.000123/2025-00)
    - unidades_destino: sigla da unidade (ex: "SFC", "ECP-SFC") OU ID numérico.
      Para múltiplas unidades, separe por vírgula.
      Se informar sigla, resolve o ID automaticamente via REST ou AJAX web.
    - manter_aberto: "N" fechar na unidade atual (padrão), "S" manter aberto
    - remover_anotacao: "S" remover anotações, "N" manter (padrão)
    - enviar_email: "S" notificar por email (só se o usuário pedir)
    - data_retorno: data de retorno programado DD/MM/AAAA (só se o usuário pedir)
    - dias_retorno: prazo em dias para retorno (alternativa à data, só se pedir)

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)

        # Resolver unidades destino: aceita sigla ou ID
        destinos = [d.strip() for d in unidades_destino.split(",") if d.strip()]
        if not destinos:
            return _json({"error": "unidades_destino não pode ser vazio."})
        ids_resolvidos: list[str] = []

        for destino in destinos:
            if destino.isdigit():
                ids_resolvidos.append(destino)
            elif backend.has_rest:
                result = await backend.rest.pesquisar_unidades(filtro=destino, limit=10)
                unidades = result.get("unidades", [])
                encontrou = False
                for u in unidades:
                    if u.get("sigla", "").upper() == destino.upper():
                        ids_resolvidos.append(str(u.get("id", "")))
                        encontrou = True
                        break
                if not encontrou:
                    return _json(
                        {
                            "error": f"Unidade '{destino}' não encontrada.",
                            "candidatos": [u.get("sigla") for u in unidades],
                            "dica": "Use sei_pesquisar_unidades para buscar.",
                        }
                    )
            else:
                # Via AJAX autocomplete
                matches = await backend.web.autocomplete_unidades(destino)
                exact = next(
                    (m for m in matches if m.get("sigla", "").upper() == destino.upper()),
                    None,  # never fall back to matches[0] — wrong unit is worse than an error
                )
                if not exact:
                    return _json(
                        {
                            "error": f"Unidade '{destino}' não encontrada via autocomplete web.",
                            "candidatos": [m.get("sigla") for m in matches],
                            "dica": "Informe o ID numérico diretamente.",
                        }
                    )
                ids_resolvidos.append(exact["id"])

        if backend.has_rest:
            result = await backend.rest.enviar_processo(
                numero_processo=numero_processo,
                unidades_destino=",".join(ids_resolvidos),
                manter_aberto=manter_aberto,
                remover_anotacao=remover_anotacao,
                enviar_email=enviar_email,
                data_retorno=data_retorno,
                dias_retorno=dias_retorno,
            )
            return _json(result)

        result = await backend.web.enviar_processo_web(
            protocolo=numero_processo,
            unidades_ids=ids_resolvidos,
            manter_aberto=manter_aberto,
            remover_anotacao=remover_anotacao,
            enviar_email=enviar_email,
            data_retorno=data_retorno,
            dias_retorno=dias_retorno,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_marcar_nao_lido(
    numero_processo: str,
    ctx: Context | None = None,
) -> str:
    """Marca um processo como não lido na unidade atual.

    O SEI não possui funcionalidade nativa para isso. Esta tool usa
    o workaround de enviar o processo para a própria unidade, o que
    faz o SEI tratar como novo recebimento (não lido).

    - numero_processo: protocolo formatado (ex: 50300.012639/2023-26)
    """
    try:
        client = _get_client(ctx)
        if not client._unidade_ativa:  # noqa: SLF001
            return _error("Unidade ativa não definida. Use sei_trocar_unidade primeiro.")
        result = await client.enviar_processo(
            numero_processo=numero_processo,
            unidades_destino=client._unidade_ativa,  # noqa: SLF001
            manter_aberto="S",
            remover_anotacao="N",
            enviar_email="N",
        )
        return _json(
            {
                "mensagem": "Processo marcado como não lido.",
                "detalhe": result.get("mensagem", ""),
            }
        )
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_concluir_processo(numero_processo: str, ctx: Context | None = None) -> str:
    """Conclui um processo na unidade atual do SEI.

    O processo é removido da caixa da unidade mas permanece acessível.
    Use sei_reabrir_processo para reverter.
    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            result = await backend.rest.concluir_processo(numero_processo)
            return _json(result)
        result = await backend.web.executar_acao_processo(numero_processo, "procedimento_concluir")
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_reabrir_processo(processo: str, ctx: Context | None = None) -> str:
    """Reabre um processo que foi concluído na unidade.

    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento

    O processo volta para a caixa da unidade atual.
    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.reabrir_processo(id_proc)
            return _json(result)
        result = await backend.web.executar_acao_processo(processo, "procedimento_reabrir")
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_atribuir_processo(  # noqa: C901, PLR0911
    numero_processo: str,
    usuario: str,
    ctx: Context | None = None,
) -> str:
    """Atribui um processo a um usuário da unidade.

    Parâmetros:
    - numero_processo: protocolo formatado (ex: 50300.000123/2025-00)
    - usuario: ID numérico do usuário OU nome/parte do nome
      (ex: "100001860" ou "Karina" ou "Karina Shimoishi")

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    Via web, o usuário é escolhido de um <select> no form — use
    sei_atribuir_processo(usuario="?") para listar os usuários disponíveis.
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            if usuario.isdigit():
                result = await backend.rest.atribuir_processo(numero_processo, usuario)
                return _json(result)
            result = await backend.rest.listar_usuarios(filtro=usuario)
            candidatos = result.get("usuarios", [])
            if not candidatos:
                return _json(
                    {
                        "error": f"Nenhum usuário encontrado com '{usuario}'",
                        "dica": "Use sei_listar_usuarios para ver os usuários disponíveis.",
                    }
                )
            erros = []
            for u in candidatos:
                id_u = u.get("id_usuario", "")
                nome = u.get("nome", "")
                sigla = u.get("sigla", "")
                try:
                    result = await backend.rest.atribuir_processo(numero_processo, id_u)
                    return _json(
                        {
                            "mensagem": result.get("mensagem", "Processo atribuído com sucesso!"),
                            "usuario": {"id": id_u, "nome": nome, "sigla": sigla},
                        }
                    )
                except Exception as e:  # noqa: BLE001
                    erros.append(f"{nome} ({sigla}): {e}")
                    continue
            return _json(
                {
                    "error": f"Nenhum dos {len(candidatos)} usuários com '{usuario}' tem permissão na unidade atual",
                    "tentativas": erros,
                    "dica": "Verifique se está na unidade correta com sei_trocar_unidade.",
                }
            )

        # Via web: parse do <select> de usuários no form atribuicao_salvar
        form_info = await backend.web.obter_form_acao(numero_processo, "atribuicao_salvar")
        opcoes_usuario = form_info.get("selects", {}).get("selAtribuicao", [])
        if not opcoes_usuario:
            return _json(
                {
                    "error": "Nenhum usuário disponível para atribuição nesta unidade.",
                    "dica": "Verifique se há usuários ativos na unidade atual.",
                }
            )
        if usuario == "?":
            return _json({"usuarios_disponiveis": opcoes_usuario})
        # Encontra o usuário pelo ID ou por correspondência de nome
        id_usuario = ""
        for opt in opcoes_usuario:
            if opt["value"] == usuario or usuario.lower() in opt["texto"].lower():
                id_usuario = opt["value"]
                break
        if not id_usuario:
            return _json(
                {
                    "error": f"Usuário '{usuario}' não encontrado no form.",
                    "usuarios_disponiveis": opcoes_usuario,
                }
            )
        result = await backend.web.executar_acao_processo(
            numero_processo, "atribuicao_salvar", {"selAtribuicao": id_usuario}
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de documentos — assinar, pesquisar tipos
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_cancelar_assinatura(
    id_documento: str,
    ctx: Context | None = None,
) -> str:
    """Tenta cancelar (derrubar) a assinatura de um documento no SEI.

    Aceita id interno ou número SEI (protocoloFormatado).

    A API do SEI não possui endpoint direto para cancelar assinatura.
    Esta tool tenta forçar uma edição mínima no documento para que o
    SEI remova a assinatura automaticamente (comportamento padrão ao editar).

    LIMITAÇÃO: só funciona se o processo não foi enviado/lido por outra
    unidade. Se falhar, o usuário deve cancelar a assinatura pela
    interface web do SEI (botão "Editar Conteúdo" no documento).
    """
    try:
        client = _get_client(ctx)
        import html as html_module  # noqa: PLC0415

        # Resolver número SEI → id interno
        doc_id = id_documento.strip()
        with suppress(Exception):
            doc_id, _ = await _resolver_documento(client, doc_id)

        # Verificar se está assinado
        secoes_data = await client.listar_secao_documento(doc_id)
        versao = str(secoes_data.get("ultimaVersaoDocumento", "1"))

        # Montar payload com todas as seções (mesmo conteúdo)
        secoes_enviar = []
        for s in secoes_data.get("secoes", []):
            if not isinstance(s, dict):
                continue
            sid = s.get("id")
            modelo = s.get("idSecaoModelo")
            conteudo = html_module.unescape(s.get("conteudo", "") or "")
            secoes_enviar.append(
                {
                    "id": str(sid),
                    "idSecaoModelo": str(modelo),
                    "conteudo": sanitize_iso8859(conteudo),
                }
            )

        # Tentar editar (derruba assinatura se permitido)
        result = await client.alterar_secao_documento(doc_id, secoes_enviar, versao)
        return _json(
            {
                "mensagem": "Assinatura cancelada com sucesso. O documento foi editado (nova versão).",
                "versao": result,
            }
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "assinado" in msg.lower():
            return _json(
                {
                    "error": "Não foi possível cancelar a assinatura via API.",
                    "motivo": msg,
                    "dica": "O processo pode ter sido enviado ou lido por outra unidade. "
                    "Cancele a assinatura pela interface web do SEI: "
                    "abra o documento → clique em 'Editar Conteúdo'.",
                }
            )
        return _error(msg)


@mcp.tool()
async def sei_assinar_documento(
    id_documento: str,
    cargo: str = "",
    orgao: str = "",
    ctx: Context | None = None,
) -> str:
    """Assina eletronicamente um documento no SEI.

    A autenticação é automática — basta informar o documento e o cargo.

    IMPORTANTE: o parâmetro `cargo` é OBRIGATÓRIO. Sem ele a assinatura falha.
    Se não souber o cargo, chame sem cargo para obter a lista de opções.
    Pergunte ao usuário qual cargo usar e chame novamente com o cargo escolhido.
    Grave o cargo escolhido para reutilizar nas próximas assinaturas.

    Parâmetros:
    - id_documento: ID interno do documento ou número SEI (protocoloFormatado).
      Se for número SEI, resolve automaticamente via pesquisa Solr.
    - cargo: cargo/função para assinatura (ex: "Agente Público").
      OBRIGATÓRIO. Se omitido, retorna a lista de cargos disponíveis.
    - orgao: código do órgão (usa o padrão se omitido)
    """
    try:
        client = _get_client(ctx)
        login = client._usuario  # noqa: SLF001
        senha = client._senha  # noqa: SLF001

        # Resolver número SEI → id interno (sempre, pois ambos são numéricos
        # e indistinguíveis pelo formato; o resolver tenta Solr primeiro
        # e só cai para id direto se Solr não achar)
        doc_id = id_documento.strip()
        try:
            doc_id, _ = await _resolver_documento(client, doc_id)
        except Exception:  # noqa: BLE001
            doc_id = id_documento.strip()  # Manter original se resolver falhar

        # Se cargo não informado, listar opções e pedir ao usuário
        if not cargo:
            try:
                resp = await client._request("GET", "/assinante/listar")  # noqa: SLF001
                data = resp.json()
                cargos = data.get("data", [])
            except Exception:  # noqa: BLE001
                cargos = []
            return _json(
                {
                    "error": "Cargo/Função não informado — é obrigatório para assinatura.",
                    "cargos_disponiveis": cargos,
                    "dica": "Pergunte ao usuário qual cargo/função usar para assinar. "
                    "Os cargos disponíveis estão listados acima. "
                    "IMPORTANTE: após o usuário escolher, salve o cargo na memória da conversa "
                    "para reutilizar em todas as próximas assinaturas sem perguntar novamente.",
                }
            )

        # Garante que a autenticação rodou e captura IdUsuario da sessão
        await client._get_headers()  # noqa: SLF001
        id_usuario = client._id_usuario or ""  # noqa: SLF001

        # Fallback: procurar via /usuario/listar caso loginData não traga o id
        if not id_usuario:
            try:
                result = await client.listar_usuarios(filtro=login, apenas_unidade=False)
                for u in result.get("usuarios", []):
                    if u.get("sigla", "").lower() == login.lower():
                        id_usuario = str(u.get("id_usuario") or "")
                        break
            except Exception:  # noqa: BLE001, S110
                pass

        result = await client.assinar_documento(
            id_documento=doc_id,
            login=login,
            senha=senha,
            cargo=cargo,
            orgao=orgao,
            id_usuario=id_usuario,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_tipos_documento(  # noqa: PLR0913
    filtro: str = "",
    favoritos: str = "",
    aplicabilidade: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa tipos de documento (séries) disponíveis no SEI.

    Parâmetros:
    - filtro: texto para filtrar por nome do tipo
    - favoritos: "S" para apenas favoritos
    - aplicabilidade: "I" para internos, "F" para externos, ou "I,F" para ambos
    - limit: quantidade por página
    - pagina: número da página (0=primeira)

    Use o 'id' retornado como id_serie em sei_criar_documento.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_tipos_documento(
            filtro=filtro,
            favoritos=favoritos,
            aplicabilidade=aplicabilidade,
            limit=limit,
            start=pagina,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de anotação
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_sobrestar_processo(
    processo: str,
    motivo: str,
    processo_vinculado: str = "",
    ctx: Context | None = None,
) -> str:
    """Sobresta um processo no SEI.

    Parâmetros:
    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento
    - motivo: motivo do sobrestamento (obrigatório)
    - processo_vinculado: protocolo de outro processo para vincular (opcional)

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            proto_vinculado = ""
            if processo_vinculado:
                proto_vinculado = await _resolver_processo(backend.rest, processo_vinculado)
            try:
                result = await backend.rest.sobrestar_processo(
                    id_procedimento=id_proc,
                    motivo=motivo,
                    protocolo_vinculado=proto_vinculado,
                )
                return _json(result)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                # Enriquece o erro com as unidades onde o processo está aberto,
                # para orientar o LLM a concluir o processo antes de sobrestar.
                if "aberto" in msg.lower() or "unidade" in msg.lower():
                    try:
                        resp = await backend.rest._request(  # noqa: SLF001
                            "GET", f"/processo/listar/unidades/{id_proc}"
                        )
                        raw = resp.get("data", []) if isinstance(resp, dict) else []
                        nomes = [
                            u.get("nome", u.get("sigla", ""))
                            for u in (raw if isinstance(raw, list) else [])
                            if isinstance(u, dict)
                        ]
                        return _json(
                            {
                                "error": msg,
                                "unidades_abertas": nomes,
                                "dica": "Conclua o processo nessas unidades antes de sobrestar.",
                            }
                        )
                    except Exception:  # noqa: BLE001, S110
                        pass
                return _error(msg)
        campos: dict[str, str] = {"txaMotivoSobrestamento": motivo}
        if processo_vinculado:
            campos["txtNrProcedimentoVinculado"] = processo_vinculado
        result = await backend.web.executar_acao_processo(
            processo, "procedimento_sobrestar", campos
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_remover_sobrestamento(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Remove o sobrestamento de um processo no SEI.

    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.remover_sobrestamento(id_proc)
            return _json(result)
        result = await backend.web.executar_acao_processo(
            processo, "procedimento_remover_sobrestamento"
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_dar_ciencia(
    referencia: str,
    tipo: Literal["documento", "processo"] = "documento",
    ctx: Context | None = None,
) -> str:
    """Dá ciência em um documento ou processo no SEI.

    Parâmetros:
    - referencia: número SEI do documento OU protocolo/IdProcedimento do processo
    - tipo: "documento" (padrão) ou "processo"

    Exemplos:
    - sei_dar_ciencia("1482875", tipo="documento")  → ciência na NT 16
    - sei_dar_ciencia("50300.018905/2018-67", tipo="processo")  → ciência no processo

    Funciona via REST (mod-wssei) ou via scraper web para tipo="processo" em
    instâncias sem mod-wssei. Tipo "documento" exige REST.
    """
    try:
        backend = _get_backend(ctx)

        if tipo == "documento":
            if not backend.has_rest:
                return _error(
                    "Dar ciência em documento requer mod-wssei (REST). "
                    "Configure SEI_URL ou use tipo='processo'."
                )
            doc_id, _ = await _resolver_documento(backend.rest, referencia)
            result = await backend.rest.dar_ciencia_documento(doc_id)
            return _json(result)

        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, referencia)
            result = await backend.rest.dar_ciencia_processo(id_proc)
            return _json(result)
        result = await backend.web.executar_acao_processo(referencia, "processo_dar_ciencia")
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_ciencias(
    referencia: str,
    tipo: Literal["documento", "processo"] = "documento",
    processo: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Lista as ciências registradas em um documento ou processo.

    Parâmetros:
    - referencia: número SEI do documento OU protocolo/IdProcedimento do processo
    - tipo: "documento" (padrão) ou "processo"
    - processo: protocolo do processo (necessário em instâncias sem mod-wssei quando tipo="documento")

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            if tipo == "documento":
                doc_id, _ = await _resolver_documento(backend.rest, referencia)
                result = await backend.rest.listar_ciencias_documento(doc_id)
            else:
                id_proc = await _resolver_processo(backend.rest, referencia)
                result = await backend.rest.listar_ciencias_processo(id_proc)
            return _json(result)
        # web fallback
        if tipo == "processo":
            return _error(
                "Listar ciências de processo requer mod-wssei (REST). "
                "Configure SEI_URL para habilitar esta funcionalidade."
            )
        if processo is None:
            return _error(
                "Em instâncias sem mod-wssei, forneça 'processo' para listar ciências de documento."
            )
        result = await backend.web.listar_ciencias_web(processo, referencia)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools adicionais de processo
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_remover_atribuicao(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Remove a atribuição de um processo (desatribui de qualquer usuário).

    - processo: protocolo formatado ou IdProcedimento
    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.remover_atribuicao(id_proc)
            return _json(result)
        result = await backend.web.executar_acao_processo(processo, "atribuicao_cancelar")
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_receber_processo(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Confirma o recebimento de um processo na unidade atual.

    - processo: protocolo formatado ou IdProcedimento
    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.receber_processo(id_proc)
            return _json(result)
        result = await backend.web.executar_acao_processo(processo, "procedimento_receber")
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_executar_acao(
    processo: str,
    acao: str,
    confirmar: bool = False,  # noqa: FBT001, FBT002
    ctx: Context | None = None,
) -> str:
    """Executa qualquer ação disponível no menu de um processo via scraper web.

    Parâmetros:
    - processo: protocolo formatado (ex: "50300.018905/2018-67")
    - acao: nome da ação no controlador SEI (ex: "procedimento_concluir")
    - confirmar: False (padrão) = dry-run que valida se a ação existe;
                 True = executa a ação de fato

    Esta é uma ferramenta de baixo nível — prefira as tools específicas
    (sei_concluir_processo, sei_reabrir_processo, etc.) quando disponíveis.
    Útil para ações sem tool dedicada ou para debugging.

    Exemplos:
    - sei_executar_acao("50300.018905/2018-67", "procedimento_concluir", confirmar=True)
    - sei_executar_acao("50300.018905/2018-67", "procedimento_visualizar")  # dry-run
    """
    if not confirmar:
        return _json(
            {
                "dry_run": True,
                "mensagem": (
                    f"Ação '{acao}' NÃO executada. Passe confirmar=True para executar. "
                    "Revise a ação antes de confirmar — algumas são irreversíveis."
                ),
                "processo": processo,
                "acao": acao,
            }
        )
    try:
        web = _get_web_client(ctx)
        result = await web.executar_acao_processo(processo, acao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_unidades_processo(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Lista as unidades onde o processo está aberto.

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.listar_unidades_processo(id_proc)
            return _json(result)
        detalhe = await backend.web.consultar_processo_detalhe(processo)
        return _json(detalhe.get("unidades_abertas", []))
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_interessados(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Lista os interessados de um processo.

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.listar_interessados(id_proc)
            return _json(result)
        detalhe = await backend.web.consultar_processo_detalhe(processo)
        return _json(detalhe.get("interessados", []))
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_sobrestamentos(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Lista o histórico de sobrestamentos de um processo.

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.listar_sobrestamentos(id_proc)
            return _json(result)
        detalhe = await backend.web.consultar_processo_detalhe(processo)
        return _json(detalhe.get("sobrestamentos", []))
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_assinaturas(
    id_documento: str,
    processo: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Lista as assinaturas de um documento.

    - id_documento: id interno do documento
    - processo: protocolo do processo (necessário em instâncias sem mod-wssei)

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            result = await backend.rest.listar_assinaturas(id_documento)
            return _json(result)
        if processo is None:
            return _error(
                "Em instâncias sem mod-wssei, forneça o parâmetro 'processo' "
                "para listar assinaturas."
            )
        result = await backend.web.listar_assinaturas_web(processo, id_documento)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_registrar_andamento(
    processo: str,
    descricao: str,
    ctx: Context | None = None,
) -> str:
    """Registra um andamento (atividade) no processo.

    - processo: protocolo formatado ou IdProcedimento
    - descricao: texto do andamento

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.registrar_andamento(id_proc, descricao)
            return _json(result)
        result = await backend.web.executar_acao_processo(
            processo, "procedimento_andamento_registrar", {"txaDescricao": descricao}
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_contatos(
    filtro: str = "",
    limit: int = 50,
    ctx: Context | None = None,
) -> str:
    """Pesquisa contatos cadastrados no SEI."""
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_contatos(filtro=filtro, limit=limit)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_criar_documento_externo(  # noqa: PLR0913
    processo: str,
    id_serie: str,
    arquivo_path: str,
    descricao: str = "",
    nivel_acesso: str = "0",
    ctx: Context | None = None,
) -> str:
    """Cria um documento externo (upload de arquivo) em um processo SEI.

    - processo: protocolo formatado ou IdProcedimento
    - id_serie: tipo do documento (use sei_pesquisar_tipos_documento)
    - arquivo_path: caminho local do arquivo (PDF, imagem, etc.)
    - descricao: descrição do documento
    - nivel_acesso: 0=público (padrão), 1=restrito, 2=sigiloso
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.criar_documento_externo(
            id_procedimento=id_proc,
            id_serie=id_serie,
            arquivo_path=arquivo_path,
            descricao=descricao,
            nivel_acesso=nivel_acesso,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_assinar_bloco(
    id_bloco: str,
    cargo: str = "",
    ctx: Context | None = None,
) -> str:
    """Assina TODOS os documentos de um bloco de assinatura.

    A autenticação é automática — basta informar o bloco e o cargo.

    IMPORTANTE: o parâmetro `cargo` é OBRIGATÓRIO. Sem ele a assinatura falha.
    Se não souber o cargo, chame sem cargo para ver a lista de opções.
    Pergunte ao usuário e grave o cargo para reutilizar na mesma conversa.

    - id_bloco: ID do bloco
    - cargo: cargo/função — OBRIGATÓRIO (se omitido, lista opções disponíveis)
    """
    try:
        client = _get_client(ctx)
        login = client._usuario  # noqa: SLF001
        senha = client._senha  # noqa: SLF001
        if not cargo:
            try:
                resp = await client._request("GET", "/assinante/listar")  # noqa: SLF001
                data = resp.json()
                cargos = data.get("data", [])
            except Exception:  # noqa: BLE001
                cargos = []
            return _json(
                {
                    "error": "Cargo/Função não informado.",
                    "cargos_disponiveis": cargos,
                    "dica": "Pergunte ao usuário qual cargo usar. "
                    "IMPORTANTE: após o usuário escolher, salve o cargo na memória da conversa "
                    "para reutilizar em todas as próximas assinaturas sem perguntar novamente.",
                }
            )
        result = await client.assinar_bloco(
            id_bloco=id_bloco,
            login=login,
            senha=senha,
            cargo=cargo,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_assinar_documentos_bloco(
    documentos: str,
    cargo: str = "",
    ctx: Context | None = None,
) -> str:
    """Assina documentos específicos de um bloco de assinatura.

    A autenticação é automática — basta informar os documentos e o cargo.

    IMPORTANTE: o parâmetro `cargo` é OBRIGATÓRIO. Sem ele a assinatura falha.
    Se não souber o cargo, chame sem cargo para ver a lista de opções.
    Pergunte ao usuário e grave o cargo para reutilizar na mesma conversa.

    - documentos: ID(s) de documento(s) separados por vírgula
    - cargo: cargo/função — OBRIGATÓRIO (se omitido, lista opções disponíveis)
    """
    try:
        client = _get_client(ctx)
        login = client._usuario  # noqa: SLF001
        senha = client._senha  # noqa: SLF001
        if not cargo:
            try:
                resp = await client._request("GET", "/assinante/listar")  # noqa: SLF001
                data = resp.json()
                cargos = data.get("data", [])
            except Exception:  # noqa: BLE001
                cargos = []
            return _json(
                {
                    "error": "Cargo/Função não informado.",
                    "cargos_disponiveis": cargos,
                    "dica": "Pergunte ao usuário qual cargo usar. "
                    "IMPORTANTE: após o usuário escolher, salve o cargo na memória da conversa "
                    "para reutilizar em todas as próximas assinaturas sem perguntar novamente.",
                }
            )
        result = await client.assinar_documentos_bloco(
            login=login,
            senha=senha,
            cargo=cargo,
            documentos=documentos,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de marcador
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_criar_marcador(
    nome: str,
    id_cor: str = "",
    ctx: Context | None = None,
) -> str:
    """Cria um marcador na unidade atual.

    - nome: nome do marcador
    - id_cor: ID da cor (use sei_listar_cores_marcador para ver opções).
      Se omitido, lista as cores disponíveis para escolha.
    """
    try:
        client = _get_client(ctx)
        if not id_cor:
            cores = await client.listar_cores_marcador()
            return _json(
                {
                    "error": "Cor não informada — escolha uma das cores disponíveis.",
                    "cores": cores,
                }
            )
        result = await client.criar_marcador(nome, id_cor)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_excluir_marcador(
    ids_marcadores: str,
    ctx: Context | None = None,
) -> str:
    """Exclui marcador(es). IDs separados por vírgula."""
    try:
        client = _get_client(ctx)
        result = await client.excluir_marcadores(ids_marcadores)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_marcar_processo(
    processo: str,
    marcador: str,
    texto: str = "",
    ctx: Context | None = None,
) -> str:
    """Adiciona ou altera marcador (etiqueta colorida) em um processo.

    Parâmetros:
    - processo: protocolo formatado ou IdProcedimento
    - marcador: ID do marcador OU "?" para listar os disponíveis
    - texto: texto/comentário associado ao marcador (opcional)

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.marcar_processo(id_proc, marcador, texto)
            return _json(result)
        if marcador == "?":
            form_info = await backend.web.obter_form_acao(processo, "marcador_alterar")
            return _json(
                {"marcadores_disponiveis": form_info.get("selects", {}).get("selMarcador", [])}
            )
        campos: dict[str, str] = {"selMarcador": marcador}
        if texto:
            campos["txtTexto"] = texto
        result = await backend.web.executar_acao_processo(processo, "marcador_alterar", campos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_marcadores(
    filtro: str = "",
    limit: int = 50,
    ctx: Context | None = None,
) -> str:
    """Lista marcadores disponíveis na unidade atual.

    Use o 'id' retornado em sei_marcar_processo.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_marcadores(filtro=filtro, limit=limit)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_consultar_marcador_processo(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Consulta os marcadores ativos de um processo."""
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.consultar_marcador_processo(id_proc)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de acompanhamento especial
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_acompanhar_processo(
    processo: str,
    grupo: str = "",
    observacao: str = "",
    ctx: Context | None = None,
) -> str:
    """Adiciona acompanhamento especial em um processo.

    Parâmetros:
    - processo: protocolo formatado ou IdProcedimento
    - grupo: ID do grupo de acompanhamento (ou "?" para listar disponíveis)
    - observacao: observação/anotação do acompanhamento

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.acompanhar_processo(id_proc, grupo, observacao)
            return _json(result)
        if grupo == "?":
            form_info = await backend.web.obter_form_acao(
                processo, "acompanhamento_especial_incluir"
            )
            return _json({"grupos_disponiveis": form_info.get("selects", {}).get("selGrupo", [])})
        campos: dict[str, str] = {}
        if grupo:
            campos["selGrupo"] = grupo
        if observacao:
            campos["txaObservacao"] = observacao
        result = await backend.web.executar_acao_processo(
            processo, "acompanhamento_especial_incluir", campos
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_remover_acompanhamento(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Remove acompanhamento especial de um processo.

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            acomp = await backend.rest.consultar_acompanhamento(id_proc)
            if not acomp:
                return _json({"mensagem": "Nenhum acompanhamento ativo neste processo."})
            id_acomp = str(acomp.get("idAcompanhamento", acomp.get("id", "")))
            if not id_acomp:
                return _error("Não foi possível identificar o acompanhamento.")
            result = await backend.rest.excluir_acompanhamento(id_acomp)
            return _json(result)
        result = await backend.web.executar_acao_processo(
            processo, "acompanhamento_especial_excluir"
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_criar_grupo_acompanhamento(
    nome: str,
    ctx: Context | None = None,
) -> str:
    """Cria um grupo de acompanhamento especial no SEI."""
    try:
        client = _get_client(ctx)
        result = await client.criar_grupo_acompanhamento(nome)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_excluir_grupo_acompanhamento(
    ids_grupos: str,
    ctx: Context | None = None,
) -> str:
    """Exclui grupo(s) de acompanhamento especial. IDs separados por vírgula."""
    try:
        client = _get_client(ctx)
        result = await client.excluir_grupo_acompanhamento(ids_grupos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_grupos_acompanhamento(
    filtro: str = "",
    ctx: Context | None = None,
) -> str:
    """Lista grupos de acompanhamento disponíveis."""
    try:
        client = _get_client(ctx)
        result = await client.listar_grupos_acompanhamento(filtro=filtro)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de bloco interno
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_criar_bloco_interno(
    descricao: str,
    ctx: Context | None = None,
) -> str:
    """Cria um bloco interno no SEI.

    Blocos internos são usados para organizar processos em lotes.
    """
    try:
        client = _get_client(ctx)
        result = await client.criar_bloco_interno(descricao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_incluir_processo_bloco_interno(
    id_bloco: str,
    processos: str,
    ctx: Context | None = None,
) -> str:
    """Inclui processo(s) em um bloco interno.

    - id_bloco: ID do bloco
    - processos: IdProcedimento(s) separados por vírgula
    """
    try:
        client = _get_client(ctx)
        result = await client.incluir_processo_bloco_interno(id_bloco, processos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_retirar_processo_bloco_interno(
    id_bloco: str,
    processos: str,
    ctx: Context | None = None,
) -> str:
    """Remove processo(s) de um bloco interno.

    - id_bloco: ID do bloco
    - processos: IdProcedimento(s) separados por vírgula
    """
    try:
        client = _get_client(ctx)
        result = await client.retirar_processo_bloco_interno(id_bloco, processos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Tools de bloco de assinatura
# ---------------------------------------------------------------------------


@mcp.tool()
async def sei_criar_bloco_assinatura(
    descricao: str,
    unidades: str = "",
    ctx: Context | None = None,
) -> str:
    """Cria um bloco de assinatura no SEI.

    Parâmetros:
    - descricao: descrição do bloco
    - unidades: sigla(s) ou ID(s) das unidades para disponibilizar
      (separados por vírgula). Se informar sigla, resolve automaticamente.
    """
    try:
        client = _get_client(ctx)

        # Resolver siglas de unidades para IDs
        if unidades:
            destinos = [u.strip() for u in unidades.split(",")]
            ids = []
            for d in destinos:
                if d.isdigit():
                    ids.append(d)
                else:
                    result = await client.pesquisar_unidades(filtro=d, limit=5)
                    found = False
                    for u in result.get("unidades", []):
                        if u.get("sigla", "").upper() == d.upper():
                            ids.append(str(u.get("id", "")))
                            found = True
                            break
                    if not found and result.get("unidades"):
                        ids.append(str(result["unidades"][0].get("id", "")))
            unidades = ",".join(ids)

        result = await client.criar_bloco_assinatura(descricao, unidades)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_incluir_documento_bloco_assinatura(
    id_bloco: str,
    documentos: str,
    ctx: Context | None = None,
) -> str:
    """Inclui documento(s) em um bloco de assinatura.

    - id_bloco: ID do bloco de assinatura
    - documentos: ID(s) de documento(s) separados por vírgula
    """
    try:
        client = _get_client(ctx)
        result = await client.incluir_documento_bloco_assinatura(id_bloco, documentos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_disponibilizar_bloco_assinatura(
    id_bloco: str,
    ctx: Context | None = None,
) -> str:
    """Disponibiliza um bloco de assinatura para as unidades configuradas.

    Após disponibilizar, os usuários das unidades podem assinar os documentos.
    """
    try:
        client = _get_client(ctx)
        result = await client.disponibilizar_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_cancelar_disponibilizacao_bloco(
    id_bloco: str,
    ctx: Context | None = None,
) -> str:
    """Cancela a disponibilização de um bloco de assinatura.

    O bloco volta ao estado aberto e pode ser editado novamente.
    """
    try:
        client = _get_client(ctx)
        result = await client.cancelar_disponibilizacao_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_blocos_assinatura(
    filtro: str = "",
    limit: int = 50,
    ctx: Context | None = None,
) -> str:
    """Pesquisa blocos de assinatura existentes."""
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_blocos_assinatura(filtro=filtro, limit=limit)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_criar_anotacao(
    processo: str,
    descricao: str,
    prioridade: str = "1",
    ctx: Context | None = None,
) -> str:
    """Cria uma anotação (post-it) em um processo no SEI.

    Parâmetros:
    - processo: protocolo formatado (ex: 50300.018905/2018-67) ou IdProcedimento
    - descricao: texto da anotação
    - prioridade: nível de prioridade (1=normal, 2=alta)

    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if backend.has_rest:
            id_proc = await _resolver_processo(backend.rest, processo)
            result = await backend.rest.criar_anotacao(
                protocolo=id_proc, descricao=descricao, prioridade=prioridade
            )
            return _json(result)
        result = await backend.web.executar_acao_processo(
            processo,
            "anotacao_incluir",
            {"txaDescricao": descricao, "selPrioridade": prioridade},
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# ---------------------------------------------------------------------------
# Endpoints adicionais do mod-wssei v2
# Todos disponíveis desde mod-wssei 2.0.0 (SEI 4.0.x), exceto:
#   - sei_listar_relacionamentos → requer mod-wssei 3.0.2+ (SEI 5.0.x)
# Se um endpoint falhar, use sei_versao para verificar a versão instalada.
# Compatibilidade: SEI 4.0.x=mod-wssei 2.0.x | SEI 4.1.1=2.2.0 | SEI 5.0.x=3.0.x
# ---------------------------------------------------------------------------


# -- Sistema / Informações --


@mcp.tool()
async def sei_versao(ctx: Context) -> str:
    """Retorna a versão do SEI e do módulo wssei instalado.

    Útil para verificar compatibilidade de funcionalidades.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.versao()
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_orgaos(ctx: Context) -> str:
    """Lista os órgãos cadastrados na instalação do SEI.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_orgaos()
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_contextos(id_orgao: str, ctx: Context) -> str:
    """Lista os contextos disponíveis para um órgão.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_contextos(id_orgao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Usuários --


@mcp.tool()
async def sei_pesquisar_usuarios(
    filtro: str = "",
    id_orgao: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa usuários por palavra-chave no órgão.

    Diferente de sei_listar_usuarios (que lista por unidade),
    este pesquisa no servidor por nome/sigla em todo o órgão.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_usuarios(
            filtro=filtro, id_orgao=id_orgao, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Unidades --


@mcp.tool()
async def sei_pesquisar_outras_unidades(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa unidades excluindo a unidade atual.

    Útil para tramitação — já filtra a unidade do usuário.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_outras_unidades(filtro=filtro, limit=limit, start=pagina)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_textos_padrao(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa textos padrão internos disponíveis na unidade.

    Textos padrão são modelos reutilizáveis para preencher documentos
    automaticamente ao criar um novo documento interno.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_textos_padrao(filtro=filtro, limit=limit, start=pagina)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Documentos --


@mcp.tool()
async def sei_consultar_documento_externo(
    id_documento: str,
    processo: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Consulta metadados de um documento externo pelo ID.

    Aceita tanto o id interno (ex: "3149544") quanto o número SEI /
    protocoloFormatado (ex: "2867926") — auto-resolve via pesquisa Solr
    quando necessário.

    - processo: protocolo do processo (necessário em instâncias sem mod-wssei)

    Retorna informações como tipo, data, nível de acesso, etc.
    Para baixar o conteúdo use sei_baixar_anexo ou sei_ler_documento.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).

    Quando o documento é restrito ou sigiloso (nivelAcesso 1 ou 2), a
    resposta inclui o campo `_aviso_acesso` — aviso INFORMATIVO de
    privacidade, NÃO erro de permissão. Os metadados foram retornados
    normalmente; não tente trocar de unidade ou rotas alternativas.
    Se falhar com erro inesperado, use sei_versao para verificar a versão.
    Funciona via REST (mod-wssei) ou via scraper web (instâncias sem mod-wssei).
    """
    try:
        backend = _get_backend(ctx)
        if not backend.has_rest:
            if processo is None:
                return _error(
                    "Em instâncias sem mod-wssei, forneça o parâmetro 'processo' "
                    "para consultar metadados de documento."
                )
            result = await backend.web.consultar_documento_web(processo, id_documento)
            return _json(result)
        client = _get_client(ctx)
        try:
            result = await client.consultar_documento_externo(id_documento)
        except Exception as primeira:
            msg = str(primeira)
            low = msg.lower()
            # Se não autorizado, pode ser id errado (passou número SEI). Tenta resolver.
            if "não autorizado" in low or "nao autorizado" in low:
                try:
                    doc_id, _ = await _resolver_documento(client, id_documento)
                    if doc_id != id_documento:
                        id_documento = doc_id
                        result = await client.consultar_documento_externo(id_documento)
                    else:
                        raise primeira  # noqa: TRY201, TRY301
                except Exception:  # noqa: BLE001
                    return _json(
                        {
                            "error": msg,
                            "dica": (
                                "SEI retornou 'não autorizado' para o id "
                                f"{id_documento!r}. Verifique se você passou o id "
                                "INTERNO do documento (ex.: 3149544) e não o número "
                                "SEI / protocoloFormatado (ex.: 2867926). Use "
                                "sei_buscar_documento para resolver número SEI → id."
                            ),
                        }
                    )
            else:
                raise

        nivel, hipotese = access_control.extrair_nivel(result)
        if access_control.precisa_disclaimer(nivel):
            result["_aviso_acesso"] = access_control.construir_disclaimer_acompanhante(
                nivel,
                hipotese,
                alvo={
                    "tipo": "documento",
                    "id": str(id_documento),
                    "tipo_documento": "X",
                },
            )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_alterar_documento_interno(
    id_documento: str,
    descricao: str = "",
    nivel_acesso: str = "",
    hipotese_legal: str = "",
    ctx: Context | None = None,
) -> str:
    """Altera metadados de um documento interno (não o conteúdo HTML).

    Para alterar o conteúdo, use sei_editar_secao.
    - id_documento: ID interno do documento
    - descricao: nova descrição
    - nivel_acesso: 0=público, 1=restrito, 2=sigiloso
    - hipotese_legal: ID da hipótese (obrigatório se restrito/sigiloso)

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.alterar_documento_interno(
            id_documento=id_documento,
            descricao=descricao,
            nivel_acesso=nivel_acesso,
            id_hipotese_legal=hipotese_legal,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_alterar_documento_externo(  # noqa: PLR0913
    id_documento: str,
    descricao: str = "",
    nivel_acesso: str = "",
    hipotese_legal: str = "",
    arquivo_path: str = "",
    ctx: Context | None = None,
) -> str:
    """Altera metadados de um documento externo (e opcionalmente substitui o arquivo).

    - id_documento: ID interno do documento
    - descricao: nova descrição
    - nivel_acesso: 0=público, 1=restrito, 2=sigiloso
    - hipotese_legal: ID da hipótese (obrigatório se restrito/sigiloso)
    - arquivo_path: caminho local de novo arquivo para substituir (opcional)

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.alterar_documento_externo(
            id_documento=id_documento,
            descricao=descricao,
            nivel_acesso=nivel_acesso,
            id_hipotese_legal=hipotese_legal,
            arquivo_path=arquivo_path,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_tipos_conferencia(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa tipos de conferência para documentos externos.

    Tipo de conferência indica se o documento externo é cópia autenticada,
    cópia simples, original, etc.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_tipos_conferencia(filtro=filtro, limit=limit, start=pagina)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_sugestao_assuntos_documento(
    id_serie: str,
    ctx: Context | None = None,
) -> str:
    """Lista sugestões de assuntos para um tipo de documento (série).

    Use o id_serie obtido via sei_pesquisar_tipos_documento.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.sugestao_assuntos_documento(id_serie)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_blocos_documento(
    id_documento: str,
    ctx: Context | None = None,
) -> str:
    """Lista blocos de assinatura em que um documento está incluído.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_blocos_documento(id_documento)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_pesquisar_tipos_documento_externo(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa tipos de documento para documentos externos (séries externas).

    Diferente de sei_pesquisar_tipos_documento que lista todos os tipos,
    este retorna apenas os tipos aplicáveis a documentos externos.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_tipos_documento_externo(
            filtro=filtro, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_parametros_upload(ctx: Context) -> str:
    """Retorna parâmetros de upload do SEI (extensões permitidas, tamanhos máximos).

    Útil antes de criar documentos externos para saber os limites.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.parametros_upload()
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Processos: assuntos, atribuição, acesso, relacionamentos --


@mcp.tool()
async def sei_pesquisar_assuntos(
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Pesquisa assuntos disponíveis para processos.

    Use o ID retornado no campo 'assuntos' ao criar processos.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.pesquisar_assuntos(filtro=filtro, limit=limit, start=pagina)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_sugestao_assuntos_processo(
    id_tipo_processo: str,
    ctx: Context | None = None,
) -> str:
    """Lista sugestões de assuntos para um tipo de processo.

    Use o id do tipo obtido via sei_pesquisar_tipos_processo.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.sugestao_assuntos_processo(id_tipo_processo)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_consultar_atribuicao(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Consulta a atribuição atual de um processo (quem está responsável).

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.consultar_atribuicao(id_proc)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_verificar_acesso(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Verifica se o usuário tem acesso a um processo.

    Útil para checar permissão antes de operações em processos restritos.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.verificar_acesso(id_proc)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_relacionamentos(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Lista processos relacionados a um processo.

    REQUER mod-wssei 3.0.2+ (SEI 5.0.x). Não disponível em versões anteriores.
    Se falhar, use sei_versao para verificar. Precisa ser >= 3.0.2.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.listar_relacionamentos(id_proc)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_atividades(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Lista o histórico de atividades/andamentos de um processo.

    Implementação via scraper web (procedimento_consultar_historico.php).
    Retorna todas as ações registradas (tramitações, assinaturas, edições, etc.)
    com data/hora, unidade, usuário e descrição.

    Aceita protocolo formatado (ex: 50300.000123/2025-00).
    """
    try:
        web = _get_web_client(ctx)
        result = await web.listar_atividades(processo)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_gerar_pdf_processo(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Gera e baixa o PDF consolidado de um processo SEI.

    Consolida todos os documentos do processo num único PDF, exatamente
    como o botão "Gerar PDF" da interface web do SEI.

    Implementação via scraper web (procedimento_gerar_pdf).

    Parâmetros:
    - processo: protocolo formatado (ex: 0029.000123/2024-00)

    Retorna base64 do PDF, tamanho e caminho do arquivo salvo em disco.

    Nota: o processo precisa estar aberto na caixa da unidade atual.
    Para processos de outras unidades, use sei_trocar_unidade primeiro.
    """
    import tempfile  # noqa: PLC0415

    try:
        web = _get_web_client(ctx)

        pdf_bytes = await web.gerar_pdf_processo(processo)

        tamanho_mb = len(pdf_bytes) / 1024 / 1024
        if tamanho_mb > 50:  # noqa: PLR2004
            return _error(f"PDF muito grande ({tamanho_mb:.1f} MB). Baixe manualmente pelo SEI.")

        protocolo_safe = processo.replace("/", "-")
        pdf_path = os.path.join(tempfile.gettempdir(), f"SEI_{protocolo_safe}.pdf")  # noqa: PTH118
        with open(pdf_path, "wb") as f:  # noqa: ASYNC230, PTH123
            f.write(pdf_bytes)

        return _json(
            {
                "arquivo": pdf_path,
                "tamanho_mb": round(tamanho_mb, 2),
                "tamanho_bytes": len(pdf_bytes),
                "base64": base64.b64encode(pdf_bytes).decode(),
            }
        )
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_gerar_zip_processo(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Gera e baixa o ZIP com todos os documentos de um processo SEI.

    Baixa todos os documentos do processo num único arquivo ZIP, exatamente
    como o botão "Gerar ZIP" da interface web do SEI.

    Implementação via scraper web (procedimento_gerar_zip).

    Parâmetros:
    - processo: protocolo formatado (ex: 0029.000123/2024-00)

    Retorna base64 do ZIP, tamanho e caminho do arquivo salvo em disco.
    """
    import tempfile  # noqa: PLC0415

    try:
        web = _get_web_client(ctx)

        zip_bytes = await web.gerar_zip_processo(processo)

        tamanho_mb = len(zip_bytes) / 1024 / 1024
        if tamanho_mb > 200:  # noqa: PLR2004
            return _error(f"ZIP muito grande ({tamanho_mb:.1f} MB). Baixe manualmente pelo SEI.")

        protocolo_safe = processo.replace("/", "-")
        zip_path = os.path.join(tempfile.gettempdir(), f"SEI_{protocolo_safe}.zip")  # noqa: PTH118
        with open(zip_path, "wb") as f:  # noqa: ASYNC230, PTH123
            f.write(zip_bytes)

        return _json(
            {
                "arquivo": zip_path,
                "tamanho_mb": round(tamanho_mb, 2),
                "tamanho_bytes": len(zip_bytes),
                "base64": base64.b64encode(zip_bytes).decode(),
            }
        )
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_incluir_documento_externo(  # noqa: PLR0913
    processo: str,
    arquivo_path: str = "",
    arquivo_base64: str = "",
    nome_arquivo: str = "",
    id_serie: str = "",
    data_elaboracao: str = "",
    nivel_acesso: str = "0",
    hipotese_legal: str = "",
    ctx: Context | None = None,
) -> str:
    """Inclui documento externo (PDF, imagem, etc.) em um processo SEI via web scraper.

    Implementação via scraper web — funciona em instâncias sem mod-wssei REST.

    Parâmetros:
    - processo: protocolo formatado (ex: 0020.008886/2026-49)
    - arquivo_path: caminho local do arquivo (apenas em modo stdio/local;
      ex: C:/Users/frank/Downloads/NF52.pdf)
    - arquivo_base64: conteúdo do arquivo em base64 (obrigatório em modo
      remoto/HTTP; alternativa a arquivo_path)
    - nome_arquivo: nome do arquivo com extensão (obrigatório com arquivo_base64;
      ex: NF52.pdf)
    - id_serie: ID do tipo de documento no SEI. Se vazio, retorna lista de tipos disponíveis.
      Para Nota Fiscal, use sei_pesquisar_tipos_documento para descobrir o id.
    - data_elaboracao: data de elaboração no formato dd/mm/aaaa (padrão: hoje)
    - nivel_acesso: 0=público (padrão), 1=restrito, 2=sigiloso
    - hipotese_legal: ID da hipótese legal (obrigatório se nivel_acesso=1 ou 2).
      Use sei_listar_hipoteses_legais para descobrir os IDs disponíveis.

    Se id_serie não for informado, retorna os tipos disponíveis para que você
    possa escolher e chamar novamente com o id correto.

    Nota: o processo deve estar aberto na caixa da unidade atual.
    Se o processo estiver concluído, use sei_reabrir_processo primeiro.
    """
    try:
        conteudo: bytes | None = None
        if arquivo_base64:
            if not nome_arquivo:
                return _error("nome_arquivo é obrigatório quando arquivo_base64 é usado.")
            try:
                conteudo = base64.b64decode(arquivo_base64, validate=True)
            except Exception:  # noqa: BLE001
                return _error("arquivo_base64 inválido (não é base64 válido).")
        elif arquivo_path:
            # Em modo remoto o caminho apontaria para o filesystem do SERVIDOR,
            # permitindo exfiltrar arquivos do host — exigir base64.
            if _http_mode:
                return _error(
                    "Em modo remoto use arquivo_base64 + nome_arquivo "
                    "(caminhos do servidor não são permitidos)."
                )
        elif id_serie:
            return _error("Informe arquivo_path (local) ou arquivo_base64 (remoto).")

        web = _get_web_client(ctx)

        result = await web.incluir_documento_externo(
            protocolo_formatado=processo,
            arquivo_path=arquivo_path or None,
            nome_arquivo=nome_arquivo or None,
            id_serie=id_serie or None,
            data_elaboracao=data_elaboracao,
            nivel_acesso=nivel_acesso,
            hipotese_legal=hipotese_legal,
            conteudo=conteudo,
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Acompanhamento: meus, da unidade, alterar --


@mcp.tool()
async def sei_listar_meus_acompanhamentos(
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Lista processos que o usuário está acompanhando (acompanhamento especial).

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_meus_acompanhamentos(limit=limit, start=pagina)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_acompanhamentos_unidade(
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Lista processos com acompanhamento especial na unidade atual.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_acompanhamentos_unidade(limit=limit, start=pagina)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_alterar_acompanhamento(
    processo: str,
    grupo: str = "",
    observacao: str = "",
    ctx: Context | None = None,
) -> str:
    """Altera acompanhamento especial de um processo.

    - processo: protocolo formatado ou IdProcedimento
    - grupo: novo grupo de acompanhamento
    - observacao: nova observação

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.alterar_acompanhamento(id_proc, grupo, observacao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Credenciamento (processos sigilosos) --


@mcp.tool()
async def sei_listar_credenciamentos(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Lista credenciamentos de acesso a um processo sigiloso.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.listar_credenciamentos(id_proc)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_conceder_credenciamento(
    processo: str,
    id_usuario: str,
    ctx: Context | None = None,
) -> str:
    """Concede credenciamento de acesso a um processo sigiloso para um usuário.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.conceder_credenciamento(id_proc, id_usuario)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_renunciar_credenciamento(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Renuncia ao credenciamento de acesso a um processo sigiloso.

    O próprio usuário perde o acesso ao processo.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.renunciar_credenciamento(id_proc)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_cassar_credenciamento(
    processo: str,
    id_usuario: str,
    ctx: Context | None = None,
) -> str:
    """Cassa (revoga) credenciamento de acesso de um usuário a processo sigiloso.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.cassar_credenciamento(id_proc, id_usuario)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Assinantes e Observação --


@mcp.tool()
async def sei_listar_assinantes(ctx: Context) -> str:
    """Lista signatários (cargos/funções) disponíveis na unidade atual.

    Retorna os cargos que podem ser usados em sei_assinar_documento.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_assinantes()
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_orgaos_assinante(ctx: Context) -> str:
    """Lista órgãos disponíveis para assinatura.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_orgaos_assinante()
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_criar_observacao(
    processo: str,
    descricao: str,
    ctx: Context | None = None,
) -> str:
    """Cria observação da unidade em um processo.

    Diferente da anotação (post-it individual), a observação é
    vinculada à unidade e visível por todos os usuários da unidade.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.criar_observacao(id_proc, descricao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_criar_contato(
    nome: str,
    tipo: str = "",
    email: str = "",
    telefone: str = "",
    ctx: Context | None = None,
) -> str:
    """Cria novo contato no SEI.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.criar_contato(nome=nome, tipo=tipo, email=email, telefone=telefone)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Modelos de documento --


@mcp.tool()
async def sei_listar_grupos_modelos(
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Lista grupos de modelos de documento disponíveis.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_grupos_modelos(limit=limit, start=pagina)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_listar_modelos(
    id_grupo: str = "",
    filtro: str = "",
    limit: int = 50,
    pagina: int = 0,
    ctx: Context | None = None,
) -> str:
    """Lista modelos de documento disponíveis.

    - id_grupo: filtrar por grupo (use sei_listar_grupos_modelos)
    - filtro: texto para filtrar por nome

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_modelos(
            id_grupo=id_grupo, filtro=filtro, limit=limit, start=pagina
        )
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Marcador: desativar, reativar, histórico --


@mcp.tool()
async def sei_desativar_marcador(
    ids_marcadores: str,
    ctx: Context | None = None,
) -> str:
    """Desativa marcador(es) sem excluir. IDs separados por vírgula.

    Marcadores desativados deixam de aparecer nas pesquisas mas
    mantêm o histórico. Use sei_reativar_marcador para reativar.
    """
    try:
        client = _get_client(ctx)
        result = await client.desativar_marcadores(ids_marcadores)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_reativar_marcador(
    ids_marcadores: str,
    ctx: Context | None = None,
) -> str:
    """Reativa marcador(es) desativados. IDs separados por vírgula."""
    try:
        client = _get_client(ctx)
        result = await client.reativar_marcadores(ids_marcadores)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_historico_marcador_processo(
    processo: str,
    ctx: Context | None = None,
) -> str:
    """Lista histórico de marcadores de um processo.

    Mostra quais marcadores foram aplicados/removidos ao longo do tempo.
    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.historico_marcador_processo(id_proc)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Bloco Interno: operações adicionais --


@mcp.tool()
async def sei_listar_processos_bloco_interno(
    id_bloco: str,
    ctx: Context | None = None,
) -> str:
    """Lista processos de um bloco interno.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.listar_processos_bloco_interno(id_bloco)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_alterar_bloco_interno(
    id_bloco: str,
    descricao: str,
    ctx: Context | None = None,
) -> str:
    """Altera descrição de um bloco interno.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.alterar_bloco_interno(id_bloco, descricao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_excluir_bloco_interno(
    ids_blocos: str,
    ctx: Context | None = None,
) -> str:
    """Exclui bloco(s) interno(s). IDs separados por vírgula.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.excluir_blocos_internos(ids_blocos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_concluir_bloco_interno(
    ids_blocos: str,
    ctx: Context | None = None,
) -> str:
    """Conclui bloco(s) interno(s). IDs separados por vírgula.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.concluir_blocos_internos(ids_blocos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_reabrir_bloco_interno(
    id_bloco: str,
    ctx: Context | None = None,
) -> str:
    """Reabre bloco interno concluído.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.reabrir_bloco_interno(id_bloco)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_anotar_processo_bloco_interno(
    id_bloco: str,
    processo: str,
    descricao: str,
    ctx: Context | None = None,
) -> str:
    """Cria anotação em processo dentro de um bloco interno.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.anotar_processo_bloco_interno(id_bloco, id_proc, descricao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_alterar_anotacao_bloco_interno(
    id_bloco: str,
    processo: str,
    descricao: str,
    ctx: Context | None = None,
) -> str:
    """Altera anotação de processo em um bloco interno.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        id_proc = await _resolver_processo(client, processo)
        result = await client.alterar_anotacao_bloco_interno(id_bloco, id_proc, descricao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


# -- Bloco de Assinatura: operações adicionais --


@mcp.tool()
async def sei_listar_documentos_bloco_assinatura(
    id_bloco: str,
    ctx: Context | None = None,
) -> str:
    """Lista documentos de um bloco de assinatura."""
    try:
        client = _get_client(ctx)
        result = await client.listar_documentos_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_retirar_documentos_bloco_assinatura(
    id_bloco: str,
    documentos: str,
    ctx: Context | None = None,
) -> str:
    """Retira documento(s) de um bloco de assinatura.

    - documentos: ID(s) de documento(s) separados por vírgula
    """
    try:
        client = _get_client(ctx)
        result = await client.retirar_documento_bloco_assinatura(id_bloco, documentos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_alterar_bloco_assinatura(
    id_bloco: str,
    descricao: str,
    ctx: Context | None = None,
) -> str:
    """Altera descrição de um bloco de assinatura.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.alterar_bloco_assinatura(id_bloco, descricao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_excluir_bloco_assinatura(
    ids_blocos: str,
    ctx: Context | None = None,
) -> str:
    """Exclui bloco(s) de assinatura. IDs separados por vírgula.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.excluir_blocos_assinatura(ids_blocos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_concluir_bloco_assinatura(
    ids_blocos: str,
    ctx: Context | None = None,
) -> str:
    """Conclui bloco(s) de assinatura. IDs separados por vírgula.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.concluir_blocos_assinatura(ids_blocos)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_reabrir_bloco_assinatura(
    id_bloco: str,
    ctx: Context | None = None,
) -> str:
    """Reabre bloco de assinatura concluído.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.reabrir_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_retornar_bloco_assinatura(
    id_bloco: str,
    ctx: Context | None = None,
) -> str:
    """Retorna bloco de assinatura para a unidade de origem.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.retornar_bloco_assinatura(id_bloco)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_anotar_documento_bloco_assinatura(
    id_bloco: str,
    documento: str,
    descricao: str,
    ctx: Context | None = None,
) -> str:
    """Cria anotação em documento dentro de um bloco de assinatura.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.anotar_documento_bloco_assinatura(id_bloco, documento, descricao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


@mcp.tool()
async def sei_alterar_anotacao_bloco_assinatura(
    id_bloco: str,
    documento: str,
    descricao: str,
    ctx: Context | None = None,
) -> str:
    """Altera anotação de documento em um bloco de assinatura.

    Disponível desde mod-wssei 2.0.0 (SEI 4.0.x).
    Se falhar com erro inesperado, use sei_versao para verificar a versão instalada.
    """
    try:
        client = _get_client(ctx)
        result = await client.alterar_anotacao_bloco_assinatura(id_bloco, documento, descricao)
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _error(str(e))


def main():  # noqa: ANN201, D103
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from todos.setup_wizard import run_setup_wizard  # noqa: PLC0415

        run_setup_wizard()
        return

    if _http_mode:
        from todos.remote import run_remote  # noqa: PLC0415

        run_remote(mcp, port=_http_port)
    else:
        mcp.run(transport="stdio", show_banner=False)
