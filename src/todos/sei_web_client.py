"""Cliente HTTP para o frontend web do SEI (scraper).

Alternativa de alta performance ao mod-wssei REST para operações de listagem
e navegação. Login via formulário SIP, navegação via páginas pré-assinadas
com `infra_hash` capturado na cadeia de redirects.

Performance medida (sei.antaq.gov.br, abril/2026):
- listar_processos: ~14.5 s (REST) → ~0.6 s (web) → 23× mais rápido
- consultar_processo: ~5.9 s (REST 2 calls) → ~0.9 s (web 2 calls) → 6× mais rápido

Limitações:
- Requer cadeia inicial de login (~3-4 s, uma vez por sessão)
- Layout dos campos depende da configuração de painel do usuário no SEI
- Sem suporte a 2FA ou CAPTCHA (aborta com erro)
- Específico para instâncias SEI com Infra v1.5x+ (login form com hdnToken)
"""

from __future__ import annotations

import logging
import os
import re
import time
import warnings
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# TTL do cache da árvore do processo (links assinados valem a sessão inteira;
# o TTL curto limita apenas a janela de staleness do conteúdo da árvore)
_ARVORE_CACHE_TTL = 30.0


def _tag_str(tag: Tag, attr: str, default: str = "") -> str:
    """Return a BS4 tag attribute as plain str (Tag.get returns str|list|None)."""
    v = tag.get(attr, default)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return v[0] if v else default
    return default


def _extrair_erro_sei(html: str) -> str | None:
    """Extrai mensagem de erro do HTML do SEI, se houver.

    O SEI exibe erros em divs/spans com classes infraMsg ou infraMensagemErro,
    ou como alertas JavaScript. Retorna None se não houver erro detectável.
    """
    soup = BeautifulSoup(html, "html.parser")
    for el in (
        soup.find(class_="infraMsg"),
        soup.find(class_="infraMensagemErro"),
        soup.find(id="divInfraMensagem"),
        soup.find(class_="alert-danger"),
    ):
        if el and isinstance(el, Tag):
            txt = el.get_text(" ", strip=True)
            if txt:
                return txt
    # JavaScript alert("mensagem de erro")
    m = re.search(r"alert\(['\"](.{10,300})['\"]", html)
    if m:
        return m.group(1)
    return None


def _extrair_submit_btn(form: Tag) -> tuple[str, str] | None:
    """Extrai o par (name, value) do botão submit de um form.

    O PHP do SEI exige o par name=value do botão submit no POST; sem ele
    ignora o form silenciosamente. Válido para input[type=submit] e button.
    """
    btn = form.find("input", type="submit") or form.find("button", type="submit")
    if btn and isinstance(btn, Tag):
        name = _tag_str(btn, "name")
        if name:
            value = _tag_str(btn, "value") or btn.get_text(strip=True) or "Enviar"
            return name, value
    return None


