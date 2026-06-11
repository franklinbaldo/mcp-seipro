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
import warnings
from typing import Any, Optional
from urllib.parse import parse_qsl, urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


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

    def __init__(self, **kwargs: Any) -> None:
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
        self._senha = kwargs.get("sei_senha", os.environ.get("SEI_SENHA", ""))
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

        verify_ssl = kwargs.get(
            "sei_verify_ssl", os.environ.get("SEI_VERIFY_SSL", "true")
        )
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
        self._inbox_url: Optional[httpx.URL] = None
        # cache do form principal de procedimento_controlar (action + hidden fields)
        self._form_action: Optional[str] = None
        self._form_hidden: dict[str, str] = {}
        # cache de URLs de processos individuais (protocolo → href pré-assinado)
        self._trabalhar_links: dict[str, str] = {}
        # URL do form de pesquisa rápida (protocolo_pesquisa_rapida + infra_hash)
        self._pesquisa_rapida_action: Optional[str] = None

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------

    async def login(self) -> None:
        """Faz login via formulário SIP e captura a inbox URL com infra_hash."""
        resp = await self._http.get(self.login_url)
        if resp.status_code != 200:
            raise RuntimeError(f"GET login.php retornou {resp.status_code}")

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
            raise RuntimeError("CAPTCHA presente no login — abortando.")
        if 'name="txtCodigo2FA"' in html or 'id="txtCodigo2FA"' in html:
            raise RuntimeError("2FA solicitado no login — não suportado.")

        soup = BeautifulSoup(html, "html.parser")
        usuario_input = soup.find("input", attrs={"name": "txtUsuario"})
        if usuario_input is None:
            raise RuntimeError("Campo txtUsuario não encontrado na página de login.")
        login_form = usuario_input.find_parent("form")
        if login_form is None:
            raise RuntimeError("<form> do login não encontrado.")

        sel_orgao = self._descobrir_sel_orgao(login_form, soup)

        form: dict[str, str] = {
            "txtUsuario": self._usuario,
            "pwdSenha": self._senha,
            "selOrgao": sel_orgao,
        }
        for h in login_form.find_all("input", type="hidden"):
            name = h.get("name")
            if name and h.get("value") is not None:
                form[name] = h["value"]

        # O PHP exige o par name=value do botão submit; sem ele ignora o POST.
        # Detecta o botão real do formulário (varia por instância:
        # sbmLogin=Acessar no ANTAQ, sbmAcessar=ACESSAR no RO, etc.)
        submit_btn = login_form.find("button", type="submit") or login_form.find(
            "input", type="submit"
        )
        if submit_btn:
            btn_name = submit_btn.get("name")
            if btn_name:
                btn_value = (
                    submit_btn.get("value")
                    or submit_btn.get_text(strip=True)
                    or "Acessar"
                )
                form[btn_name] = btn_value
        else:
            # fallback para instâncias mais antigas
            form["sbmLogin"] = "Acessar"

        # Corrige hdnAcao: o JS seta o valor correto antes de submeter via
        # acaoLogin(N) no onsubmit. Ex: onsubmit="return acaoLogin(2);"
        # O HTML tem value="1" (padrão), mas ação=2 é o login com usuário/senha.
        onsubmit = login_form.get("onsubmit", "")
        m_acao = re.search(r"acaoLogin\((\d+)\)", onsubmit)
        if m_acao and "hdnAcao" in form:
            form["hdnAcao"] = m_acao.group(1)
        sel_ctx = login_form.find("select", attrs={"name": "selContexto"})
        if sel_ctx is not None:
            ctx_val = ""
            for opt in sel_ctx.find_all("option"):
                if opt.get("selected") is not None:
                    ctx_val = opt.get("value") or ""
                    break
            form["selContexto"] = ctx_val

        action = login_form.get("action") or self.login_url
        post_url = urljoin(self.login_url, action)
        post_resp = await self._http.post(
            post_url,
            data=form,
            headers={"Referer": self.login_url, "Origin": self.sei_root},
        )
        if post_resp.status_code != 200:
            raise RuntimeError(f"POST login retornou {post_resp.status_code}")

        # após follow_redirects, resp.url é a URL final da cadeia
        # sip/login → sei/inicializar.php → sei/controlador.php?acao=procedimento_controlar
        final_url = post_resp.url
        qs = dict(
            parse_qsl(
                final_url.query.decode()
                if isinstance(final_url.query, bytes)
                else final_url.query
            )
        )
        if qs.get("acao") != "procedimento_controlar" or "infra_hash" not in qs:
            body = post_resp.text
            if 'name="txtUsuario"' in body or 'id="txtUsuario"' in body:
                raise RuntimeError(
                    "Login falhou: o servidor retornou a página de login novamente. "
                    "Verifique credenciais."
                )
            raise RuntimeError(f"URL inesperada após login: {final_url}")

        self._inbox_url = final_url
        # popula cache do form principal e dos links de processos a partir
        # da própria resposta do post-login (já contém o HTML da inbox)
        self._extract_main_form(post_resp.text)
        self._extract_pesquisa_rapida(post_resp.text)
        self._populate_trabalhar_links(post_resp.text)
        logger.info("SEI web login bem-sucedido — inbox capturada")

    def _descobrir_sel_orgao(self, login_form, soup) -> str:
        """Descobre o value do <select selOrgao> que corresponde ao órgão.

        Estratégia: option já selecionado → option com texto contendo a sigla
        do órgão → primeiro option não-vazio.
        """
        sel = login_form.find("select", attrs={"name": "selOrgao"})
        if sel is None:
            sel = soup.find("select", attrs={"name": "selOrgao"})
        if sel is None:
            raise RuntimeError("<select name='selOrgao'> não encontrado")

        # 1) option já selecionado
        for opt in sel.find_all("option"):
            if (
                opt.get("selected") is not None
                and opt.get("value")
                and opt.get("value") != "null"
            ):
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
        raise RuntimeError("Nenhum <option> válido em selOrgao.")

    def _extract_pesquisa_rapida(self, html: str) -> None:
        """Captura a action do form de pesquisa rápida (protocolo_pesquisa_rapida)."""
        soup = BeautifulSoup(html, "html.parser")
        for f in soup.find_all("form"):
            action = f.get("action") or ""
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
            action = f.get("action") or ""
            if "procedimento_controlar" in action:
                self._form_action = action.replace("&amp;", "&")
                self._form_hidden = {}
                for h in f.find_all("input", type="hidden"):
                    name = h.get("name")
                    if name:
                        self._form_hidden[name] = h.get("value", "") or ""
                return

    def _populate_trabalhar_links(self, inbox_html: str) -> None:
        """Mapeia protocolo → URL pré-assinada de procedimento_trabalhar.

        Sem isso não conseguimos navegar para um processo específico —
        a infra_hash é gerada server-side e não pode ser reconstruída.
        """
        soup = BeautifulSoup(inbox_html, "html.parser")
        for a in soup.find_all("a", href=re.compile(r"acao=procedimento_trabalhar")):
            txt = a.get_text(strip=True)
            href = a.get("href", "").replace("&amp;", "&")
            if txt and href:
                self._trabalhar_links.setdefault(txt, href)

    # ------------------------------------------------------------------
    # Listar processos (Controle de Processos / inbox)
    # ------------------------------------------------------------------

    async def fetch_inbox(
        self,
        detalhada: bool = True,
        pagina: int = 0,
        apenas_meus: bool = False,
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
        if self._inbox_url is None:
            raise RuntimeError("login() não foi chamado antes de fetch_inbox().")

        # Caso simples: GET inicial sem detalhada/filtros/paginação
        if (
            not detalhada
            and pagina == 0
            and not apenas_meus
            and self._form_action is None
        ):
            resp = await self._http.get(
                self._inbox_url,
                headers={"Referer": str(self._inbox_url)},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"fetch_inbox status={resp.status_code}")
            self._extract_main_form(resp.text)
            self._populate_trabalhar_links(resp.text)
            return len(resp.content), resp.text

        # Precisa do form action — fetch inicial se ainda não temos
        if self._form_action is None:
            seed = await self._http.get(
                self._inbox_url,
                headers={"Referer": str(self._inbox_url)},
            )
            if seed.status_code != 200:
                raise RuntimeError(f"seed inbox status={seed.status_code}")
            self._extract_main_form(seed.text)
            if self._form_action is None:
                raise RuntimeError(
                    "Form principal de procedimento_controlar não encontrado"
                )

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
        if resp.status_code != 200:
            raise RuntimeError(f"fetch_inbox POST status={resp.status_code}")

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
        if self._inbox_url is None:
            raise RuntimeError("login() não foi chamado")

        if self._pesquisa_rapida_action is None:
            await self.fetch_inbox(detalhada=False)
            if self._pesquisa_rapida_action is None:
                raise RuntimeError(
                    "Form de pesquisa rápida não encontrado no HTML da inbox"
                )

        post_url = urljoin(str(self._inbox_url), self._pesquisa_rapida_action)
        r = await self._http.post(
            post_url,
            data={"txtPesquisaRapida": protocolo},
            headers={"Referer": str(self._inbox_url)},
        )
        if r.status_code != 200:
            raise RuntimeError(f"pesquisa_rapida status={r.status_code}")

        final_url = str(r.url)
        sei_base = f"{self.sei_root}/sei/"

        if "procedimento_trabalhar" in final_url:
            # Redirecionou direto para o processo
            href = (
                final_url.replace(sei_base, "")
                if final_url.startswith(sei_base)
                else final_url
            )
            self._trabalhar_links[protocolo] = href
            return

        # Página de resultados (protocolo_pesquisar) — busca o link correto
        soup = BeautifulSoup(r.text, "html.parser")
        proto_norm = protocolo.replace(" ", "")
        for a in soup.find_all("a", href=re.compile(r"procedimento_trabalhar")):
            txt = a.get_text(strip=True).replace(" ", "")
            if proto_norm in txt:
                href = a.get("href", "").replace("&amp;", "&")
                self._trabalhar_links[protocolo] = href
                return

        # Tenta também via links com id_procedimento (tooltip ou linha da tabela)
        for a in soup.find_all("a", href=re.compile(r"procedimento_trabalhar")):
            href = a.get("href", "").replace("&amp;", "&")
            self._trabalhar_links[protocolo] = href
            return

        raise RuntimeError(
            f"Processo {protocolo!r} não encontrado na pesquisa. "
            "Verifique se o número está correto e se você tem acesso."
        )

    async def consultar_processo(self, protocolo_formatado: str) -> dict:
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
        if self._inbox_url is None:
            raise RuntimeError("login() não foi chamado antes de consultar_processo()")

        # garante que o protocolo está no cache de links da inbox
        if protocolo_formatado not in self._trabalhar_links:
            await self.fetch_inbox(detalhada=False)
        if protocolo_formatado not in self._trabalhar_links:
            # processo fora da caixa — usa pesquisa rápida
            await self.pesquisar_processo(protocolo_formatado)

        trab_url = urljoin(
            str(self._inbox_url), self._trabalhar_links[protocolo_formatado]
        )

        # Step 1: procedimento_trabalhar.php (frameset, leve)
        r1 = await self._http.get(trab_url, headers={"Referer": str(self._inbox_url)})
        if r1.status_code != 200:
            raise RuntimeError(f"procedimento_trabalhar status={r1.status_code}")

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
            raise RuntimeError("ifrArvore não encontrado no frameset")
        arvore_src = ifr.get("src", "").replace("&amp;", "&")
        arvore_url = urljoin(str(r1.url), arvore_src)

        # extrai id_procedimento da URL do trabalhar
        m_id = re.search(r"id_procedimento=(\d+)", str(r1.url))
        id_proc = m_id.group(1) if m_id else None

        # Step 2: procedimento_visualizar (arvore_montar.php)
        r2 = await self._http.get(arvore_url, headers={"Referer": trab_url})
        if r2.status_code != 200:
            raise RuntimeError(f"procedimento_visualizar status={r2.status_code}")

        nos = parse_arvore_nos(r2.text)
        arvore_html = r2.text

        # Step 3: Se houver PASTA colapsadas, fetch novamente com abrir_pastas=1
        has_collapsed = len(nos) > 1 and any(
            n.get("tipo_no") == "PASTA" for n in nos[1:]
        )
        if has_collapsed:
            arvore_url_str = str(arvore_url)
            if "abrir_pastas=" not in arvore_url_str:
                sep = "&" if "?" in arvore_url_str else "?"
                arvore_url_expandida = f"{arvore_url_str}{sep}abrir_pastas=1"
            else:
                arvore_url_expandida = re.sub(
                    r"abrir_pastas=0", "abrir_pastas=1", arvore_url_str
                )

            r3 = await self._http.get(
                arvore_url_expandida, headers={"Referer": trab_url}
            )
            if r3.status_code == 200:
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
                if n.get("tipo_no") not in ("PASTA",)
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

    async def _gerar_arquivo_processo(
        self, protocolo_formatado: str, acao: str
    ) -> bytes:
        """Helper compartilhado para gerar_pdf_processo e gerar_zip_processo.

        Fluxo de 5 etapas (igual para PDF e ZIP):
        1. procedimento_trabalhar → frameset com ifrArvore
        2. arvore_montar → busca link da ação (procedimento_gerar_pdf/zip)
        3. GET form de opções
        4. POST com hdnFlagGerar=1 → HTML com ifrDownload.src
        5. GET exibir_arquivo → bytes do arquivo
        """

        def _find_link(proto: str) -> Optional[str]:
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
        if r1.status_code != 200:
            raise RuntimeError(f"trabalhar status={r1.status_code}")

        if 'name="txtUsuario"' in r1.text or 'id="txtUsuario"' in r1.text:
            self._form_action = None
            self._form_hidden = {}
            await self.login()
            return await self._gerar_arquivo_processo(protocolo_formatado, acao)

        soup_fs = BeautifulSoup(r1.text, "html.parser")
        ifr = soup_fs.find("iframe", id="ifrArvore")
        if not ifr:
            raise RuntimeError("ifrArvore não encontrado no frameset")
        arvore_url = urljoin(str(r1.url), ifr.get("src", "").replace("&amp;", "&"))

        r2 = await self._http.get(arvore_url, headers={"Referer": trab_url})
        if r2.status_code != 200:
            raise RuntimeError(f"arvore status={r2.status_code}")

        m_link = re.search(
            rf"(controlador\.php\?acao={re.escape(acao)}[^\"'\s]*infra_hash=[a-f0-9]+)",
            r2.text,
        )
        if not m_link:
            raise RuntimeError(f"Link {acao} não encontrado na árvore")

        sei_base = f"{self.sei_root}/sei/"
        form_url = urljoin(sei_base, m_link.group(1).replace("&amp;", "&"))

        r3 = await self._http.get(form_url, headers={"Referer": str(r2.url)})
        if r3.status_code != 200:
            raise RuntimeError(f"form {acao} status={r3.status_code}")

        soup3 = BeautifulSoup(r3.content.decode("iso-8859-1", "replace"), "html.parser")
        form = soup3.find("form", id=re.compile(r"frmProcedimento(Pdf|Zip)", re.I))
        if not form:
            raise RuntimeError("Formulário frmProcedimento(Pdf|Zip) não encontrado")
        form_action = form.get("action", "").replace("&amp;", "&")
        post_url = urljoin(str(r3.url), form_action)

        post_data: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            if name:
                post_data[name] = inp.get("value", "") or ""
        post_data["rdoTipo"] = "T"
        post_data["hdnFlagGerar"] = "1"

        r4 = await self._http.post(
            post_url,
            data=post_data,
            headers={"Referer": str(r3.url)},
            timeout=httpx.Timeout(180.0, connect=10.0),
        )
        if r4.status_code != 200:
            raise RuntimeError(f"POST {acao} status={r4.status_code}")

        body4 = r4.content.decode("iso-8859-1", "replace")
        m_dl = re.search(
            r"getElementById\(['\"]ifrDownload['\"]\)\.src\s*=\s*'([^']+)'",
            body4,
        )
        if not m_dl:
            raise RuntimeError(
                f"URL de download (ifrDownload.src) não encontrada após {acao}. "
                "O processo pode não ter documentos disponíveis."
            )

        download_url = urljoin(sei_base, m_dl.group(1).replace("&amp;", "&"))

        r5 = await self._http.get(download_url, headers={"Referer": str(r4.url)})
        if r5.status_code != 200:
            raise RuntimeError(f"download {acao} status={r5.status_code}")

        return r5.content

    async def gerar_pdf_processo(self, protocolo_formatado: str) -> bytes:
        """Gera e baixa o PDF consolidado de um processo SEI.

        Usa o mesmo endpoint do botão "Gerar PDF" da interface web.
        Retorna os bytes brutos do PDF.
        """
        if self._inbox_url is None:
            raise RuntimeError("login() não foi chamado")
        content = await self._gerar_arquivo_processo(
            protocolo_formatado, "procedimento_gerar_pdf"
        )
        if "pdf" not in self._http.headers.get("accept", "").lower():
            pass  # conteúdo válido independente do accept
        if not content.startswith(b"%PDF") and b"pdf" not in content[:32].lower():
            ct = "(desconhecido)"
            raise RuntimeError(f"Esperado PDF mas recebeu Content-Type: {ct}")
        return content

    async def gerar_zip_processo(self, protocolo_formatado: str) -> bytes:
        """Gera e baixa o ZIP com todos os documentos de um processo SEI.

        Usa o mesmo endpoint do botão "Gerar ZIP" da interface web.
        Retorna os bytes brutos do arquivo ZIP.
        """
        if self._inbox_url is None:
            raise RuntimeError("login() não foi chamado")
        return await self._gerar_arquivo_processo(
            protocolo_formatado, "procedimento_gerar_zip"
        )

    async def listar_atividades(self, protocolo_formatado: str) -> dict:
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
        if self._inbox_url is None:
            raise RuntimeError("login() não foi chamado")

        # garante que o protocolo está no cache
        if protocolo_formatado not in self._trabalhar_links:
            await self.fetch_inbox(detalhada=False)
        if protocolo_formatado not in self._trabalhar_links:
            await self.pesquisar_processo(protocolo_formatado)

        trab_url = urljoin(
            str(self._inbox_url), self._trabalhar_links[protocolo_formatado]
        )

        # frameset → arvore
        r1 = await self._http.get(trab_url, headers={"Referer": str(self._inbox_url)})
        if r1.status_code != 200:
            raise RuntimeError(f"trabalhar status={r1.status_code}")
        soup_fs = BeautifulSoup(r1.text, "html.parser")
        ifr = soup_fs.find("iframe", id="ifrArvore")
        if not ifr:
            raise RuntimeError("ifrArvore não encontrado")
        arvore_url = urljoin(str(r1.url), ifr.get("src", "").replace("&amp;", "&"))

        m_id = re.search(r"id_procedimento=(\d+)", str(r1.url))
        id_proc = m_id.group(1) if m_id else ""

        # fetch arvore para pegar o link do histórico
        r2 = await self._http.get(arvore_url, headers={"Referer": trab_url})
        if r2.status_code != 200:
            raise RuntimeError(f"arvore status={r2.status_code}")

        m_hist = re.search(
            r"(controlador\.php\?acao=procedimento_consultar_historico[^\"']*infra_hash=[a-f0-9]+)",
            r2.text,
        )
        if not m_hist:
            raise RuntimeError(
                "Link procedimento_consultar_historico não encontrado na árvore"
            )
        hist_url = urljoin(str(r2.url), m_hist.group(1).replace("&amp;", "&"))

        # fetch histórico
        r3 = await self._http.get(hist_url, headers={"Referer": str(r2.url)})
        if r3.status_code != 200:
            raise RuntimeError(f"histórico status={r3.status_code}")

        soup_h = BeautifulSoup(r3.text, "html.parser")
        tbl = soup_h.find("table", id="tblHistorico")
        andamentos: list[dict[str, str]] = []
        if tbl:
            for tr in tbl.find_all("tr")[1:]:  # pula header
                tds = tr.find_all("td")
                if len(tds) >= 4:
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

    async def incluir_documento_externo(
        self,
        protocolo_formatado: str,
        arquivo_path: str,
        nome_arquivo: Optional[str] = None,
        id_serie: Optional[str] = None,
        data_elaboracao: str = "",
        nivel_acesso: str = "0",
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
        import mimetypes
        import os as _os
        from datetime import date as _date

        if self._inbox_url is None:
            raise RuntimeError("login() não foi chamado")

        if protocolo_formatado not in self._trabalhar_links:
            await self.fetch_inbox(detalhada=False)
        if protocolo_formatado not in self._trabalhar_links:
            await self.pesquisar_processo(protocolo_formatado)

        trab_url = urljoin(
            str(self._inbox_url), self._trabalhar_links[protocolo_formatado]
        )

        # --- Step 1: trabalhar → frameset ---
        r1 = await self._http.get(trab_url, headers={"Referer": str(self._inbox_url)})
        if r1.status_code != 200:
            raise RuntimeError(f"trabalhar status={r1.status_code}")
        if 'name="txtUsuario"' in r1.text or 'id="txtUsuario"' in r1.text:
            self._form_action = None
            await self.login()
            return await self.incluir_documento_externo(
                protocolo_formatado,
                arquivo_path,
                nome_arquivo,
                id_serie,
                data_elaboracao,
            )

        soup_fs = BeautifulSoup(r1.text, "html.parser")
        ifr = soup_fs.find("iframe", id="ifrArvore")
        if not ifr:
            raise RuntimeError("ifrArvore não encontrado no frameset")
        arvore_url = urljoin(str(r1.url), ifr.get("src", "").replace("&amp;", "&"))

        # --- Step 2: arvore_montar → Nos[0].acoes ---
        r2 = await self._http.get(arvore_url, headers={"Referer": str(r1.url)})
        if r2.status_code != 200:
            raise RuntimeError(f"arvore status={r2.status_code}")

        acoes_html = ""
        for pat in (
            r"Nos\[0\]\.acoes\s*=\s*'((?:[^'\\]|\\.)*)'",
            r'Nos\[0\]\.acoes\s*=\s*"((?:[^"\\]|\\.)*)"',
        ):
            m = re.search(pat, r2.text, re.S)
            if m:
                acoes_html = (
                    m.group(1)
                    .replace("\\'", "'")
                    .replace('\\"', '"')
                    .replace("\\\\", "\\")
                )
                break

        if not acoes_html:
            raise RuntimeError(
                "Nos[0].acoes não encontrado — o processo pode estar concluído "
                "ou você não tem permissão para incluir documentos nele."
            )

        sei_base = f"{self.sei_root}/sei/"
        soup_acoes = BeautifulSoup(acoes_html, "html.parser")
        incluir_href: Optional[str] = None
        for a in soup_acoes.find_all("a", href=re.compile(r"documento_escolher_tipo")):
            incluir_href = a.get("href", "").replace("&amp;", "&")
            break
        if not incluir_href:
            for img in soup_acoes.find_all("img"):
                if "Incluir" in (img.get("title", "") or "") or "incluir" in (
                    img.get("src", "") or ""
                ):
                    pa = img.find_parent("a")
                    if pa:
                        incluir_href = pa.get("href", "").replace("&amp;", "&")
                        break

        if not incluir_href:
            raise RuntimeError(
                "Link 'Incluir Documento' não encontrado nas ações do processo. "
                "O processo pode estar concluído, sem tramitação para esta unidade, "
                "ou você não tem permissão. Tente reabrir o processo primeiro."
            )

        # --- Step 3: GET documento_escolher_tipo ---
        escolher_url = urljoin(sei_base, incluir_href)
        r3 = await self._http.get(escolher_url, headers={"Referer": str(r2.url)})
        if r3.status_code != 200:
            raise RuntimeError(f"documento_escolher_tipo status={r3.status_code}")

        body3 = r3.content.decode("iso-8859-1", "replace")
        soup3 = BeautifulSoup(body3, "html.parser")
        form3 = soup3.find("form", id="frmDocumentoEscolherTipo")
        if not form3:
            raise RuntimeError("frmDocumentoEscolherTipo não encontrado")
        form3_action = form3.get("action", "").replace("&amp;", "&")
        post3_url = urljoin(str(r3.url), form3_action)

        # --- Step 4: POST escolher com hdnIdSerie=-1 → documento_receber ---
        post3_data: dict[str, str] = {}
        for inp in form3.find_all("input", type="hidden"):
            n = inp.get("name")
            if n:
                post3_data[n] = inp.get("value", "") or ""
        post3_data["hdnIdSerie"] = "-1"

        r4 = await self._http.post(
            post3_url, data=post3_data, headers={"Referer": str(r3.url)}
        )
        if r4.status_code != 200:
            raise RuntimeError(f"POST escolher_tipo status={r4.status_code}")

        body4 = r4.content.decode("iso-8859-1", "replace")

        # --- Step 5: Parse documento_receber ---
        # Validação de página: infraUpload deve estar presente no JS
        if "infraUpload" not in body4 and "frmDocumentoCadastro" not in body4:
            raise RuntimeError(
                "documento_receber não encontrado — verifique o processo e as permissões"
            )

        # parse frmDocumentoCadastro
        soup4 = BeautifulSoup(body4, "html.parser")
        form4 = soup4.find("form", id="frmDocumentoCadastro")
        if not form4:
            raise RuntimeError(
                "frmDocumentoCadastro não encontrado em documento_receber"
            )
        form4_action = form4.get("action", "").replace("&amp;", "&")
        post4_url = urljoin(str(r4.url), form4_action)

        form4_data: dict[str, str] = {}
        for inp in form4.find_all("input", type="hidden"):
            n = inp.get("name")
            if n:
                form4_data[n] = inp.get("value", "") or ""

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
        nome = nome_arquivo or _os.path.basename(arquivo_path)
        mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
        with open(arquivo_path, "rb") as f:
            file_bytes = f.read()

        tam_int = len(file_bytes)
        if tam_int < 1024:
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
            raise RuntimeError(
                "URL de upload (infraUpload) não encontrada em documento_receber"
            )
        upload_url = urljoin(str(r4.url), m_up.group(1).replace("&amp;", "&"))

        r5 = await self._http.post(
            upload_url,
            files={"filArquivo": (nome, file_bytes, mime)},
            headers={"Referer": str(r4.url)},
        )
        if r5.status_code != 200:
            raise RuntimeError(f"upload status={r5.status_code}: {r5.text[:200]}")

        # Resposta: nome_upload#nome#mime#tamanho#data_hora#
        up_parts = r5.text.strip().rstrip("#").split("#")
        if len(up_parts) < 2:
            raise RuntimeError(f"Resposta de upload inesperada: {r5.text!r}")
        nome_upload = up_parts[0]
        upload_dh = up_parts[4] if len(up_parts) > 4 else ""
        upload_tam = up_parts[3] if len(up_parts) > 3 else str(tam_int)

        # Extrai usuario e unidade da linha JS:
        # objTabelaAnexos.adicionar([arr[...], ..., 'CPF', 'SIGLA'])
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
        import urllib.parse as _up

        _SEP = "%B1"  # ± URL-encoded como ISO-8859-1 (PHP split target)

        def _qpart(s: str) -> str:
            return _up.quote(s.replace(" ", "+"), safe="+-.!~*'()_")

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
        form4_data["txtDataElaboracao"] = data_elaboracao or _date.today().strftime(
            "%d/%m/%Y"
        )
        form4_data["hdnStaNivelAcessoLocal"] = nivel_acesso
        form4_data["rdoNivelAcesso"] = nivel_acesso
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
        if r6.status_code != 200:
            raise RuntimeError(f"POST frmDocumentoCadastro status={r6.status_code}")

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
            scripts6 = re.findall(
                r"<script[^>]*>(.*?)</script>", body6, re.DOTALL | re.IGNORECASE
            )
            for sc in scripts6:
                if "alert(" in sc:
                    m_alert = re.search(r"alert\(['\"]([^'\"]+)['\"]", sc)
                    if m_alert:
                        erros.append(m_alert.group(1))
            msg = "; ".join(erros) if erros else f"URL final inesperada: {final_url}"
            raise RuntimeError(f"Falha ao incluir documento: {msg}")

        m_id = re.search(r"id_documento=(\d+)", final_url)
        id_doc = m_id.group(1) if m_id else ""
        return {
            "sucesso": True,
            "id_documento": id_doc,
            "url_final": final_url,
            "nome_arquivo": nome,
            "tamanho": tamanho_fmt,
        }

    async def listar_processos(
        self,
        detalhada: bool = True,
        pagina: int = 0,
        apenas_meus: bool = False,
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
            total_servidor = int(
                self._form_hidden.get("hdnDetalhadoNroItens", "0") or "0"
            )
        else:
            total_servidor = int(
                self._form_hidden.get("hdnRecebidosNroItens", "0") or "0"
            ) + int(self._form_hidden.get("hdnGeradosNroItens", "0") or "0")
        if total_servidor == 0:
            total_servidor = len(rows)

        # Filtros client-side: aplicados após o parse, sobre os rows.
        rows_filtrados = rows
        if tipo:
            tipo_lower = tipo.lower()
            rows_filtrados = [
                r
                for r in rows_filtrados
                if tipo_lower in (r.get("Tipo", "") or "").lower()
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
            "tem_proxima": len(rows) > 0
            and (pagina + 1) * max(len(rows), 1) < total_servidor,
            "layout": layout,
        }


# ---------------------------------------------------------------------------
# Parsers de HTML (independentes de instância)
# ---------------------------------------------------------------------------


def parse_arvore_nos(html: str) -> list[dict]:
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
        re.S,
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
            if (s.startswith('"') and s.endswith('"')) or (
                s.startswith("'") and s.endswith("'")
            ):
                return s[1:-1]
            return s

        if len(args) >= 7:
            out.append(
                {
                    "tipo_no": unquote(args[0]),
                    "id": unquote(args[1]),
                    "pai": unquote(args[2]),
                    "link": unquote(args[3]),
                    "target": unquote(args[4]),
                    "label": unquote(args[5]),
                    "tooltip": unquote(args[6]),
                    "icone": unquote(args[7]) if len(args) > 7 else "",
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


def _extract_tooltip(link_tag, row: dict) -> None:
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


def parse_inbox(html: str) -> tuple[str, list[dict]]:
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
                    {0: "_check", 1: "icones", 2: "_processo", 3: "atribuicao"}.get(
                        i, f"col{i}"
                    )
                )

        for tr in tbl.find_all("tr", id=re.compile(r"^P\d+$")):
            tds = tr.find_all("td", recursive=False)
            row: dict[str, Any] = {"id_procedimento": tr["id"][1:]}
            link = tr.find("a", href=re.compile(r"acao=procedimento_trabalhar"))
            if link is not None:
                row["protocolo"] = link.get_text(" ", strip=True)
                # Especificação + tipo estão no tooltip do link do processo:
                # onmouseover="return infraTooltipMostrar('Especificação','Tipo')"
                # Disponível INDEPENDENTE de a coluna estar habilitada no painel.
                _extract_tooltip(link, row)
            if len(tds) >= 2:
                icones = []
                for img in tds[1].find_all("img"):
                    title = img.get("title") or img.get("alt") or ""
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
            if len(tds) >= 2:
                icones = []
                for img in tds[1].find_all("img"):
                    title = img.get("title") or img.get("alt") or ""
                    if title:
                        icones.append(title.strip())
                if icones:
                    row["icones"] = icones
            if len(tds) >= 4:
                atrib_text = _RE_PARENS.sub(
                    "", tds[-1].get_text(" ", strip=True)
                ).strip()
                if atrib_text:
                    row["atribuicao"] = atrib_text
            rows.append(row)

    if found_any:
        return ("resumida", rows)
    return ("desconhecido", [])