class SEIWebClient:
    """Cliente HTTP assíncrono para o frontend web do SEI.

    Mantém uma sessão SIP autenticada e cacheia o `infra_hash` da inbox URL
    e o action+hidden fields do form principal de procedimento_controlar.

    Uso:
        client = SEIWebClient()
        await client.login()
        layout, rows = await client.listar_processos(detalhada=True)
        await client.close()

    A reutilização da sessão é o que torna esse client rápido — login custa
    ~3 s mas listagens subsequentes custam ~600 ms cada.
    """

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401, D107
        # Reusa as mesmas env vars do SEIClient REST
        sei_url = kwargs.get("sei_url", os.environ.get("SEI_URL", ""))
        # SEI_WEB_URL permite modo web-only (sem mod-wssei) apontando direto para
        # a raiz do SEI (ex: https://sei.orgao.gov.br). Tem precedência sobre SEI_URL.
        sei_web_url = kwargs.get("sei_web_url", os.environ.get("SEI_WEB_URL", ""))
        if sei_web_url:
            self.sei_root = sei_web_url.rstrip("/")
        elif "/sei/" in sei_url:
            # Deriva raiz a partir da URL da REST
            # Ex: https://sei.antaq.gov.br/sei/modulos/wssei/... → https://sei.antaq.gov.br
            self.sei_root = sei_url.split("/sei/", 1)[0]
        else:
            self.sei_root = sei_url.rstrip("/")

        self._usuario = kwargs.get("sei_usuario", os.environ.get("SEI_USUARIO", ""))
        
        senha = kwargs.get("sei_senha", os.environ.get("SEI_SENHA", ""))
        if not senha and self._usuario:
            try:
                import keyring
                senha_keyring = keyring.get_password("todos-mcp", self._usuario)
                if senha_keyring:
                    senha = senha_keyring
            except Exception as e:
                logger.warning("Não foi possível obter a senha do keyring: %s", e)
        self._senha = senha

        # SEI_ORGAO no .env é o id da REST (geralmente "0"). O selOrgao do SIP
        # é descoberto dinamicamente do <select> na página de login.
        self._sigla_orgao = kwargs.get(
            "sei_sigla_orgao", os.environ.get("SEI_SIGLA_ORGAO", "ANTAQ")
        )
        self._sigla_sistema = kwargs.get(
            "sei_sigla_sistema", os.environ.get("SEI_SIGLA_SISTEMA", "SEI")
        )
        # SEI_SIGLA_ORGAO_SISTEMA: parâmetro da URL do SIP login (ex: "RO" para Rondônia).
        # Quando não definido, usa SEI_SIGLA_ORGAO (mantém compatibilidade p/ instâncias
        # onde sigla_orgao_sistema == sigla do órgão no selOrgao, ex: ANTAQ).
        _sigla_orgao_sistema = kwargs.get(
            "sei_sigla_orgao_sistema",
            os.environ.get("SEI_SIGLA_ORGAO_SISTEMA", self._sigla_orgao),
        )

        verify_ssl = kwargs.get("sei_verify_ssl", os.environ.get("SEI_VERIFY_SSL", "true"))
        if isinstance(verify_ssl, str):
            verify_ssl = verify_ssl.lower() != "false"
        if not verify_ssl:
            warnings.filterwarnings("ignore", message="Unverified HTTPS request")

        self.login_url = (
            f"{self.sei_root}/sip/login.php"
            f"?sigla_orgao_sistema={_sigla_orgao_sistema}&sigla_sistema={self._sigla_sistema}"
        )

        self._http = httpx.AsyncClient(
            verify=verify_ssl,
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=10.0, read=45.0),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            },
        )
        self._inbox_url: httpx.URL | None = None
        self._unidade_atual: dict[str, str] | None = None
        # cache do form principal de procedimento_controlar (action + hidden fields)
        self._form_action: str | None = None
        self._form_hidden: dict[str, str] = {}
        # cache de URLs de processos individuais (protocolo → href pré-assinado)
        self._trabalhar_links: dict[str, str] = {}
        # URL do form de pesquisa rápida (protocolo_pesquisa_rapida + infra_hash)
        self._pesquisa_rapida_action: str | None = None
        # cache curto da árvore (protocolo → (ts, (html, url))): evita refetch
        # quando várias ações usam a mesma árvore em sequência (ex: ler vários
        # documentos do mesmo processo, ou fallback interno→externo)
        self._arvore_cache: dict[str, tuple[float, tuple[str, str]]] = {}

    async def close(self) -> None:  # noqa: D102
        await self._http.aclose()

    async def ensure_authenticated(self) -> None:
        """Garante sessão SIP ativa; faz login automaticamente se necessário."""
        if self._inbox_url is None:
            await self.login()

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------

    async def login(self) -> None:  # noqa: C901, PLR0912, PLR0915
        """Faz login via formulário SIP e captura a inbox URL com infra_hash."""
        if not self.sei_root:
            raise RuntimeError(  # noqa: TRY003
                "Nenhuma URL do SEI configurada. Defina SEI_URL (API REST "  # noqa: EM101
                "mod-wssei) ou SEI_WEB_URL (raiz web, ex: https://sei.orgao.gov.br)."
            )
        resp = await self._http.get(self.login_url)
        if resp.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"GET login.php retornou {resp.status_code}")  # noqa: EM102, TRY003

        html = resp.text
        # Verifica CAPTCHA: busca o elemento HTML real, não o seletor CSS
        # (o CSS inline sempre contém "#txtInfraCaptcha {...}" — falso positivo)
        if (
            "g-recaptcha" in html
            or "h-captcha" in html
            or "hcaptcha" in html
            or 'name="txtInfraCaptcha"' in html
            or 'id="txtInfraCaptcha"' in html
        ):
            raise RuntimeError("CAPTCHA presente no login — abortando.")  # noqa: EM101, TRY003
        if 'name="txtCodigo2FA"' in html or 'id="txtCodigo2FA"' in html:
            raise RuntimeError("2FA solicitado no login — não suportado.")  # noqa: EM101, TRY003

        soup = BeautifulSoup(html, "html.parser")
        usuario_input = soup.find("input", attrs={"name": "txtUsuario"})
        if usuario_input is None:
            raise RuntimeError("Campo txtUsuario não encontrado na página de login.")  # noqa: EM101, TRY003
        login_form = usuario_input.find_parent("form")
        if login_form is None:
            raise RuntimeError("<form> do login não encontrado.")  # noqa: EM101, TRY003

        sel_orgao = self._descobrir_sel_orgao(login_form, soup)

        form: dict[str, str] = {
            "txtUsuario": self._usuario,
            "pwdSenha": self._senha,
            "selOrgao": sel_orgao,
        }
        for h in login_form.find_all("input", type="hidden"):
            name = _tag_str(h, "name")
            if name and h.get("value") is not None:
                form[name] = _tag_str(h, "value")

        # O PHP exige o par name=value do botão submit; sem ele ignora o POST.
        # Detecta o botão real do formulário (varia por instância:
        # sbmLogin=Acessar no ANTAQ, sbmAcessar=ACESSAR no RO, etc.)
        submit_btn = login_form.find("button", type="submit") or login_form.find(
            "input", type="submit"
        )
        if submit_btn and isinstance(submit_btn, Tag):
            btn_name = _tag_str(submit_btn, "name")
            if btn_name:
                btn_value = (
                    _tag_str(submit_btn, "value") or submit_btn.get_text(strip=True) or "Acessar"
                )
                form[btn_name] = btn_value
        else:
            # fallback para instâncias mais antigas
            form["sbmLogin"] = "Acessar"

        # Corrige hdnAcao: o JS seta o valor correto antes de submeter via
        # acaoLogin(N) no onsubmit. Ex: onsubmit="return acaoLogin(2);"
        # O HTML tem value="1" (padrão), mas ação=2 é o login com usuário/senha.
        onsubmit = _tag_str(login_form, "onsubmit")
        m_acao = re.search(r"acaoLogin\((\d+)\)", onsubmit)
        if m_acao and "hdnAcao" in form:
            form["hdnAcao"] = m_acao.group(1)
        sel_ctx = login_form.find("select", attrs={"name": "selContexto"})
        if sel_ctx is not None and isinstance(sel_ctx, Tag):
            ctx_val = ""
            for opt in sel_ctx.find_all("option"):
                if isinstance(opt, Tag) and opt.get("selected") is not None:
                    ctx_val = _tag_str(opt, "value")
                    break
            form["selContexto"] = ctx_val

        action = _tag_str(login_form, "action") or self.login_url
        post_url = urljoin(self.login_url, action)
        post_resp = await self._http.post(
            post_url,
            data=form,
            headers={"Referer": self.login_url, "Origin": self.sei_root},
        )
        if post_resp.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"POST login retornou {post_resp.status_code}")  # noqa: EM102, TRY003

        # após follow_redirects, resp.url é a URL final da cadeia
        # sip/login → sei/inicializar.php → sei/controlador.php?acao=procedimento_controlar
        final_url = post_resp.url
        qs = dict(
            parse_qsl(
                final_url.query.decode() if isinstance(final_url.query, bytes) else final_url.query
            )
        )
        if qs.get("acao") != "procedimento_controlar" or "infra_hash" not in qs:
            body = post_resp.text
            if 'name="txtUsuario"' in body or 'id="txtUsuario"' in body:
                raise RuntimeError(  # noqa: TRY003
                    "Login falhou: o servidor retornou a página de login novamente. "  # noqa: EM101
                    "Verifique credenciais."
                )
            raise RuntimeError(f"URL inesperada após login: {final_url}")  # noqa: EM102, TRY003

        self._inbox_url = final_url
        self._arvore_cache.clear()
        # popula cache do form principal e dos links de processos a partir
        # da própria resposta do post-login (já contém o HTML da inbox)
        self._extract_main_form(post_resp.text)
        self._extract_pesquisa_rapida(post_resp.text)
        self._populate_trabalhar_links(post_resp.text)
        self._extract_unidade_atual(post_resp.text)
        logger.info("SEI web login bem-sucedido — inbox capturada")

    def _descobrir_sel_orgao(self, login_form, soup) -> str:  # noqa: ANN001
        """Descobre o value do <select selOrgao> que corresponde ao órgão.

        Estratégia: option já selecionado → option com texto contendo a sigla
        do órgão → primeiro option não-vazio.
        """
        sel = login_form.find("select", attrs={"name": "selOrgao"})
        if sel is None:
            sel = soup.find("select", attrs={"name": "selOrgao"})
        if sel is None:
            raise RuntimeError("<select name='selOrgao'> não encontrado")  # noqa: EM101, TRY003

        # 1) option já selecionado
        for opt in sel.find_all("option"):
            if opt.get("selected") is not None and opt.get("value") and opt.get("value") != "null":
                return opt["value"]
        # 2) option cujo texto contém a sigla do órgão (ex: ANTAQ)
        sigla_upper = self._sigla_orgao.upper()
        for opt in sel.find_all("option"):
            if (
                sigla_upper in opt.get_text(strip=True).upper()
                and opt.get("value")
                and opt.get("value") != "null"
            ):
                return opt["value"]
        # 3) primeiro option válido
        for opt in sel.find_all("option"):
            v = opt.get("value")
            if v and v != "null":
                return v
        raise RuntimeError("Nenhum <option> válido em selOrgao.")  # noqa: EM101, TRY003

    def _extract_pesquisa_rapida(self, html: str) -> None:
        """Captura a action do form de pesquisa rápida (protocolo_pesquisa_rapida)."""
        soup = BeautifulSoup(html, "html.parser")
        for f in soup.find_all("form"):
            if not isinstance(f, Tag):
                continue
            action = _tag_str(f, "action")
            if "protocolo_pesquisa_rapida" in action:
                self._pesquisa_rapida_action = action.replace("&amp;", "&")
                return

    def _extract_main_form(self, html: str) -> None:
        """Captura action + hidden fields do form principal de procedimento_controlar.

        Esse form tem seu próprio `infra_hash` (diferente da inbox URL) e é
        usado para alternar visualização (resumida↔detalhada) e paginação.
        """
        soup = BeautifulSoup(html, "html.parser")
        for f in soup.find_all("form"):
            if not isinstance(f, Tag):
                continue
            action = _tag_str(f, "action")
            if "procedimento_controlar" in action:
                self._form_action = action.replace("&amp;", "&")
                self._form_hidden = {}
                for h in f.find_all("input", type="hidden"):
                    if not isinstance(h, Tag):
                        continue
                    name = _tag_str(h, "name")
                    if name:
                        self._form_hidden[name] = _tag_str(h, "value")
                return

    def _populate_trabalhar_links(self, inbox_html: str) -> None:
        """Mapeia protocolo → URL pré-assinada de procedimento_trabalhar.

        Sem isso não conseguimos navegar para um processo específico —
        a infra_hash é gerada server-side e não pode ser reconstruída.
        """
        soup = BeautifulSoup(inbox_html, "html.parser")
        for a in soup.find_all("a", href=re.compile(r"acao=procedimento_trabalhar")):
            if not isinstance(a, Tag):
                continue
            txt = a.get_text(strip=True)
            href = _tag_str(a, "href").replace("&amp;", "&")
            if txt and href:
                self._trabalhar_links.setdefault(txt, href)

    def _extract_unidade_atual(self, html: str) -> None:
        """Extrai a unidade ativa do seletor exibido no cabecalho do SEI."""
        soup = BeautifulSoup(html, "html.parser")
        unit_link = soup.find(
            "a",
            id=re.compile(r"unidade", re.IGNORECASE),
            title=True,
        )
        if not isinstance(unit_link, Tag):
            return

        sigla = unit_link.get_text(" ", strip=True)
        nome = _tag_str(unit_link, "title").strip()
        if not sigla and not nome:
            return

        unidade: dict[str, str] = {"sigla": sigla, "nome": nome}
        if self._inbox_url is not None:
            query = dict(parse_qsl(str(self._inbox_url.query)))
            id_unidade = query.get("infra_unidade_atual", "")
            if id_unidade:
                unidade["id_unidade"] = id_unidade
        self._unidade_atual = unidade

    async def unidade_atual(self) -> dict[str, str]:
        """Retorna id, sigla e nome da unidade ativa na sessao web."""
        await self.ensure_authenticated()
        if self._unidade_atual is None:
            _, html = await self.fetch_inbox(detalhada=False)
            self._extract_unidade_atual(html)
        if self._unidade_atual is None:
            msg = "Nao foi possivel identificar a unidade ativa na pagina do SEI."
            raise RuntimeError(msg)
        return dict(self._unidade_atual)

    async def _fetch_unit_switch_form(self) -> tuple[str, Tag]:
        """Abre a tela de troca de unidade e retorna URL e formulario."""
        await self.ensure_authenticated()
        _, html = await self.fetch_inbox(detalhada=False)
        soup = BeautifulSoup(html, "html.parser")
        unit_link = soup.find("a", id="lnkInfraUnidade")
        if not isinstance(unit_link, Tag):
            msg = "Link de troca de unidade nao encontrado."
            raise TypeError(msg)

        onclick = _tag_str(unit_link, "onclick")
        match = re.search(r"window\.location\.href='([^']+)'", onclick)
        if not match:
            msg = "URL de troca de unidade nao encontrada."
            raise RuntimeError(msg)

        switch_url = urljoin(str(self._inbox_url), match.group(1))
        response = await self._http.get(switch_url, headers={"Referer": str(self._inbox_url)})
        if response.status_code != 200:  # noqa: PLR2004
            msg = f"Tela de troca de unidade retornou {response.status_code}."
            raise RuntimeError(msg)

        switch_soup = BeautifulSoup(response.text, "html.parser")
        form = switch_soup.find("form", id="frmInfraSelecaoUnidade")
        if not isinstance(form, Tag):
            msg = "Formulario de troca de unidade nao encontrado."
            raise TypeError(msg)
        return str(response.url), form

    async def listar_unidades(self) -> list[dict[str, str]]:
        """Lista unidades acessiveis ao usuario pela tela web de troca."""
        _, form = await self._fetch_unit_switch_form()
        units: list[dict[str, str]] = []
        for radio in form.find_all("input", attrs={"name": "chkInfraItem"}):
            if not isinstance(radio, Tag):
                continue
            id_unidade = _tag_str(radio, "value")
            row = radio.find_parent("tr")
            if not id_unidade or not isinstance(row, Tag):
                continue
            cells = [" ".join(td.get_text(" ", strip=True).split()) for td in row.find_all("td")]
            values = [cell for cell in cells if cell]
            if len(values) < 2:  # noqa: PLR2004
                continue
            units.append(
                {
                    "id_unidade": id_unidade,
                    "sigla": values[0],
                    "nome": values[1],
                }
            )
        return units

    async def trocar_unidade(self, referencia: str) -> dict[str, str]:
        """Troca a unidade ativa por ID ou sigla usando a interface web."""
        units = await self.listar_unidades()
        ref = referencia.strip().casefold()
        matches = [
            unit
            for unit in units
            if unit["id_unidade"].casefold() == ref or unit["sigla"].casefold() == ref
        ]
        if not matches:
            msg = f"Unidade {referencia!r} nao encontrada entre as unidades acessiveis."
            raise RuntimeError(msg)

        target = matches[0]
        form_url, form = await self._fetch_unit_switch_form()
        action = _tag_str(form, "action")
        post_url = urljoin(form_url, action)
        data: dict[str, str] = {}
        for field in form.find_all("input"):
            if not isinstance(field, Tag):
                continue
            name = _tag_str(field, "name")
            field_type = _tag_str(field, "type").lower()
            if name and field_type == "hidden":
                data[name] = _tag_str(field, "value")
        data["selInfraUnidades"] = target["id_unidade"]

        response = await self._http.post(post_url, data=data, headers={"Referer": form_url})
        if response.status_code != 200:  # noqa: PLR2004
            msg = f"Troca de unidade retornou {response.status_code}."
            raise RuntimeError(msg)

        self._inbox_url = response.url
        self._form_action = None
        self._form_hidden = {}
        self._trabalhar_links.clear()
        self._pesquisa_rapida_action = None
        self._arvore_cache.clear()
        self._unidade_atual = None
        self._extract_main_form(response.text)
        self._extract_pesquisa_rapida(response.text)
        self._populate_trabalhar_links(response.text)
        self._extract_unidade_atual(response.text)

        current = await self.unidade_atual()
        if current.get("id_unidade") != target["id_unidade"]:
            msg = f"SEI nao confirmou a troca para {target['sigla']}."
            raise RuntimeError(msg)
        return current

    # ------------------------------------------------------------------
    # Listar processos (Controle de Processos / inbox)
    # ------------------------------------------------------------------

    async def fetch_inbox(
        self,
        detalhada: bool = True,  # noqa: FBT001, FBT002
        pagina: int = 0,
        apenas_meus: bool = False,  # noqa: FBT001, FBT002
    ) -> tuple[int, str]:
        """Busca o HTML da página de Controle de Processos.

        - `detalhada=True`: força a visualização Detalhada via POST
          `hdnTipoVisualizacao=D`. A primeira chamada precisa de um GET prévio
          para descobrir o form action; chamadas subsequentes reaproveitam o cache.
        - `pagina=N>0`: POST com `hdnInfraPaginaAtual=N` + `hdnInfraHashCriterios`
          (cacheado da resposta anterior).
        - `apenas_meus=True`: POST `hdnMeusProcessos=M` (TA_MINHAS) — retorna
          apenas processos atribuídos ao usuário logado. Sempre passa o valor
          explicitamente (T ou M) para não herdar de chamadas anteriores.

        Retorna `(bytes, html)`.
        """
        await self.ensure_authenticated()
        inbox_url = str(self._inbox_url)

        # Caso simples: GET inicial sem detalhada/filtros/paginação
        if not detalhada and pagina == 0 and not apenas_meus and self._form_action is None:
            resp = await self._http.get(
                inbox_url,
                headers={"Referer": inbox_url},
            )
            if resp.status_code != 200:  # noqa: PLR2004
                raise RuntimeError(f"fetch_inbox status={resp.status_code}")  # noqa: EM102, TRY003
            self._extract_main_form(resp.text)
            self._populate_trabalhar_links(resp.text)
            self._extract_unidade_atual(resp.text)
            return len(resp.content), resp.text

        # Precisa do form action — fetch inicial se ainda não temos
        if self._form_action is None:
            seed = await self._http.get(
                inbox_url,
                headers={"Referer": inbox_url},
            )
            if seed.status_code != 200:  # noqa: PLR2004
                raise RuntimeError(f"seed inbox status={seed.status_code}")  # noqa: EM102, TRY003
            self._extract_main_form(seed.text)
            if self._form_action is None:
                raise RuntimeError("Form principal de procedimento_controlar não encontrado")  # noqa: EM101, TRY003

        # POST para alternar visualização / aplicar filtros / navegar páginas
        post_data = dict(self._form_hidden)
        if detalhada:
            post_data["hdnTipoVisualizacao"] = "D"
        # apenas_meus: sempre seta explicitamente (M ou T) para não herdar
        # estado de chamadas anteriores. Valores em AtividadeRN.php:
        # T=TODAS, M=MINHAS, D=DEFINIDAS, E=ESPECIFICAS.
        post_data["hdnMeusProcessos"] = "M" if apenas_meus else "T"
        if pagina > 0:
            post_data["hdnInfraPaginaAtual"] = str(pagina)

        post_url = urljoin(str(self._inbox_url), self._form_action)
        resp = await self._http.post(
            post_url,
            data=post_data,
            headers={"Referer": str(self._inbox_url)},
        )
        if resp.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"fetch_inbox POST status={resp.status_code}")  # noqa: EM102, TRY003

        # detecta sessão expirada
        body = resp.text
        if 'name="txtUsuario"' in body or 'id="txtUsuario"' in body:
            logger.info("Sessão SEI expirou, re-logando")
            self._form_action = None
            self._form_hidden = {}
            await self.login()
            return await self.fetch_inbox(
                detalhada=detalhada, pagina=pagina, apenas_meus=apenas_meus
            )

        # atualiza cache do form (action e hashCriterios podem mudar entre páginas)
        self._extract_main_form(body)
        self._extract_pesquisa_rapida(body)
        self._populate_trabalhar_links(body)
        self._extract_unidade_atual(body)
        return len(resp.content), body

    # ------------------------------------------------------------------
    # Consultar processo (página de detalhe)
    # ------------------------------------------------------------------

    async def pesquisar_processo(self, protocolo: str) -> None:
        """Busca um processo pelo protocolo via pesquisa rápida do SEI.

        Popula `_trabalhar_links` com a URL pré-assinada do processo encontrado,
        permitindo navegação posterior mesmo para processos fora da caixa atual.

        Raises RuntimeError se o processo não for encontrado.
        """
        await self.ensure_authenticated()

        if self._pesquisa_rapida_action is None:
            await self.fetch_inbox(detalhada=False)
            if self._pesquisa_rapida_action is None:
                raise RuntimeError("Form de pesquisa rápida não encontrado no HTML da inbox")  # noqa: EM101, TRY003

        post_url = urljoin(str(self._inbox_url), self._pesquisa_rapida_action)
        r = await self._http.post(
            post_url,
            data={"txtPesquisaRapida": protocolo},
            headers={"Referer": str(self._inbox_url)},
        )
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"pesquisa_rapida status={r.status_code}")  # noqa: EM102, TRY003

        final_url = str(r.url)
        sei_base = f"{self.sei_root}/sei/"

        if "procedimento_trabalhar" in final_url:
            # Redirecionou direto para o processo
            href = final_url.replace(sei_base, "") if final_url.startswith(sei_base) else final_url
            self._trabalhar_links[protocolo] = href
            return

        # Página de resultados (protocolo_pesquisar) — busca o link correto
        soup = BeautifulSoup(r.text, "html.parser")
        proto_norm = protocolo.replace(" ", "")
        for a in soup.find_all("a", href=re.compile(r"procedimento_trabalhar")):
            txt = a.get_text(strip=True).replace(" ", "")
            if proto_norm in txt:
                href = _tag_str(a, "href").replace("&amp;", "&")
                self._trabalhar_links[protocolo] = href
                return

        # Tenta também via links com id_procedimento (tooltip ou linha da tabela)
        for a in soup.find_all("a", href=re.compile(r"procedimento_trabalhar")):
            href = _tag_str(a, "href").replace("&amp;", "&")
            self._trabalhar_links[protocolo] = href
            return

        raise RuntimeError(  # noqa: TRY003
            f"Processo {protocolo!r} não encontrado na pesquisa. "  # noqa: EM102
            "Verifique se o número está correto e se você tem acesso."
        )

    async def consultar_processo(self, protocolo_formatado: str) -> dict:  # noqa: C901, PLR0912, PLR0915
        """Busca dados de um processo navegando pela cadeia de páginas web.

        Fluxo:
        1. Garante que o protocolo está no cache `_trabalhar_links` (links
           pré-assinados extraídos da inbox). Se não, faz fetch_inbox uma vez
           para popular.
        2. GET procedimento_trabalhar.php (frameset, ~70 ms) — confirma o
           id_procedimento e captura a URL assinada do iframe da árvore.
        3. GET procedimento_visualizar / arvore_montar.php (~1 s) — extrai o
           array Nos[] do JS e popula a lista de documentos.
        4. Se houver PASTA colapsadas, faz GET com abrir_pastas=1 para expandir
           todos os processos relacionados.

        Retorna:
            {
              "id_procedimento": str,
              "protocolo": str,
              "tipo": str,           # da tooltip do nó raiz
              "documentos": [{id, label, tipo_no, link}, ...],
              "total_documentos": int,
              "relacionados": [str, ...],
            }

        Raises se o protocolo não for encontrado nos links da inbox.
        Para enriquecer com especificacao/assuntos/interessados (que só estão
        na REST), combine com `SEIClient.consultar_processo_completo()`.
        """
        await self.ensure_authenticated()

        # garante que o protocolo está no cache de links da inbox
        if protocolo_formatado not in self._trabalhar_links:
            await self.fetch_inbox(detalhada=False)
        if protocolo_formatado not in self._trabalhar_links:
            # processo fora da caixa — usa pesquisa rápida
            await self.pesquisar_processo(protocolo_formatado)

        trab_url = urljoin(str(self._inbox_url), self._trabalhar_links[protocolo_formatado])

        # Step 1: procedimento_trabalhar.php (frameset, leve)
        r1 = await self._http.get(trab_url, headers={"Referer": str(self._inbox_url)})
        if r1.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"procedimento_trabalhar status={r1.status_code}")  # noqa: EM102, TRY003

        # detecta sessão expirada
        if 'name="txtUsuario"' in r1.text or 'id="txtUsuario"' in r1.text:
            logger.info("Sessão SEI expirou, re-logando")
            self._form_action = None
            self._form_hidden = {}
            await self.login()
            return await self.consultar_processo(protocolo_formatado)

        soup_fs = BeautifulSoup(r1.text, "html.parser")
        ifr = soup_fs.find("iframe", id="ifrArvore")
        if ifr is None:
            raise RuntimeError("ifrArvore não encontrado no frameset")  # noqa: EM101, TRY003
        arvore_src = _tag_str(ifr, "src").replace("&amp;", "&")
        arvore_url = urljoin(str(r1.url), arvore_src)

        # extrai id_procedimento da URL do trabalhar
        m_id = re.search(r"id_procedimento=(\d+)", str(r1.url))
        id_proc = m_id.group(1) if m_id else None

        # Step 2: procedimento_visualizar (arvore_montar.php)
        r2 = await self._http.get(arvore_url, headers={"Referer": trab_url})
        if r2.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"procedimento_visualizar status={r2.status_code}")  # noqa: EM102, TRY003

        nos = parse_arvore_nos(r2.text)
        arvore_html = r2.text

        # Step 3: Se houver PASTA colapsadas, fetch novamente com abrir_pastas=1
        has_collapsed = len(nos) > 1 and any(n.get("tipo_no") == "PASTA" for n in nos[1:])
        if has_collapsed:
            arvore_url_str = str(arvore_url)
            if "abrir_pastas=" not in arvore_url_str:
                sep = "&" if "?" in arvore_url_str else "?"
                arvore_url_expandida = f"{arvore_url_str}{sep}abrir_pastas=1"
            else:
                arvore_url_expandida = re.sub(r"abrir_pastas=0", "abrir_pastas=1", arvore_url_str)

            r3 = await self._http.get(arvore_url_expandida, headers={"Referer": trab_url})
            if r3.status_code == 200:  # noqa: PLR2004
                nos = parse_arvore_nos(r3.text)
                arvore_html = r3.text
                logger.debug("Pastas expandidas via abrir_pastas=1")

        result: dict[str, Any] = {
            "id_procedimento": id_proc or "",
            "protocolo": protocolo_formatado,
        }
        if nos:
            root = nos[0]
            result["tipo"] = root.get("tooltip", "")
            result["icone"] = root.get("icone", "")
            # documentos = todos os Nos exceto o root e exceto PASTA
            docs = [
                {
                    "id": n["id"],
                    "label": n.get("label", ""),
                    "tipo_no": n.get("tipo_no", ""),
                    "link": n.get("link", ""),
                }
                for n in nos[1:]
                if n.get("tipo_no") not in ("PASTA",)  # noqa: FURB171
            ]
            result["documentos"] = docs
            result["total_documentos"] = len(docs)

        # processos relacionados (cards na sidebar do arvore_montar)
        soup_arv = BeautifulSoup(arvore_html, "html.parser")
        rels: list[str] = []
        for div_rel in soup_arv.find_all("div", class_=re.compile(r"cardRelacionado")):
            link_rel = div_rel.find("a")
            if link_rel:
                rels.append(link_rel.get_text(strip=True))
        if rels:
            result["relacionados"] = rels

        return result

    async def listar_documentos(self, protocolo_formatado: str) -> dict:
        """Lista documentos de um processo via web scraper (arvore_montar).

        Chama `consultar_processo()` internamente e parseia os labels dos nós
        para extrair tipo do documento, sigla da unidade e número SEI.

        Retorna:
            {
              "processo": {"protocolo": str, "id_procedimento": str, "tipo": str},
              "total_documentos": int,
              "documentos": [{ordem, id, nome_composto, tipo_documento, sigla_unidade,
                              numero_sei, tipo_no, icone}, ...],
            }

        ~10× mais rápido que a REST /documento/listar (9.7 s → ~1 s).
        """
        proc = await self.consultar_processo(protocolo_formatado)

        docs_raw = proc.get("documentos", [])
        docs = []
        for i, d in enumerate(docs_raw):
            label = d.get("label", "")
            parsed = _parse_doc_label(label)
            docs.append(
                {
                    "ordem": i + 1,
                    "id": d["id"],
                    "nome_composto": label,
                    **parsed,
                    "tipo_no": d.get("tipo_no", ""),
                    "icone": d.get("icone", ""),
                }
            )

        return {
            "processo": {
                "protocolo": protocolo_formatado,
                "id_procedimento": proc.get("id_procedimento", ""),
                "tipo": proc.get("tipo", ""),
            },
            "total_documentos": len(docs),
            "documentos": docs,
        }

    # ------------------------------------------------------------------
    # Ações genéricas em processos
    # ------------------------------------------------------------------

    async def _garantir_link_trabalhar(self, protocolo: str) -> str:
        """Garante que _trabalhar_links[protocolo] existe e retorna o href."""
        if protocolo not in self._trabalhar_links:
            await self.fetch_inbox(detalhada=False)
        if protocolo not in self._trabalhar_links:
            await self.pesquisar_processo(protocolo)
        href = self._trabalhar_links.get(protocolo)
        if not href:
            raise RuntimeError(f"Processo {protocolo!r} não encontrado")  # noqa: EM102, TRY003
        return href

    async def _arvore_do_processo(self, protocolo: str) -> tuple[str, str]:
        """Navega trabalhar→frameset→arvore; retorna (html_arvore, url_arvore).

        Resultado cacheado por _ARVORE_CACHE_TTL segundos; ações que alteram
        o processo invalidam a entrada via _invalidar_arvore().
        """
        em_cache = self._arvore_cache.get(protocolo)
        if em_cache is not None:
            ts, resultado = em_cache
            if time.monotonic() - ts <= _ARVORE_CACHE_TTL:
                return resultado
            del self._arvore_cache[protocolo]

        href = await self._garantir_link_trabalhar(protocolo)
        trab_url = urljoin(str(self._inbox_url), href)

        r1 = await self._http.get(trab_url, headers={"Referer": str(self._inbox_url)})
        if r1.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"trabalhar status={r1.status_code}")  # noqa: EM102, TRY003
        if 'name="txtUsuario"' in r1.text or 'id="txtUsuario"' in r1.text:
            self._form_action = None
            self._form_hidden = {}
            self._trabalhar_links.pop(protocolo, None)
            await self.login()
            return await self._arvore_do_processo(protocolo)

        soup_fs = BeautifulSoup(r1.text, "html.parser")
        ifr = soup_fs.find("iframe", id="ifrArvore")
        if not isinstance(ifr, Tag):
            raise RuntimeError("ifrArvore não encontrado no frameset")  # noqa: EM101, TRY003, TRY004
        arvore_url = urljoin(str(r1.url), _tag_str(ifr, "src").replace("&amp;", "&"))

        r2 = await self._http.get(arvore_url, headers={"Referer": trab_url})
        if r2.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"arvore status={r2.status_code}")  # noqa: EM102, TRY003
        resultado = (r2.text, str(r2.url))
        self._arvore_cache[protocolo] = (time.monotonic(), resultado)
        return resultado

    def _invalidar_arvore(self, protocolo: str) -> None:
        """Remove a árvore cacheada de um processo (após ação que a altera)."""
        self._arvore_cache.pop(protocolo, None)

    async def executar_acao_processo(  # noqa: C901
        self,
        protocolo: str,
        nome_acao: str,
        campos_extras: dict[str, str] | None = None,
    ) -> dict:
        """Executa uma ação simples em um processo via scraper web do SEI.

        Fluxo: trabalhar → arvore_montar → link(acao=nome_acao) → GET [→ POST form]

        Parâmetros:
        - protocolo: número SEI formatado (ex: "50300.018905/2018-67")
        - nome_acao: nome da ação no controlador (ex: "procedimento_concluir")
        - campos_extras: campos adicionais para o POST do form de confirmação

        Retorna dict com {"ok": True, "mensagem": str} ou levanta RuntimeError.
        """
        await self.ensure_authenticated()

        html_arvore, url_arvore = await self._arvore_do_processo(protocolo)
        sei_base = f"{self.sei_root}/sei/"

        m = re.search(
            rf"(controlador\.php\?acao={re.escape(nome_acao)}[^\"'\s]*infra_hash=[a-f0-9]+)",
            html_arvore,
        )
        if not m:
            raise RuntimeError(  # noqa: TRY003
                f"Ação '{nome_acao}' não encontrada no menu do processo. "  # noqa: EM102
                "Verifique se você tem permissão para esta ação e se o "
                "processo está no estado correto."
            )

        acao_url = urljoin(sei_base, m.group(1).replace("&amp;", "&"))
        r = await self._http.get(acao_url, headers={"Referer": url_arvore})
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"GET {nome_acao} status={r.status_code}")  # noqa: EM102, TRY003

        body = r.content.decode("iso-8859-1", "replace")
        erro = _extrair_erro_sei(body)
        if erro:
            raise RuntimeError(erro)

        soup = BeautifulSoup(body, "html.parser")
        form = soup.find("form")
        if form and isinstance(form, Tag):
            action = _tag_str(form, "action").replace("&amp;", "&")
            post_url = urljoin(str(r.url), action) if action else str(r.url)
            post_data: dict[str, str] = {}
            for inp in form.find_all("input"):
                if not isinstance(inp, Tag):
                    continue
                n = _tag_str(inp, "name")
                if n:
                    post_data[n] = _tag_str(inp, "value")
            if campos_extras:
                post_data.update(campos_extras)
            r2 = await self._http.post(post_url, data=post_data, headers={"Referer": str(r.url)})
            if r2.status_code != 200:  # noqa: PLR2004
                raise RuntimeError(f"POST {nome_acao} status={r2.status_code}")  # noqa: EM102, TRY003
            body2 = r2.content.decode("iso-8859-1", "replace")
            erro2 = _extrair_erro_sei(body2)
            if erro2:
                raise RuntimeError(erro2)
        else:
            # Sem form: pode ser ação que executa direto via GET (ex: redirect imediato).
            # Valida que não há erro oculto e loga para facilitar debug.
            if _extrair_erro_sei(body):  # já checado acima mas re-verifica body completo
                raise RuntimeError(f"Ação '{nome_acao}' falhou sem form de confirmação.")  # noqa: EM102, TRY003
            logger.debug(
                "executar_acao_processo: ação '%s' concluída via GET (sem form)", nome_acao
            )

        self._invalidar_arvore(protocolo)
        return {
            "ok": True,
            "mensagem": f"Ação '{nome_acao}' executada com sucesso.",
            "protocolo": protocolo,
        }

    async def obter_form_acao(  # noqa: C901, PLR0912
        self,
        protocolo: str,
        nome_acao: str,
    ) -> dict:
        """Retorna os campos e opções disponíveis no form de uma ação.

        Útil para descobrir os IDs válidos de selects (ex: selUsuario, selMarcador)
        antes de submeter o form com executar_acao_processo.

        Retorna dict com:
        - "campos": {name: value} dos hidden inputs pré-preenchidos
        - "selects": {name: [{value, texto}, ...]} dos campos select
        - "textareas": [name, ...] dos campos de texto livre
        """
        await self.ensure_authenticated()

        html_arvore, url_arvore = await self._arvore_do_processo(protocolo)
        sei_base = f"{self.sei_root}/sei/"

        m = re.search(
            rf"(controlador\.php\?acao={re.escape(nome_acao)}[^\"'\s]*infra_hash=[a-f0-9]+)",
            html_arvore,
        )
        if not m:
            raise RuntimeError(  # noqa: TRY003
                f"Ação '{nome_acao}' não encontrada no menu do processo."  # noqa: EM102
            )

        acao_url = urljoin(sei_base, m.group(1).replace("&amp;", "&"))
        r = await self._http.get(acao_url, headers={"Referer": url_arvore})
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"GET {nome_acao} status={r.status_code}")  # noqa: EM102, TRY003

        body = r.content.decode("iso-8859-1", "replace")
        soup = BeautifulSoup(body, "html.parser")
        form = soup.find("form")
        if not isinstance(form, Tag):
            return {"campos": {}, "selects": {}, "textareas": []}

        campos: dict[str, str] = {}
        for inp in form.find_all("input", type="hidden"):
            if not isinstance(inp, Tag):
                continue
            n = _tag_str(inp, "name")
            if n:
                campos[n] = _tag_str(inp, "value")

        selects: dict[str, list[dict]] = {}
        for sel in form.find_all("select"):
            if not isinstance(sel, Tag):
                continue
            n = _tag_str(sel, "name")
            if not n:
                continue
            opcoes = []
            for opt in sel.find_all("option"):
                if not isinstance(opt, Tag):
                    continue
                v = _tag_str(opt, "value")
                t = opt.get_text(strip=True)
                if v:
                    opcoes.append({"value": v, "texto": t})
            selects[n] = opcoes

        textareas = []
        for ta in form.find_all("textarea"):
            if not isinstance(ta, Tag):
                continue
            n = _tag_str(ta, "name")
            if n:
                textareas.append(n)

        return {"campos": campos, "selects": selects, "textareas": textareas}

    # ------------------------------------------------------------------
    # Read scrapers — PR #4
    # ------------------------------------------------------------------

    async def _get_doc_signed_url(
        self, protocolo: str, id_documento: str, acao: str
    ) -> tuple[str, str]:
        """Retorna (signed_url, arvore_url) para uma ação de documento.

        Aceita tanto o id interno (id do nó da árvore) quanto o número SEI
        (extraído do label do nó, ex: "Despacho GPF 2874369") — web-only não
        tem Solr para resolver. Para `documento_consultar` usa Nos[].link;
        para outras ações busca a URL assinada por regex com o id resolvido.
        """
        html_arvore, url_arvore = await self._arvore_do_processo(protocolo)
        sei_base = f"{self.sei_root}/sei/"

        # Resolve a referência para o nó da árvore: por id interno, depois
        # por número SEI no label
        nos = parse_arvore_nos(html_arvore)
        no_alvo: dict | None = None
        for no in nos[1:]:
            if no.get("id") == id_documento:
                no_alvo = no
                break
        if no_alvo is None:
            for no in nos[1:]:
                if _parse_doc_label(no.get("label", "")).get("numero_sei") == id_documento:
                    no_alvo = no
                    break
        id_interno = str(no_alvo["id"]) if no_alvo else id_documento

        # Para documento_consultar, o link está em Nos[].link
        if acao == "documento_consultar" and no_alvo and no_alvo.get("link"):
            raw = str(no_alvo["link"]).replace("&amp;", "&")
            return urljoin(sei_base, raw), url_arvore

        # Busca genérica: qualquer URL com acao=X e id_documento=Y
        # (?=&|&amp;|["'\s]) âncora o fim do id para evitar match por prefixo
        # (ex: id=287 não deve casar com id=2874369)
        _id_anchor = r"(?=&(?:amp;)?|[\"'\s])"
        pattern = (
            rf"(controlador\.php\?acao={re.escape(acao)}"
            rf"[^\"'\s]*id_documento={re.escape(id_interno)}{_id_anchor}"
            rf"[^\"'\s]*infra_hash=[a-fA-F0-9]+)"
        )
        m = re.search(pattern, html_arvore)
        if not m:
            # Tenta ordem invertida (infra_hash antes de id_documento)
            pattern2 = (
                rf"(controlador\.php\?acao={re.escape(acao)}"
                rf"[^\"'\s]*infra_hash=[a-fA-F0-9]+"
                rf"[^\"'\s]*id_documento={re.escape(id_interno)}{_id_anchor}"
                rf"[^\"'\s]*)"
            )
            m = re.search(pattern2, html_arvore)
        if not m:
            raise RuntimeError(  # noqa: TRY003
                f"Ação '{acao}' não encontrada para o documento {id_documento} "  # noqa: EM102
                f"na árvore do processo {protocolo}."
            )
        return urljoin(sei_base, m.group(1).replace("&amp;", "&")), url_arvore

    async def consultar_documento_web(self, protocolo: str, id_documento: str) -> dict:
        """Scrape dos metadados de documento_consultar (tipo, data, assinaturas, etc.)."""
        await self.ensure_authenticated()
        url, referer = await self._get_doc_signed_url(
            protocolo, id_documento, "documento_consultar"
        )
        r = await self._http.get(url, headers={"Referer": referer})
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"documento_consultar status={r.status_code}")  # noqa: EM102, TRY003
        html = r.content.decode("iso-8859-1", "replace")
        erro = _extrair_erro_sei(html)
        if erro:
            # SEI retorna 200 com página de erro (sessão expirada, sem permissão)
            raise RuntimeError(f"documento_consultar: {erro}")  # noqa: EM102, TRY003
        return _parse_documento_consultar(html, id_documento)

    async def listar_assinaturas_web(self, protocolo: str, id_documento: str) -> list[dict]:
        """Lista assinaturas de um documento via scrape de documento_consultar."""
        data = await self.consultar_documento_web(protocolo, id_documento)
        return data.get("assinaturas", [])  # type: ignore[return-value]

    async def listar_ciencias_web(self, protocolo: str, id_documento: str) -> list[dict]:
        """Lista ciências de um documento via scrape de documento_consultar."""
        data = await self.consultar_documento_web(protocolo, id_documento)
        return data.get("ciencias", [])  # type: ignore[return-value]

    async def visualizar_documento_interno_web(self, protocolo: str, id_documento: str) -> str:
        """Retorna HTML de um documento interno via documento_visualizar."""
        await self.ensure_authenticated()
        url, referer = await self._get_doc_signed_url(
            protocolo, id_documento, "documento_visualizar"
        )
        r = await self._http.get(url, headers={"Referer": referer})
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"documento_visualizar status={r.status_code}")  # noqa: EM102, TRY003
        html = r.content.decode("iso-8859-1", "replace")
        erro = _extrair_erro_sei(html)
        if erro:
            # SEI retorna 200 com página de erro; sem este check o erro seria
            # devolvido como se fosse o conteúdo do documento (e quebraria a
            # auto-detecção interno→externo de sei_ler_documento)
            raise RuntimeError(f"documento_visualizar: {erro}")  # noqa: EM102, TRY003
        return html

    async def baixar_documento_externo_web(self, protocolo: str, id_documento: str) -> bytes:
        """Baixa bytes de um documento externo via documento_download_anexo."""
        await self.ensure_authenticated()
        url, referer = await self._get_doc_signed_url(
            protocolo, id_documento, "documento_download_anexo"
        )
        r = await self._http.get(url, headers={"Referer": referer})
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"documento_download_anexo status={r.status_code}")  # noqa: EM102, TRY003
        if "text/html" in r.headers.get("content-type", "").lower():
            # Anexo não chega como text/html: é página de erro com status 200
            erro = _extrair_erro_sei(r.content.decode("iso-8859-1", "replace"))
            raise RuntimeError(  # noqa: TRY003
                f"documento_download_anexo: {erro or 'resposta HTML inesperada'}"  # noqa: EM102
            )
        return r.content

    async def consultar_processo_detalhe(self, protocolo: str) -> dict:
        """Scrape de procedimento_consultar: unidades, interessados, sobrestamento.

        Navega trabalhar → arvore → link procedimento_consultar → parse tabelas.
        """
        await self.ensure_authenticated()

        html_arvore, url_arvore = await self._arvore_do_processo(protocolo)
        sei_base = f"{self.sei_root}/sei/"

        m = re.search(
            r"(controlador\.php\?acao=procedimento_consultar[^\"'\s]*infra_hash=[a-f0-9]+)",
            html_arvore,
        )
        if not m:
            raise RuntimeError(  # noqa: TRY003
                f"Link procedimento_consultar não encontrado na árvore de {protocolo}."  # noqa: EM102
            )
        consultar_url = urljoin(sei_base, m.group(1).replace("&amp;", "&"))

        r = await self._http.get(consultar_url, headers={"Referer": url_arvore})
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"procedimento_consultar status={r.status_code}")  # noqa: EM102, TRY003

        html = r.content.decode("iso-8859-1", "replace")
        erro = _extrair_erro_sei(html)
        if erro:
            raise RuntimeError(f"procedimento_consultar: {erro}")  # noqa: EM102, TRY003
        return _parse_procedimento_consultar(html, protocolo)

    async def _gerar_arquivo_processo(self, protocolo_formatado: str, acao: str) -> bytes:  # noqa: C901, PLR0912, PLR0915
        """Helper compartilhado para gerar_pdf_processo e gerar_zip_processo.

        Fluxo de 5 etapas (igual para PDF e ZIP):
        1. procedimento_trabalhar → frameset com ifrArvore
        2. arvore_montar → busca link da ação (procedimento_gerar_pdf/zip)
        3. GET form de opções
        4. POST com hdnFlagGerar=1 → HTML com ifrDownload.src
        5. GET exibir_arquivo → bytes do arquivo
        """  # noqa: D401

        def _find_link(proto: str) -> str | None:
            if proto in self._trabalhar_links:
                return self._trabalhar_links[proto]
            proto_norm = proto.replace(" ", "")
            for k, v in self._trabalhar_links.items():
                if k.replace(" ", "") == proto_norm:
                    return v
            return None

        if _find_link(protocolo_formatado) is None:
            await self.fetch_inbox(detalhada=False)
        if _find_link(protocolo_formatado) is None:
            await self.pesquisar_processo(protocolo_formatado)

        trab_href = _find_link(protocolo_formatado)
        trab_url = urljoin(str(self._inbox_url), trab_href)

        r1 = await self._http.get(trab_url, headers={"Referer": str(self._inbox_url)})
        if r1.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"trabalhar status={r1.status_code}")  # noqa: EM102, TRY003

        if 'name="txtUsuario"' in r1.text or 'id="txtUsuario"' in r1.text:
            self._form_action = None
            self._form_hidden = {}
            await self.login()
            return await self._gerar_arquivo_processo(protocolo_formatado, acao)

        soup_fs = BeautifulSoup(r1.text, "html.parser")
        ifr = soup_fs.find("iframe", id="ifrArvore")
        if not ifr:
            raise RuntimeError("ifrArvore não encontrado no frameset")  # noqa: EM101, TRY003
        arvore_url = urljoin(str(r1.url), _tag_str(ifr, "src").replace("&amp;", "&"))

        r2 = await self._http.get(arvore_url, headers={"Referer": trab_url})
        if r2.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"arvore status={r2.status_code}")  # noqa: EM102, TRY003

        m_link = re.search(
            rf"(controlador\.php\?acao={re.escape(acao)}[^\"'\s]*infra_hash=[a-f0-9]+)",
            r2.text,
        )
        if not m_link:
            raise RuntimeError(f"Link {acao} não encontrado na árvore")  # noqa: EM102, TRY003

        sei_base = f"{self.sei_root}/sei/"
        form_url = urljoin(sei_base, m_link.group(1).replace("&amp;", "&"))

        r3 = await self._http.get(form_url, headers={"Referer": str(r2.url)})
        if r3.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"form {acao} status={r3.status_code}")  # noqa: EM102, TRY003

        soup3 = BeautifulSoup(r3.content.decode("iso-8859-1", "replace"), "html.parser")
        form = soup3.find("form", id=re.compile(r"frmProcedimento(Pdf|Zip)", re.I))  # noqa: FURB167
        if not form:
            raise RuntimeError("Formulário frmProcedimento(Pdf|Zip) não encontrado")  # noqa: EM101, TRY003
        form_action = _tag_str(form, "action").replace("&amp;", "&")
        post_url = urljoin(str(r3.url), form_action)

        post_data: dict[str, str] = {}
        for inp in form.find_all("input"):
            if not isinstance(inp, Tag):
                continue
            name = _tag_str(inp, "name")
            if name:
                post_data[name] = _tag_str(inp, "value")
        post_data["rdoTipo"] = "T"
        post_data["hdnFlagGerar"] = "1"

        r4 = await self._http.post(
            post_url,
            data=post_data,
            headers={"Referer": str(r3.url)},
            timeout=httpx.Timeout(180.0, connect=10.0),
        )
        if r4.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"POST {acao} status={r4.status_code}")  # noqa: EM102, TRY003

        body4 = r4.content.decode("iso-8859-1", "replace")
        m_dl = re.search(
            r"getElementById\(['\"]ifrDownload['\"]\)\.src\s*=\s*'([^']+)'",
            body4,
        )
        if not m_dl:
            raise RuntimeError(  # noqa: TRY003
                f"URL de download (ifrDownload.src) não encontrada após {acao}. "  # noqa: EM102
                "O processo pode não ter documentos disponíveis."
            )

        download_url = urljoin(sei_base, m_dl.group(1).replace("&amp;", "&"))

        r5 = await self._http.get(download_url, headers={"Referer": str(r4.url)})
        if r5.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"download {acao} status={r5.status_code}")  # noqa: EM102, TRY003

        return r5.content

    async def gerar_pdf_processo(self, protocolo_formatado: str) -> bytes:
        """Gera e baixa o PDF consolidado de um processo SEI.

        Usa o mesmo endpoint do botão "Gerar PDF" da interface web.
        Retorna os bytes brutos do PDF.
        """
        await self.ensure_authenticated()
        content = await self._gerar_arquivo_processo(protocolo_formatado, "procedimento_gerar_pdf")
        if "pdf" not in self._http.headers.get("accept", "").lower():
            pass  # conteúdo válido independente do accept
        if not content.startswith(b"%PDF") and b"pdf" not in content[:32].lower():
            ct = "(desconhecido)"
            raise RuntimeError(f"Esperado PDF mas recebeu Content-Type: {ct}")  # noqa: EM102, TRY003
        return content

    async def gerar_zip_processo(self, protocolo_formatado: str) -> bytes:
        """Gera e baixa o ZIP com todos os documentos de um processo SEI.

        Usa o mesmo endpoint do botão "Gerar ZIP" da interface web.
        Retorna os bytes brutos do arquivo ZIP.
        """
        await self.ensure_authenticated()
        return await self._gerar_arquivo_processo(protocolo_formatado, "procedimento_gerar_zip")

    async def listar_atividades(self, protocolo_formatado: str) -> dict:  # noqa: C901
        """Lista andamentos/atividades de um processo via web scraper.

        Scrape de `procedimento_consultar_historico.php` (~370 ms, vs ~2.5 s REST).
        Precisa da URL assinada do histórico que está na árvore do processo.

        Retorna:
            {
              "processo": {"protocolo": str, "id_procedimento": str},
              "total_andamentos": int,
              "andamentos": [{data_hora, unidade, usuario, descricao}, ...],
            }
        """
        await self.ensure_authenticated()

        # garante que o protocolo está no cache
        if protocolo_formatado not in self._trabalhar_links:
            await self.fetch_inbox(detalhada=False)
        if protocolo_formatado not in self._trabalhar_links:
            await self.pesquisar_processo(protocolo_formatado)

        trab_url = urljoin(str(self._inbox_url), self._trabalhar_links[protocolo_formatado])

        # frameset → arvore
        r1 = await self._http.get(trab_url, headers={"Referer": str(self._inbox_url)})
        if r1.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"trabalhar status={r1.status_code}")  # noqa: EM102, TRY003
        soup_fs = BeautifulSoup(r1.text, "html.parser")
        ifr = soup_fs.find("iframe", id="ifrArvore")
        if not ifr:
            raise RuntimeError("ifrArvore não encontrado")  # noqa: EM101, TRY003
        arvore_url = urljoin(str(r1.url), _tag_str(ifr, "src").replace("&amp;", "&"))

        m_id = re.search(r"id_procedimento=(\d+)", str(r1.url))
        id_proc = m_id.group(1) if m_id else ""

        # fetch arvore para pegar o link do histórico
        r2 = await self._http.get(arvore_url, headers={"Referer": trab_url})
        if r2.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"arvore status={r2.status_code}")  # noqa: EM102, TRY003

        m_hist = re.search(
            r"(controlador\.php\?acao=procedimento_consultar_historico[^\"']*infra_hash=[a-f0-9]+)",
            r2.text,
        )
        if not m_hist:
            raise RuntimeError("Link procedimento_consultar_historico não encontrado na árvore")  # noqa: EM101, TRY003
        hist_url = urljoin(str(r2.url), m_hist.group(1).replace("&amp;", "&"))

        # fetch histórico
        r3 = await self._http.get(hist_url, headers={"Referer": str(r2.url)})
        if r3.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"histórico status={r3.status_code}")  # noqa: EM102, TRY003

        soup_h = BeautifulSoup(r3.text, "html.parser")
        tbl = soup_h.find("table", id="tblHistorico")
        andamentos: list[dict[str, str]] = []
        if tbl:
            for tr in tbl.find_all("tr")[1:]:  # pula header
                tds = tr.find_all("td")
                if len(tds) >= 4:  # noqa: PLR2004
                    andamentos.append(
                        {
                            "data_hora": tds[0].get_text(" ", strip=True),
                            "unidade": tds[1].get_text(" ", strip=True),
                            "usuario": tds[2].get_text(" ", strip=True),
                            "descricao": tds[3].get_text(" ", strip=True),
                        }
                    )

        return {
            "processo": {
                "protocolo": protocolo_formatado,
                "id_procedimento": id_proc,
            },
            "total_andamentos": len(andamentos),
            "andamentos": andamentos,
        }

    # ------------------------------------------------------------------
    # Complex forms — PR #5
    # ------------------------------------------------------------------

    async def autocomplete_unidades(self, termo: str) -> list[dict]:
        """Resolve sigla/nome de unidade via AJAX autocomplete do SEI.

        Retorna lista de {"id": str, "sigla": str, "nome": str}.
        """
        await self.ensure_authenticated()
        sei_base = f"{self.sei_root}/sei/"
        r = await self._http.get(
            f"{sei_base}controlador_ajax.php",
            params={"acao_ajax": "unidade_auto_completar", "termo": termo},
            headers={"Referer": str(self._inbox_url)},
        )
        if r.status_code != 200:  # noqa: PLR2004
            return []
        try:
            raw = r.json()
        except Exception:  # noqa: BLE001
            return []
        results = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "id": str(item.get("id", item.get("value", ""))),
                    "sigla": str(item.get("sigla", item.get("label", ""))),
                    "nome": str(item.get("nome", item.get("descricao", ""))),
                }
            )
        return results

    async def enviar_processo_web(  # noqa: C901, PLR0912, PLR0913
        self,
        protocolo: str,
        unidades_ids: list[str],
        manter_aberto: str = "N",
        remover_anotacao: str = "N",
        enviar_email: str = "N",
        data_retorno: str = "",
        dias_retorno: str = "",
    ) -> dict:
        """Envia (tramita) um processo via scraper web do SEI.

        Fluxo: trabalhar → arvore → link(procedimento_tramitar) → GET form → POST.
        As `unidades_ids` devem ser IDs numéricos já resolvidos.
        """
        await self.ensure_authenticated()

        html_arvore, url_arvore = await self._arvore_do_processo(protocolo)
        sei_base = f"{self.sei_root}/sei/"

        m = re.search(
            r"(controlador\.php\?acao=procedimento_tramitar[^\"'\s]*infra_hash=[a-fA-F0-9]+)",
            html_arvore,
        )
        if not m:
            raise RuntimeError(  # noqa: TRY003
                f"Ação 'procedimento_tramitar' não encontrada na árvore de {protocolo}. "  # noqa: EM102
                "Verifique permissão de tramitação neste processo."
            )

        tramitar_url = urljoin(sei_base, m.group(1).replace("&amp;", "&"))
        r = await self._http.get(tramitar_url, headers={"Referer": url_arvore})
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"procedimento_tramitar status={r.status_code}")  # noqa: EM102, TRY003

        body = r.content.decode("iso-8859-1", "replace")
        erro = _extrair_erro_sei(body)
        if erro:
            raise RuntimeError(erro)

        soup = BeautifulSoup(body, "html.parser")
        form = soup.find("form")
        if not isinstance(form, Tag):
            raise RuntimeError("Form procedimento_tramitar não encontrado.")  # noqa: EM101, TRY003, TRY004

        action = _tag_str(form, "action").replace("&amp;", "&")
        post_url = urljoin(sei_base, action) if action else tramitar_url

        # Coleta campos hidden do form
        post_data: list[tuple[str, str]] = []
        for inp in form.find_all("input", type="hidden"):
            if not isinstance(inp, Tag):
                continue
            name = _tag_str(inp, "name")
            if name:
                post_data.append((name, _tag_str(inp, "value")))

        # Botão submit obrigatório (PHP ignora POST sem ele silenciosamente)
        sbm = _extrair_submit_btn(form)
        if sbm:
            post_data.append(sbm)

        # Adiciona uma entrada hdnIdUnidadeEnvio por unidade destino
        post_data.extend(("hdnIdUnidadeEnvio", uid) for uid in unidades_ids)

        # Opções de tramitação — usa os nomes padrão do SEI
        if manter_aberto.upper() == "S":
            post_data.append(("chkSinManterAberto", "S"))
        if remover_anotacao.upper() == "S":
            post_data.append(("chkSinRemoverAnotacoes", "S"))
        if enviar_email.upper() == "S":
            post_data.append(("chkSinEnviarEmailNotificacao", "S"))
        if data_retorno:
            post_data.append(("dtaRetorno", data_retorno))
        if dias_retorno:
            post_data.append(("numDiasRetorno", dias_retorno))

        r2 = await self._http.post(
            post_url,
            content=urlencode(post_data).encode(),
            headers={"Referer": tramitar_url, "Content-Type": "application/x-www-form-urlencoded"},
        )
        if r2.status_code not in (200, 302):
            raise RuntimeError(f"POST procedimento_tramitar falhou com status={r2.status_code}")  # noqa: EM102, TRY003
        body2 = r2.content.decode("iso-8859-1", "replace")
        erro2 = _extrair_erro_sei(body2)
        if erro2:
            raise RuntimeError(erro2)

        return {
            "ok": True,
            "mensagem": f"Processo {protocolo} enviado para {len(unidades_ids)} unidade(s).",
            "protocolo": protocolo,
            "unidades": unidades_ids,
        }

    async def _obter_link_toolbar(self, acao: str) -> str:
        """Retorna URL assinada (com infra_hash) de uma ação do toolbar da inbox.

        Busca o link da ação `acao` no HTML da inbox. Necessário para ações
        que não partem de um processo específico (ex: procedimento_cadastrar).
        """
        await self.ensure_authenticated()
        inbox_url = str(self._inbox_url)
        r = await self._http.get(
            inbox_url,
            headers={"Referer": inbox_url},
        )
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"inbox status={r.status_code}")  # noqa: EM102, TRY003
        html = r.content.decode("iso-8859-1", "replace")
        m = re.search(
            rf"(controlador\.php\?acao={re.escape(acao)}[^\"'\s]*infra_hash=[a-fA-F0-9]+)",
            html,
        )
        if not m:
            raise RuntimeError(  # noqa: TRY003
                f"Ação '{acao}' não encontrada no toolbar da inbox."  # noqa: EM102
            )
        sei_base = f"{self.sei_root}/sei/"
        return urljoin(sei_base, m.group(1).replace("&amp;", "&"))

    async def criar_processo_web(  # noqa: C901, PLR0912, PLR0913, PLR0915
        self,
        tipo_processo: str,
        especificacao: str = "",
        assuntos_ids: list[str] | None = None,
        interessados_ids: list[str] | None = None,
        nivel_acesso: str = "0",
        hipotese_legal: str = "",
    ) -> dict:
        """Cria novo processo via scraper web do SEI.

        Fluxo: toolbar(procedimento_cadastrar) → GET form → POST.
        """
        await self.ensure_authenticated()
        sei_base = f"{self.sei_root}/sei/"

        cadastrar_url = await self._obter_link_toolbar("procedimento_cadastrar")
        r = await self._http.get(cadastrar_url, headers={"Referer": str(self._inbox_url)})
        if r.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"procedimento_cadastrar status={r.status_code}")  # noqa: EM102, TRY003

        body = r.content.decode("iso-8859-1", "replace")
        erro = _extrair_erro_sei(body)
        if erro:
            raise RuntimeError(erro)

        soup = BeautifulSoup(body, "html.parser")
        form = soup.find("form")
        if not isinstance(form, Tag):
            raise RuntimeError("Form procedimento_cadastrar não encontrado.")  # noqa: EM101, TRY003, TRY004

        action = _tag_str(form, "action").replace("&amp;", "&")
        post_url = urljoin(sei_base, action) if action else cadastrar_url

        post_data: list[tuple[str, str]] = []
        for inp in form.find_all("input", type="hidden"):
            if not isinstance(inp, Tag):
                continue
            name = _tag_str(inp, "name")
            if name:
                post_data.append((name, _tag_str(inp, "value")))

        # Botão submit obrigatório (PHP ignora POST sem ele silenciosamente)
        sbm = _extrair_submit_btn(form)
        if sbm:
            post_data.append(sbm)

        post_data.append(("selTipoProcedimento", tipo_processo))
        if especificacao:
            post_data.append(("txtDescricao", especificacao))
        post_data.append(("selNivelAcesso", nivel_acesso))
        if hipotese_legal and nivel_acesso in ("1", "2"):
            post_data.append(("selHipoteseLegal", hipotese_legal))
        post_data.extend(("hdnIdAssunto", aid) for aid in (assuntos_ids or []))
        post_data.extend(("hdnIdInteressado", iid) for iid in (interessados_ids or []))

        r2 = await self._http.post(
            post_url,
            content=urlencode(post_data).encode(),
            headers={"Referer": cadastrar_url, "Content-Type": "application/x-www-form-urlencoded"},
        )
        if r2.status_code not in (200, 302):
            raise RuntimeError(f"POST procedimento_cadastrar falhou com status={r2.status_code}")  # noqa: EM102, TRY003
        body2 = r2.content.decode("iso-8859-1", "replace")
        erro2 = _extrair_erro_sei(body2)
        if erro2:
            raise RuntimeError(erro2)

        # Extrai o IdProcedimento e protocoloFormatado da resposta
        id_proc = ""
        protocolo = ""
        m_id = re.search(r"IdProcedimento[\"']?\s*[:=]\s*[\"']?(\d+)", body2)
        if m_id:
            id_proc = m_id.group(1)
        m_proto = re.search(r"ProtocoloFormatado[\"']?\s*[:=]\s*[\"']([^\"']+)", body2)
        if m_proto:
            protocolo = m_proto.group(1)

        # Fallback: final URL may carry the id
        if not id_proc:
            m_url = re.search(r"id_procedimento=(\d+)", str(r2.url))
            if m_url:
                id_proc = m_url.group(1)

        if not id_proc:
            raise RuntimeError(  # noqa: TRY003
                "Processo aparentemente criado mas idProcedimento não pôde ser extraído da resposta."  # noqa: EM101
            )

        return {
            "ok": True,
            "idProcedimento": id_proc,
            "protocoloFormatado": protocolo,
            "mensagem": "Processo criado com sucesso.",
        }

    async def criar_documento_interno_web(  # noqa: C901, PLR0912, PLR0915
        self,
        protocolo: str,
        id_serie: str,
        descricao: str = "",
        nivel_acesso: str = "0",
        hipotese_legal: str = "",
    ) -> dict:
        """Cria documento interno em um processo via scraper web do SEI.

        Fluxo:
          1. arvore → link documento_escolher_tipo
          2. GET documento_escolher_tipo → busca link para id_serie
          3. GET editor_montar para o tipo escolhido
          4. POST documento_gerar com campos do editor (conteúdo vazio)

        O documento é criado vazio; use sei_editar_secao para inserir conteúdo.
        """
        await self.ensure_authenticated()

        html_arvore, url_arvore = await self._arvore_do_processo(protocolo)
        sei_base = f"{self.sei_root}/sei/"

        # --- Step 1: encontrar link documento_escolher_tipo na árvore ---
        incluir_href: str | None = None
        soup_acoes = BeautifulSoup(html_arvore, "html.parser")
        for a in soup_acoes.find_all("a", href=re.compile(r"documento_escolher_tipo")):
            incluir_href = _tag_str(a, "href").replace("&amp;", "&")
            break
        if not incluir_href:
            for img in soup_acoes.find_all("img"):
                if "Incluir" in (img.get("title", "") or "") or "incluir" in (
                    img.get("src", "") or ""
                ):
                    pa = img.find_parent("a")
                    # Confirm the parent link points to documento_escolher_tipo,
                    # not "Incluir em Bloco" or other "incluir" toolbar actions.
                    if pa and "documento_escolher_tipo" in _tag_str(pa, "href"):
                        incluir_href = _tag_str(pa, "href").replace("&amp;", "&")
                        break

        if not incluir_href:
            raise RuntimeError(  # noqa: TRY003
                "Link 'Incluir Documento' não encontrado nas ações do processo."  # noqa: EM101
            )

        escolher_url = urljoin(sei_base, incluir_href)

        # --- Step 2: GET documento_escolher_tipo e encontrar link para id_serie ---
        r3 = await self._http.get(escolher_url, headers={"Referer": url_arvore})
        if r3.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"documento_escolher_tipo status={r3.status_code}")  # noqa: EM102, TRY003

        body3 = r3.content.decode("iso-8859-1", "replace")
        erro3 = _extrair_erro_sei(body3)
        if erro3:
            raise RuntimeError(erro3)

        # Se id_serie não fornecido — retorna lista de tipos disponíveis
        if not id_serie:
            soup3 = BeautifulSoup(body3, "html.parser")
            tipos = []
            for a in soup3.find_all("a", href=re.compile(r"id_serie=")):
                href = _tag_str(a, "href")
                m_s = re.search(r"id_serie=(\d+)", href)
                if m_s:
                    tipos.append({"id_serie": m_s.group(1), "nome": a.get_text(strip=True)})
            return {"tipos_disponiveis": tipos}

        # Encontra o link do editor para este id_serie
        m_editor = re.search(
            rf"(controlador\.php[^\"'\s]*acao=editor_montar[^\"'\s]*id_serie={re.escape(id_serie)}[^\"'\s]*infra_hash=[a-fA-F0-9]+)",
            body3,
        )
        if not m_editor:
            # Try reverse order: infra_hash before id_serie
            m_editor = re.search(
                rf"(controlador\.php[^\"'\s]*acao=editor_montar[^\"'\s]*infra_hash=[a-fA-F0-9]+[^\"'\s]*id_serie={re.escape(id_serie)}[^\"'\s]*)",
                body3,
            )
        if not m_editor:
            raise RuntimeError(  # noqa: TRY003
                f"Link editor_montar para id_serie={id_serie} não encontrado. "  # noqa: EM102
                "Use id_serie='' para listar os tipos disponíveis."
            )

        editor_url = urljoin(sei_base, m_editor.group(1).replace("&amp;", "&"))

        # --- Step 3: GET editor_montar ---
        r4 = await self._http.get(editor_url, headers={"Referer": str(r3.url)})
        if r4.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"editor_montar status={r4.status_code}")  # noqa: EM102, TRY003

        body4 = r4.content.decode("iso-8859-1", "replace")
        erro4 = _extrair_erro_sei(body4)
        if erro4:
            raise RuntimeError(erro4)

        soup4 = BeautifulSoup(body4, "html.parser")
        form4 = soup4.find("form")
        if not isinstance(form4, Tag):
            raise RuntimeError("Form editor_montar não encontrado.")  # noqa: EM101, TRY003, TRY004

        action4 = _tag_str(form4, "action").replace("&amp;", "&")
        post_url4 = urljoin(sei_base, action4) if action4 else editor_url

        # --- Step 4: POST documento_gerar ---
        post_data4: list[tuple[str, str]] = []
        for inp in form4.find_all("input", type="hidden"):
            if not isinstance(inp, Tag):
                continue
            name = _tag_str(inp, "name")
            if name:
                post_data4.append((name, _tag_str(inp, "value")))

        # Botão submit obrigatório (PHP ignora POST sem ele silenciosamente)
        sbm4 = _extrair_submit_btn(form4)
        if sbm4:
            post_data4.append(sbm4)

        if descricao:
            post_data4.append(("txtDescricao", descricao))
        post_data4.append(("selNivelAcesso", nivel_acesso))
        if hipotese_legal and nivel_acesso in ("1", "2"):
            post_data4.append(("selHipoteseLegal", hipotese_legal))

        r5 = await self._http.post(
            post_url4,
            content=urlencode(post_data4).encode(),
            headers={"Referer": editor_url, "Content-Type": "application/x-www-form-urlencoded"},
        )
        if r5.status_code not in (200, 302):
            raise RuntimeError(f"POST documento_gerar falhou com status={r5.status_code}")  # noqa: EM102, TRY003
        body5 = r5.content.decode("iso-8859-1", "replace")
        erro5 = _extrair_erro_sei(body5)
        if erro5:
            raise RuntimeError(erro5)

        # Extrai id do documento criado da resposta / URL final
        id_doc = ""
        m_doc = re.search(r"id_documento=(\d+)", str(r5.url))
        if m_doc:
            id_doc = m_doc.group(1)
        if not id_doc:
            m_doc2 = re.search(r"IdDocumento[\"']?\s*[:=]\s*[\"']?(\d+)", body5)
            if m_doc2:
                id_doc = m_doc2.group(1)

        if not id_doc:
            raise RuntimeError(  # noqa: TRY003
                "Documento aparentemente criado mas idDocumento não pôde ser extraído da resposta."  # noqa: EM101
            )

        return {
            "ok": True,
            "idDocumento": id_doc,
            "protocolo": protocolo,
            "id_serie": id_serie,
            "mensagem": "Documento criado com sucesso.",
        }

    MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # limite de segurança (o SEI rejeita antes)

    async def incluir_documento_externo(  # noqa: C901, PLR0912, PLR0913, PLR0915
        self,
        protocolo_formatado: str,
        arquivo_path: str | None = None,
        nome_arquivo: str | None = None,
        id_serie: str | None = None,
        data_elaboracao: str = "",
        nivel_acesso: str = "0",
        hipotese_legal: str = "",
        conteudo: bytes | None = None,
    ) -> dict:
        """Inclui documento externo (upload de arquivo) em processo SEI via web.

        Fluxo:
        1. procedimento_trabalhar → frameset → arvore_montar
        2. Extrai Nos[0].acoes → link documento_escolher_tipo
        3. GET documento_escolher_tipo
        4. POST frmDocumentoEscolherTipo com hdnIdSerie=-1 → documento_receber
        5. Parse: upload URL, selSerie, user, unidade
        6. Se id_serie vazio → retorna tipos disponíveis
        7. POST multipart upload (filArquivo) → nome_upload#nome#data_hora#tamanho
        8. Build hdnAnexos + POST frmDocumentoCadastro

        Retorna:
            {"sucesso": True, "url_final": str}
            ou {"tipos_disponiveis": [{id, nome}, ...]} se id_serie=None
        """
        import mimetypes  # noqa: PLC0415
        import os as _os  # noqa: PLC0415
        from datetime import date as _date  # noqa: PLC0415

        await self.ensure_authenticated()

        if protocolo_formatado not in self._trabalhar_links:
            await self.fetch_inbox(detalhada=False)
        if protocolo_formatado not in self._trabalhar_links:
            await self.pesquisar_processo(protocolo_formatado)

        trab_url = urljoin(str(self._inbox_url), self._trabalhar_links[protocolo_formatado])

        # --- Step 1: trabalhar → frameset ---
        r1 = await self._http.get(trab_url, headers={"Referer": str(self._inbox_url)})
        if r1.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"trabalhar status={r1.status_code}")  # noqa: EM102, TRY003
        if 'name="txtUsuario"' in r1.text or 'id="txtUsuario"' in r1.text:
            self._form_action = None
            await self.login()
            return await self.incluir_documento_externo(
                protocolo_formatado,
                arquivo_path,
                nome_arquivo,
                id_serie,
                data_elaboracao,
                nivel_acesso,
                hipotese_legal,
                conteudo,
            )

        soup_fs = BeautifulSoup(r1.text, "html.parser")
        ifr = soup_fs.find("iframe", id="ifrArvore")
        if not ifr:
            raise RuntimeError("ifrArvore não encontrado no frameset")  # noqa: EM101, TRY003
        arvore_url = urljoin(str(r1.url), _tag_str(ifr, "src").replace("&amp;", "&"))

        # --- Step 2: arvore_montar → Nos[0].acoes ---
        r2 = await self._http.get(arvore_url, headers={"Referer": str(r1.url)})
        if r2.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"arvore status={r2.status_code}")  # noqa: EM102, TRY003

        acoes_html = ""
        for pat in (
            r"Nos\[0\]\.acoes\s*=\s*'((?:[^'\\]|\\.)*)'",
            r'Nos\[0\]\.acoes\s*=\s*"((?:[^"\\]|\\.)*)"',
        ):
            m = re.search(pat, r2.text, re.S)  # noqa: FURB167
            if m:
                acoes_html = (
                    m.group(1).replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
                )
                break

        if not acoes_html:
            raise RuntimeError(  # noqa: TRY003
                "Nos[0].acoes não encontrado — o processo pode estar concluído "  # noqa: EM101
                "ou você não tem permissão para incluir documentos nele."
            )

        sei_base = f"{self.sei_root}/sei/"
        soup_acoes = BeautifulSoup(acoes_html, "html.parser")
        incluir_href: str | None = None
        for a in soup_acoes.find_all("a", href=re.compile(r"documento_escolher_tipo")):
            incluir_href = _tag_str(a, "href").replace("&amp;", "&")
            break
        if not incluir_href:
            for img in soup_acoes.find_all("img"):
                if "Incluir" in (img.get("title", "") or "") or "incluir" in (
                    img.get("src", "") or ""
                ):
                    pa = img.find_parent("a")
                    if pa:
                        incluir_href = _tag_str(pa, "href").replace("&amp;", "&")
                        break

        if not incluir_href:
            raise RuntimeError(  # noqa: TRY003
                "Link 'Incluir Documento' não encontrado nas ações do processo. "  # noqa: EM101
                "O processo pode estar concluído, sem tramitação para esta unidade, "
                "ou você não tem permissão. Tente reabrir o processo primeiro."
            )

        # --- Step 3: GET documento_escolher_tipo ---
        escolher_url = urljoin(sei_base, incluir_href)
        r3 = await self._http.get(escolher_url, headers={"Referer": str(r2.url)})
        if r3.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"documento_escolher_tipo status={r3.status_code}")  # noqa: EM102, TRY003

        body3 = r3.content.decode("iso-8859-1", "replace")
        soup3 = BeautifulSoup(body3, "html.parser")
        form3 = soup3.find("form", id="frmDocumentoEscolherTipo")
        if not form3:
            raise RuntimeError("frmDocumentoEscolherTipo não encontrado")  # noqa: EM101, TRY003
        form3_action = _tag_str(form3, "action").replace("&amp;", "&")
        post3_url = urljoin(str(r3.url), form3_action)

        # --- Step 4: POST escolher com hdnIdSerie=-1 → documento_receber ---
        post3_data: dict[str, str] = {}
        for inp in form3.find_all("input", type="hidden"):
            if not isinstance(inp, Tag):
                continue
            n = _tag_str(inp, "name")
            if n:
                post3_data[n] = _tag_str(inp, "value")
        post3_data["hdnIdSerie"] = "-1"

        r4 = await self._http.post(post3_url, data=post3_data, headers={"Referer": str(r3.url)})
        if r4.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"POST escolher_tipo status={r4.status_code}")  # noqa: EM102, TRY003

        body4 = r4.content.decode("iso-8859-1", "replace")

        # --- Step 5: Parse documento_receber ---
        # Validação de página: infraUpload deve estar presente no JS
        if "infraUpload" not in body4 and "frmDocumentoCadastro" not in body4:
            raise RuntimeError(  # noqa: TRY003
                "documento_receber não encontrado — verifique o processo e as permissões"  # noqa: EM101
            )

        # parse frmDocumentoCadastro
        soup4 = BeautifulSoup(body4, "html.parser")
        form4 = soup4.find("form", id="frmDocumentoCadastro")
        if not form4:
            raise RuntimeError("frmDocumentoCadastro não encontrado em documento_receber")  # noqa: EM101, TRY003
        form4_action = _tag_str(form4, "action").replace("&amp;", "&")
        post4_url = urljoin(str(r4.url), form4_action)

        form4_data: dict[str, str] = {}
        for inp in form4.find_all("input", type="hidden"):
            if not isinstance(inp, Tag):
                continue
            n = _tag_str(inp, "name")
            if n:
                form4_data[n] = _tag_str(inp, "value")

        # selSerie options
        sel_serie = form4.find("select", attrs={"name": "selSerie"})
        tipos: list[dict] = []
        if sel_serie:
            for opt in sel_serie.find_all("option"):
                v = opt.get("value", "")
                t = opt.get_text(strip=True)
                if v and v not in ("-1", ""):
                    tipos.append({"id": v, "nome": t})

        # Se id_serie não informado, retorna tipos disponíveis
        if not id_serie:
            return {"tipos_disponiveis": tipos}

        # --- Step 6: Upload do arquivo ---
        if conteudo is not None:
            if not nome_arquivo:
                raise ValueError("nome_arquivo é obrigatório quando conteudo é passado")  # noqa: EM101, TRY003
            nome = nome_arquivo
            file_bytes = conteudo
        else:
            if not arquivo_path:
                raise ValueError("Informe arquivo_path ou conteudo")  # noqa: EM101, TRY003
            if not _os.path.isfile(arquivo_path):  # noqa: ASYNC240, PTH113
                raise ValueError(f"Arquivo não encontrado ou não é regular: {arquivo_path}")  # noqa: EM102, TRY003
            if _os.path.getsize(arquivo_path) > self.MAX_UPLOAD_BYTES:  # noqa: ASYNC240, PTH202
                raise ValueError(  # noqa: TRY003
                    f"Arquivo excede o limite de {self.MAX_UPLOAD_BYTES // 1024 // 1024} MB"  # noqa: EM102
                )
            nome = nome_arquivo or _os.path.basename(arquivo_path)  # noqa: PTH119
            with open(arquivo_path, "rb") as f:  # noqa: ASYNC230, PTH123
                file_bytes = f.read(self.MAX_UPLOAD_BYTES + 1)
        if len(file_bytes) > self.MAX_UPLOAD_BYTES:
            raise ValueError(  # noqa: TRY003
                f"Conteúdo excede o limite de {self.MAX_UPLOAD_BYTES // 1024 // 1024} MB"  # noqa: EM102
            )
        mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"

        tam_int = len(file_bytes)
        if tam_int < 1024:  # noqa: PLR2004
            tamanho_fmt = f"{tam_int} B"
        elif tam_int < 1024 * 1024:
            tamanho_fmt = f"{tam_int / 1024:.1f} KB"
        else:
            tamanho_fmt = f"{tam_int / 1024 / 1024:.1f} MB"

        # Extrai URL de upload: new infraUpload('frmAnexos', 'URL')
        m_up = re.search(
            r"new infraUpload\(['\"][^'\"]*['\"],\s*['\"]([^'\"]*documento_upload_anexo[^'\"]*)['\"]",
            body4,
        )
        if not m_up:
            raise RuntimeError("URL de upload (infraUpload) não encontrada em documento_receber")  # noqa: EM101, TRY003
        upload_url = urljoin(str(r4.url), m_up.group(1).replace("&amp;", "&"))

        r5 = await self._http.post(
            upload_url,
            files={"filArquivo": (nome, file_bytes, mime)},
            headers={"Referer": str(r4.url)},
        )
        if r5.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"upload status={r5.status_code}: {r5.text[:200]}")  # noqa: EM102, TRY003

        # Resposta: nome_upload#nome#mime#tamanho#data_hora#  # noqa: ERA001
        up_parts = r5.text.strip().rstrip("#").split("#")
        if len(up_parts) < 2:  # noqa: PLR2004
            raise RuntimeError(f"Resposta de upload inesperada: {r5.text!r}")  # noqa: EM102, TRY003
        nome_upload = up_parts[0]
        upload_dh = up_parts[4] if len(up_parts) > 4 else ""  # noqa: PLR2004
        upload_tam = up_parts[3] if len(up_parts) > 3 else str(tam_int)  # noqa: PLR2004

        # Extrai usuario e unidade da linha JS:
        # objTabelaAnexos.adicionar([arr[...], ..., 'CPF', 'SIGLA'])  # noqa: ERA001
        m_add = re.search(
            r"objTabelaAnexos\.adicionar\(\[.*?'([0-9]+)'\s*,\s*'([^']+)'\s*\]\)",
            body4,
            re.DOTALL,
        )
        usuario = m_add.group(1) if m_add else self._usuario
        unidade = m_add.group(2) if m_add else ""

        # --- Step 7: POST frmDocumentoCadastro com hdnAnexos ---
        # SEI Pro extension usa ± (U+00B1) como separador, com encodeURIComponent
        # e remoção do byte alto UTF-8 (%C2) para manter %B1 (ISO-8859-1 ±).
        # O PHP servidor divide hdnAnexos em \xB1.
        import urllib.parse as _up  # noqa: PLC0415

        _SEP = "%B1"  # ± URL-encoded como ISO-8859-1 (PHP split target)  # noqa: N806

        def _qpart(s: str) -> str:
            # '+' fora do safe → vira %2B ('+' literal no nome não pode chegar
            # cru ao corpo x-www-form-urlencoded, onde decodifica como espaço);
            # espaço → %20 → '+' (convenção form-urlencoded)
            return _up.quote(s, safe="-.!~*'()_").replace("%20", "+")

        hdn_anexos = _SEP.join(
            [
                _qpart(nome_upload),
                _qpart(nome),
                _qpart(upload_dh),
                _qpart(upload_tam),
                _qpart(tamanho_fmt),
                _qpart(usuario),
                _qpart(unidade),
            ]
        )

        # Monta body URL-encoded manualmente para hdnAnexos não ser duplo-codificado
        form4_data["hdnAnexos"] = ""  # placeholder — substituído abaixo
        form4_data["hdnIdSerie"] = id_serie
        form4_data["selSerie"] = id_serie
        form4_data["txtDataElaboracao"] = data_elaboracao or _date.today().strftime("%d/%m/%Y")  # noqa: DTZ011
        form4_data["hdnStaNivelAcessoLocal"] = nivel_acesso
        form4_data["rdoNivelAcesso"] = nivel_acesso
        if hipotese_legal and nivel_acesso in ("1", "2"):
            form4_data["selHipoteseLegal"] = hipotese_legal
        form4_data["rdoFormato"] = "N"  # nato-digital
        # JS submeter() altera de '1' → '2' antes do form.submit()
        form4_data["hdnFlagDocumentoCadastro"] = "2"

        # Codifica todos os campos exceto hdnAnexos, depois concatena manualmente
        other_fields = {k: v for k, v in form4_data.items() if k != "hdnAnexos"}
        raw_body = _up.urlencode(other_fields) + "&hdnAnexos=" + hdn_anexos

        r6 = await self._http.post(
            post4_url,
            content=raw_body.encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": str(r4.url),
            },
        )
        if r6.status_code != 200:  # noqa: PLR2004
            raise RuntimeError(f"POST frmDocumentoCadastro status={r6.status_code}")  # noqa: EM102, TRY003

        body6 = r6.content.decode("iso-8859-1", "replace")
        final_url = str(r6.url)
        sucesso = "arvore_visualizar" in final_url

        if not sucesso:
            soup6 = BeautifulSoup(body6, "html.parser")
            erros = []
            for cls in ["infraMsg", "infraMsgErro", "errMsg"]:
                el = soup6.find(class_=cls)
                if el:
                    erros.append(el.get_text(strip=True)[:300])
            scripts6 = re.findall(r"<script[^>]*>(.*?)</script>", body6, re.DOTALL | re.IGNORECASE)
            for sc in scripts6:
                if "alert(" in sc:
                    m_alert = re.search(r"alert\(['\"]([^'\"]+)['\"]", sc)
                    if m_alert:
                        erros.append(m_alert.group(1))
            msg = "; ".join(erros) if erros else f"URL final inesperada: {final_url}"
            raise RuntimeError(f"Falha ao incluir documento: {msg}")  # noqa: EM102, TRY003

        m_id = re.search(r"id_documento=(\d+)", final_url)
        id_doc = m_id.group(1) if m_id else ""
        self._invalidar_arvore(protocolo_formatado)
        return {
            "sucesso": True,
            "id_documento": id_doc,
            "url_final": final_url,
            "nome_arquivo": nome,
            "tamanho": tamanho_fmt,
        }

    async def listar_processos(
        self,
        detalhada: bool = True,  # noqa: FBT001, FBT002
        pagina: int = 0,
        apenas_meus: bool = False,  # noqa: FBT001, FBT002
        tipo: str = "",
        filtro: str = "",
    ) -> dict:
        """Lista processos da caixa da unidade atual via web scraper.

        Filtros server-side (POST form fields):
        - `apenas_meus=True`: hdnMeusProcessos=M (apenas atribuídos ao usuário logado)

        Filtros client-side (após fetch, em substring case-insensitive):
        - `tipo`: filtra pela coluna "Tipo" (apenas detalhada)
        - `filtro`: filtra por substring em qualquer campo de texto

        Retorna dict no formato:
            {
              "processos": [{...}, ...],
              "total_itens": N,            # total no servidor (antes de filtros client-side)
              "total_filtrados": N,        # após filtros client-side
              "pagina_atual": int,
              "tem_proxima": bool,
              "layout": "detalhada"|"resumida",
            }
        """
        _, html = await self.fetch_inbox(
            detalhada=detalhada, pagina=pagina, apenas_meus=apenas_meus
        )
        layout, rows = parse_inbox(html)

        # total_itens: vem dos hidden fields hdn{Selecao}NroItens (capturados
        # pelo _extract_main_form via fetch_inbox). Esses campos têm o total
        # da seleção atual no servidor, não só da página visível.
        if layout == "detalhada":
            total_servidor = int(self._form_hidden.get("hdnDetalhadoNroItens", "0") or "0")
        else:
            total_servidor = int(self._form_hidden.get("hdnRecebidosNroItens", "0") or "0") + int(
                self._form_hidden.get("hdnGeradosNroItens", "0") or "0"
            )
        if total_servidor == 0:
            total_servidor = len(rows)

        # Filtros client-side: aplicados após o parse, sobre os rows.
        rows_filtrados = rows
        if tipo:
            tipo_lower = tipo.lower()
            rows_filtrados = [
                r for r in rows_filtrados if tipo_lower in (r.get("Tipo", "") or "").lower()
            ]
        if filtro:
            filtro_lower = filtro.lower()
            rows_filtrados = [
                r
                for r in rows_filtrados
                if any(
                    filtro_lower in str(v).lower()
                    for v in r.values()
                    if isinstance(v, (str, int, float))
                )
            ]

        return {
            "processos": rows_filtrados,
            "total_itens": total_servidor,
            "total_filtrados": len(rows_filtrados),
            "pagina_atual": pagina,
            "tem_proxima": len(rows) > 0 and (pagina + 1) * max(len(rows), 1) < total_servidor,
            "layout": layout,
        }


# ---------------------------------------------------------------------------
# Parsers de HTML (independentes de instância)
# ---------------------------------------------------------------------------


def parse_arvore_nos(html: str) -> list[dict]:  # noqa: C901
    """Extrai o array `Nos[]` do JS de arvore_montar.php.

    Cada nó é construído como `Nos[i] = new infraArvoreNo(tipo, id, pai, link,
    target, label, tooltip, icone, ...)`. Retorna lista de dicts com as
    primeiras 8 posições nomeadas. O primeiro elemento (Nos[0]) é a raiz —
    o próprio processo.
    """
    out: list[dict] = []
    for m in re.finditer(
        r"Nos\[\d+\]\s*=\s*new infraArvoreNo\(([^;]*?)\);",
        html,
        re.S,  # noqa: FURB167
    ):
        args_str = m.group(1)
        # tokenizer simples: separa por vírgula respeitando aspas
        args: list[str] = []
        cur = ""
        in_str = False
        quote_char = None
        for ch in args_str:
            if in_str:
                cur += ch
                if ch == quote_char:
                    in_str = False
            elif ch in ('"', "'"):
                in_str = True
                quote_char = ch
                cur += ch
            elif ch == ",":
                args.append(cur.strip())
                cur = ""
            else:
                cur += ch
        if cur.strip():
            args.append(cur.strip())

        def unquote(s: str) -> str:
            s = s.strip()
            if s in ("null", ""):
                return ""
            if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
                return s[1:-1]
            return s

        if len(args) >= 7:  # noqa: PLR2004
            out.append(
                {
                    "tipo_no": unquote(args[0]),
                    "id": unquote(args[1]),
                    "pai": unquote(args[2]),
                    "link": unquote(args[3]),
                    "target": unquote(args[4]),
                    "label": unquote(args[5]),
                    "tooltip": unquote(args[6]),
                    "icone": unquote(args[7]) if len(args) > 7 else "",  # noqa: PLR2004
                }
            )
    return out


_RE_PARENS = re.compile(r"^\s*\(\s*|\s*\)\s*$")
# Parseia label de documento: "Despacho GPF 2874369" ou "Relatório (2869849)"
_RE_DOC_LABEL = re.compile(
    r"^(.+?)\s+([A-Z][A-Z0-9/_-]+)\s+(\d+)$"  # interno: Tipo SIGLA NUMERO
    r"|^(.+?)\s+\((\d+)\)$"  # externo: Tipo (NUMERO)
    r"|^(.+?)\s+([A-Z][A-Z0-9/_-]+)\s+(\d+)\s+\((\d+)\)$"  # misto: Tipo SIGLA NUMERO (SEI)
)


_RE_TOOLTIP = re.compile(r"infraTooltipMostrar\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")


def _parse_doc_label(label: str) -> dict:
    """Parseia o label de um nó DOCUMENTO da árvore do SEI.

    Formatos conhecidos:
    - Interno: "Despacho GPF 2874369"  → tipo=Despacho, sigla=GPF, numero=2874369
    - Externo: "Relatório Geral (2869849)" → tipo=Relatório Geral, numero=2869849
    - Misto:   "Comprovante de envio e-CGU - SA 4 (2869849)"

    Retorna dict com chaves opcionais: tipo_documento, sigla_unidade, numero_sei.
    """
    result: dict[str, str] = {}
    if not label:
        return result

    # Tenta formato interno: "Tipo SIGLA NUMERO"
    m = re.match(r"^(.+?)\s+([A-Z][A-Z0-9/_-]+)\s+(\d+)$", label)
    if m:
        result["tipo_documento"] = m.group(1).strip()
        result["sigla_unidade"] = m.group(2)
        result["numero_sei"] = m.group(3)
        return result

    # Tenta formato com parênteses: "Tipo (NUMERO)" ou "Tipo SIGLA (NUMERO)"
    m = re.match(r"^(.+?)\s+\((\d+)\)$", label)
    if m:
        corpo = m.group(1).strip()
        result["numero_sei"] = m.group(2)
        # tenta extrair sigla do corpo: "Comprovante e-CGU - SA 4"
        m2 = re.match(r"^(.+?)\s+([A-Z][A-Z0-9/_-]+)\s+\d*$", corpo)
        if m2:
            result["tipo_documento"] = m2.group(1).strip()
            result["sigla_unidade"] = m2.group(2)
        else:
            result["tipo_documento"] = corpo
        return result

    # fallback: label inteiro como tipo
    # tenta ao menos extrair o número no final
    m = re.search(r"(\d{5,})$", label)
    if m:
        result["numero_sei"] = m.group(1)
        result["tipo_documento"] = label[: m.start()].strip()
    else:
        result["tipo_documento"] = label
    return result


def _extract_tooltip(link_tag, row: dict) -> None:  # noqa: ANN001
    """Extrai especificacao e tipo do onmouseover do link do processo.

    O SEI renderiza um tooltip JS em TODOS os links de processo da inbox:
        onmouseover="return infraTooltipMostrar('Especificação','Tipo Processual')"

    Esse tooltip contém a especificação INDEPENDENTE de a coluna estar
    habilitada no painel — é sempre renderizado.
    """
    mouseover = link_tag.get("onmouseover", "")
    m = _RE_TOOLTIP.search(mouseover)
    if m:
        especificacao = m.group(1).strip()
        tipo_tooltip = m.group(2).strip()
        if especificacao:
            row["especificacao"] = especificacao
        if tipo_tooltip and "Tipo" not in row:
            row["tipo"] = tipo_tooltip


def parse_inbox(html: str) -> tuple[str, list[dict]]:  # noqa: C901, PLR0912, PLR0915
    """Parseia o HTML de procedimento_controlar.php e extrai lista de processos.

    Suporta dois layouts:
    - **Detalhada**: tabela única `tblProcessosDetalhado` com colunas
      configuráveis (Tipo, Especificação, Interessados, etc.)
    - **Resumida**: duas tabelas `tblProcessosRecebidos` + `tblProcessosGerados`
      (default do SEI quando o usuário não trocou para Detalhada)

    Retorna tupla `(layout, rows)` onde layout in {'detalhada','resumida','desconhecido'}.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []

    tbl = soup.find("table", id="tblProcessosDetalhado")
    if tbl:
        first_tr = tbl.find("tr")
        if first_tr is None:
            return ("detalhada", [])
        ths = first_tr.find_all("th")
        headers = [th.get_text(" ", strip=True) for th in ths]
        # 4 primeiras colunas tipicamente sem header textual:
        # checkbox / status icons / Processo / Atribuição
        col_names: list[str] = []
        for i, h in enumerate(headers):
            if h:
                col_names.append(h)
            else:
                col_names.append(
                    {0: "_check", 1: "icones", 2: "_processo", 3: "atribuicao"}.get(i, f"col{i}")
                )

        for tr in tbl.find_all("tr", id=re.compile(r"^P\d+$")):
            tds = tr.find_all("td", recursive=False)
            row: dict[str, Any] = {"id_procedimento": tr["id"][1:]}
            link = tr.find("a", href=re.compile(r"acao=procedimento_trabalhar"))
            if link is not None:
                row["protocolo"] = link.get_text(" ", strip=True)
                # Especificação + tipo estão no tooltip do link do processo:
                # onmouseover="return infraTooltipMostrar('Especificação','Tipo')"  # noqa: ERA001
                # Disponível INDEPENDENTE de a coluna estar habilitada no painel.
                _extract_tooltip(link, row)
            if len(tds) >= 2:  # noqa: PLR2004
                icones = []
                for img in tds[1].find_all("img"):
                    if not isinstance(img, Tag):
                        continue
                    title = _tag_str(img, "title") or _tag_str(img, "alt")
                    if title:
                        icones.append(title.strip())
                if icones:
                    row["icones"] = icones
            for i, name in enumerate(col_names):
                if name.startswith("_") or name == "icones":
                    continue
                if i < len(tds):
                    val = tds[i].get_text(" ", strip=True)
                    if val:
                        if name == "atribuicao":
                            val = _RE_PARENS.sub("", val).strip()
                        row[name] = val
            rows.append(row)
        return ("detalhada", rows)

    # Resumida — fallback
    found_any = False
    for tbl_id, origem in [
        ("tblProcessosRecebidos", "recebido"),
        ("tblProcessosGerados", "gerado"),
    ]:
        tbl = soup.find("table", id=tbl_id)
        if tbl is None:
            continue
        found_any = True
        for tr in tbl.find_all("tr", id=re.compile(r"^P\d+$")):
            tds = tr.find_all("td", recursive=False)
            row: dict[str, Any] = {
                "id_procedimento": tr["id"][1:],
                "origem": origem,
            }
            link = tr.find("a", href=re.compile(r"acao=procedimento_trabalhar"))
            if link is not None:
                row["protocolo"] = link.get_text(" ", strip=True)
                _extract_tooltip(link, row)
            if len(tds) >= 2:  # noqa: PLR2004
                icones = []
                for img in tds[1].find_all("img"):
                    if not isinstance(img, Tag):
                        continue
                    title = _tag_str(img, "title") or _tag_str(img, "alt")
                    if title:
                        icones.append(title.strip())
                if icones:
                    row["icones"] = icones
            if len(tds) >= 4:  # noqa: PLR2004
                atrib_text = _RE_PARENS.sub("", tds[-1].get_text(" ", strip=True)).strip()
                if atrib_text:
                    row["atribuicao"] = atrib_text
            rows.append(row)

    if found_any:
        return ("resumida", rows)
    return ("desconhecido", [])


# Tabelas de listas conhecidas das páginas de consulta — excluídas da
# extração genérica de pares label/valor de metadados
_TABELAS_LISTA = frozenset(
    {
        "tblAssinaturas",
        "tblCiencias",
        "tblSobrestamento",
        "tblUnidadesProcesso",
        "tblAndamento",
        "tblInteressados",
        "tblHistorico",
        "tblDocumentos",
    }
)


def _extrair_metadados_tabelas(soup: BeautifulSoup, result: dict[str, object]) -> None:
    """Extrai pares label/valor (th + td) das tabelas de metadados da página.

    Ignora as tabelas de listas conhecidas (assinaturas, ciências, etc.) e
    linhas de cabeçalho (duas células <th>), que não são pares label/valor.
    """
    for tbl in soup.find_all("table"):
        if not isinstance(tbl, Tag):
            continue
        if _tag_str(tbl, "id") in _TABELAS_LISTA:
            continue
        for tr in tbl.find_all("tr"):
            cels = tr.find_all(["th", "td"])
            if len(cels) != 2:  # noqa: PLR2004
                continue
            if cels[0].name == "th" and cels[1].name == "th":
                continue  # linha de cabeçalho, não par label/valor
            k = cels[0].get_text(" ", strip=True).rstrip(":").lower()
            v = cels[1].get_text(" ", strip=True)
            if k and v and len(k) < 60:  # noqa: PLR2004
                result[k.replace(" ", "_").replace("/", "_")] = v


def _parse_documento_consultar(html: str, id_documento: str) -> dict:
    """Extrai metadados, assinaturas e ciências de documento_consultar."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, object] = {"id_documento": id_documento}

    _extrair_metadados_tabelas(soup, result)

    # -- assinaturas: tblAssinaturas --
    assinaturas: list[dict] = []
    tbl_ass = soup.find("table", id="tblAssinaturas")
    if tbl_ass and isinstance(tbl_ass, Tag):
        for tr in tbl_ass.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) >= 3:  # noqa: PLR2004
                assinaturas.append(
                    {
                        "assinante": tds[0].get_text(" ", strip=True),
                        "cargo": tds[1].get_text(" ", strip=True),
                        "data_hora": tds[2].get_text(" ", strip=True),
                    }
                )
    result["assinaturas"] = assinaturas

    # -- ciências: tblCiencias --
    ciencias: list[dict] = []
    tbl_cien = soup.find("table", id="tblCiencias")
    if tbl_cien and isinstance(tbl_cien, Tag):
        for tr in tbl_cien.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) >= 3:  # noqa: PLR2004
                ciencias.append(
                    {
                        "usuario": tds[0].get_text(" ", strip=True),
                        "cargo": tds[1].get_text(" ", strip=True),
                        "data_hora": tds[2].get_text(" ", strip=True),
                    }
                )
    result["ciencias"] = ciencias

    return result


def _parse_procedimento_consultar(html: str, protocolo: str) -> dict:  # noqa: C901, PLR0912
    """Extrai unidades abertas, interessados e sobrestamento de procedimento_consultar."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, object] = {"protocolo": protocolo}

    _extrair_metadados_tabelas(soup, result)

    # -- unidades abertas: tblUnidadesProcesso --
    # (tblAndamento NÃO serve de fallback: é histórico com layout
    # data/unidade/usuário/descrição, não lista de unidades abertas)
    unidades: list[dict] = []
    tbl_un = soup.find("table", id="tblUnidadesProcesso")
    if tbl_un and isinstance(tbl_un, Tag):
        for tr in tbl_un.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if tds:
                entry: dict[str, str] = {"unidade": tds[0].get_text(" ", strip=True)}
                if len(tds) >= 2:  # noqa: PLR2004
                    entry["situacao"] = tds[1].get_text(" ", strip=True)
                unidades.append(entry)
    # Fallback: procura qualquer link de unidade
    if not unidades:
        for a in soup.find_all("a", href=re.compile(r"acao=unidade_visualizar")):
            if isinstance(a, Tag):
                txt = a.get_text(" ", strip=True)
                if txt:
                    unidades.append({"unidade": txt})
    result["unidades_abertas"] = unidades

    # -- interessados: busca por label ou tabela --
    interessados: list[str] = []
    tbl_int = soup.find("table", id="tblInteressados")
    if tbl_int and isinstance(tbl_int, Tag):
        for tr in tbl_int.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if tds:
                v = tds[0].get_text(" ", strip=True)
                if v:
                    interessados.append(v)
    if not interessados and "interessados" in result:
        interessados = [str(result.pop("interessados"))]
    result["interessados"] = interessados

    # -- sobrestamento: campo "Sobrestado" ou tabela tblSobrestamento --
    sobrestamentos: list[dict] = []
    tbl_sob = soup.find("table", id="tblSobrestamento")
    if tbl_sob and isinstance(tbl_sob, Tag):
        for tr in tbl_sob.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) >= 2:  # noqa: PLR2004
                sobrestamentos.append(
                    {
                        "motivo": tds[0].get_text(" ", strip=True),
                        "data": tds[1].get_text(" ", strip=True),
                    }
                )
    result["sobrestamentos"] = sobrestamentos

    return result
