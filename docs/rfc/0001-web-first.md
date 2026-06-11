# RFC 0001 — Web-first: paridade funcional para instâncias SEI sem mod-wssei

**Status**: Proposta
**Data**: 2026-06-11
**Autores**: Franklin Baldo (com Claude Code)

## 1. Problema

O todos hoje é REST-first: ~105 das 116 tools dependem da API mod-wssei v2.
Em instâncias que não têm o módulo instalado — caso real: **SEI-RO**
(`sei.sistemas.ro.gov.br`, 404 em `/modulos/wssei/.../autenticar`) — essas tools
falham na autenticação antes de qualquer operação.

O mod-wssei é um módulo opcional. A maioria dos órgãos estaduais e municipais
não o instala. O frontend web, por outro lado, **existe em 100% das instâncias**
— é o próprio SEI. Quem scrapeia o frontend atende qualquer instância.

A prova de conceito já existe no projeto: 8 tools usam o `SEIWebClient`
(listar_processos 23× mais rápido, arvore 10×, upload de documento externo
funcionando em SEI-RO onde o REST nem autentica, além de gerar_pdf_processo
e gerar_zip_processo — scraping dos forms `frmProcedimentoPdf/Zip`).

## 2. Objetivo

Tornar o web scraper um **backend completo**, não um acelerador do REST:

1. Toda tool de uso cotidiano funciona em instância sem mod-wssei.
2. O REST passa a ser otimização opcional (quando disponível), não dependência.
3. Detecção automática de capacidade: o servidor descobre no startup o que a
   instância suporta e roteia cada tool para o melhor backend.

Fora de escopo: assinatura digital (PKI no navegador), pesquisa Solr completa,
funções administrativas (órgãos, contextos).

## 3. Fundamento técnico — o padrão universal do SEI

Tudo que aprendemos implementando `incluir_documento_externo` (e validado
contra o código da extensão [SEI Pro](https://github.com/SEI-Pro/sei-pro), que
faz scraping client-side há anos em dezenas de instâncias) converge para um
único padrão:

```
1. GET procedimento_trabalhar?id_procedimento=X   (URL pré-assinada da inbox/pesquisa)
2. GET arvore (iframe ifrArvore) → extrair Nos[0].acoes (JS com HTML embutido)
3. Achar o link da ação desejada pelo padrão acao=<acao> no href
   (o link já vem assinado com infra_hash válido para a sessão)
4. GET a página do form
5. POST o form com hidden fields + campos preenchidos
6. Validar sucesso pela URL final do redirect (ex.: acao=arvore_visualizar)
```

A diferença entre "concluir processo" e "enviar processo" é só **qual `acao=`
buscar e quais campos preencher**. O `SEIWebClient` já implementa os passos
1–2; falta generalizar 3–6.

### Invariantes descobertos (hard-won, documentar para não redescobrir)

| Invariante | Detalhe |
|---|---|
| Botão submit no login | O PHP exige o par `name=value` do botão submit no POST; sem ele ignora o form silenciosamente. O nome **varia por instância** (`sbmLogin=Acessar` na ANTAQ, `sbmAcessar=ACESSAR` no SEI-RO) — detectar dinamicamente do form |
| Token CSRF dinâmico | `hdnToken<hash>` capturado do GET da página de login — nunca reutilizar entre sessões |
| `infra_hash` | sha256(params+secret) da sessão; links de `Nos[0].acoes` já vêm assinados; reutilizável entre chamadas da mesma sessão |
| Encoding | Backend é ISO-8859-1; decodificar com `iso-8859-1` e POSTar idem — UTF-8 corrompe acentos |
| `hdnFlagDocumentoCadastro` | JS `submeter()` muda `'1'→'2'` antes do submit; sem isso o POST é tratado como reload |
| `hdnAnexos` | Separador é `±` (U+00B1) URL-encoded como `%B1` (ISO-8859-1), **não** `#`; não pode ser duplo-codificado (o byte alto UTF-8 `%C2` quebra o split no PHP); campos: `nome_upload±nome±data_hora±tamanho±tamanho_fmt±cpf±sigla_unidade` |
| Radio buttons | `rdoNivelAcesso` e `rdoFormato` precisam ir no POST (browsers só enviam se checked) |
| Usuario/unidade p/ anexo | Extraídos do literal JS `objTabelaAnexos.adicionar([..., 'CPF', 'SIGLA'])` |
| Visualização Detalhada | Requer `hdnTipoVisualizacao=D` no POST do form `procedimento_controlar` |
| Sucesso de POST | Verificar `acao=arvore_visualizar` (ou equivalente) na URL final, não status 200 |

## 4. Proposta

### 4.1 Camada de roteamento por capacidade

```python
class SEIBackend:
    """Resolve qual implementação atende cada operação."""

    def __init__(self, rest: SEIClient | None, web: SEIWebClient) -> None:
        self._rest = rest
        self._web = web
        self._has_rest = rest is not None

    async def detect(self) -> None:
        # 1 chamada no startup: GET /api/v2/versao (ou /autenticar)
        # 404 → rest = None; tudo roteia para web
        ...

    async def listar_processos(self, ...) -> list[dict]:
        # web é sempre mais rápido para listagem
        return await self._web.listar_processos(...)

    async def incluir_documento_interno(self, ...) -> dict:
        if self._has_rest:
            return await self._rest.incluir_documento_interno(...)
        return await self._web.incluir_documento_interno(...)  # fase 3
```

Cada tool declara preferência: `prefer="web"`, `prefer="rest"`, ou
`prefer="hybrid"`. Quando o backend preferido não existe, cai no outro; quando
nenhum suporta, erro claro ("esta operação requer mod-wssei, não disponível
nesta instância").

### 4.2 Primitiva genérica de ação

O coração da proposta — um método único que executa o padrão universal:

```python
async def executar_acao_processo(
    self,
    protocolo: str,
    acao: str,                            # ex. "procedimento_concluir"
    campos: dict[str, str] | None = None, # campos do form a sobrescrever
    confirmar: bool = True,               # False = dry-run (retorna campos sem POSTar)
) -> dict:
    """trabalhar → arvore → Nos[0].acoes → link acao= → GET form → POST."""
    ...
```

Na primitiva interna, `confirmar=True` é o default (os wrappers mapeados como
`concluir_processo` executam de verdade). Na **tool MCP genérica**
`sei_executar_acao`, o default inverte para `confirmar=False` (dry-run) — ver
Decisão 1 na §9.

`incluir_documento_externo` vira um caso especializado dessa primitiva
(precisa do upload em duas fases). As ações simples viram one-liners:

```python
async def concluir_processo(self, protocolo):
    return await self.executar_acao_processo(protocolo, "procedimento_concluir")
```

### 4.3 Cache de catálogos (substituto dos dropdowns SIP)

Tipos de documento, hipóteses legais, unidades etc. vêm de autocompletes SIP
no web — mas **também aparecem como `<select>` em forms** (ex.: `selSerie` com
415 opções em `documento_receber`). Estratégia:

- Extrair catálogos dos selects dos forms quando o fluxo passar por eles
  (custo zero) e cachear com TTL 24h em disco (`~/.cache/todos/`).
- `sei_pesquisar_tipos_documento` etc. consultam o cache; se vazio, fazem um
  GET do form que contém o select e populam.

### 4.4 Fases de implementação

**Fase 1 — fundações + ações simples** (1 semana)
- `scripts/smoke_web.py` (pré-requisito: é o critério de aceite de tudo abaixo)
- `SEIBackend` com detecção de capacidade no startup
- `executar_acao_processo` genérico
- Migrar: `concluir_processo`, `reabrir_processo`, `receber_processo`,
  `remover_atribuicao`, `dar_ciencia`
- Critério de aceite: as 5 tools funcionam em SEI-RO (via smoke test)

**Fase 2 — forms com campos** (1–2 semanas)
- `registrar_andamento` (txtDescricao)
- `criar_anotacao` (txaDescricao, selPrioridade)
- `marcar_processo` (selMarcador + texto)
- `sobrestar_processo` / `remover_sobrestamento`
- `atribuir_processo` (selUsuario — select simples no form, não autocomplete)
- `acompanhar_processo` / `remover_acompanhamento`

**Fase 3 — read scrapers** (1 semana)
- `consultar_documento_externo` (página documento_consultar)
- `listar_assinaturas`, `listar_ciencias` (tabelas na mesma página)
- `listar_unidades_processo`, `listar_interessados`, `listar_sobrestamentos`
- `baixar_anexo` / `ler_documento` via link `documento_download_anexo` da arvore

**Fase 4 — forms complexos** (2–4 semanas, sob demanda)
- `enviar_processo`: autocomplete de unidades via endpoint AJAX
  (`controlador_ajax.php?acao_ajax=unidade_auto_completar`)
- `criar_processo`: tipo via select, interessados via mesmo padrão AJAX
- `criar_documento` (interno): POST direto ao `documento_gerar`,
  pulando o CKEditor (o editor é só UI; o submit é um form normal com
  `txaEditor_*`). Requer engenharia reversa do form de `editor_montar`.

**Permanente — não migrar**
- Assinatura (PKI), cancelar assinatura, Solr (`pesquisar_processos`),
  admin (órgãos/contextos), sugestões de assunto.
- Em instância sem REST essas tools retornam erro explicativo com alternativa
  quando houver.

### 4.5 Robustez

- **Teste de fumaça por instância**: script `scripts/smoke_web.py` que roda
  login + 1 ação de cada categoria contra uma instância alvo, para validar
  compatibilidade antes de ativar (variações de versão SEI 4.0/4.1/5.0 mudam
  detalhes do HTML).
- **Versão do SEI**: extrair do rodapé do login (`v4.x.x`) e gatear parsers
  por versão quando divergirem.
- **Falha legível**: todo POST valida a URL final e extrai `alert()`/`infraMsg`
  do HTML de resposta para reportar o erro real do SEI (já implementado em
  `incluir_documento_externo` — extrair para helper `_extrair_erro_sei`).
- **Sessão**: re-login automático ao detectar `txtUsuario` na resposta (já
  implementado; manter no helper genérico).

## 5. Variáveis de ambiente

| Variável | Obrigatoriedade | Descrição |
|---|---|---|
| `SEI_URL` | Opcional | URL REST mod-wssei (ex: `https://sei.org.gov.br/sei/modulos/wssei/.../api/v2`). Quando presente, `sei_root` é derivado dela |
| `SEI_WEB_URL` | Obrigatória se `SEI_URL` ausente | Raiz web do SEI (ex: `https://sei.org.gov.br`). Tem precedência sobre a derivação de `SEI_URL` |
| `SEI_USUARIO` | Obrigatória | Usuário SEI/SIP |
| `SEI_SENHA` | Obrigatória | Senha SEI/SIP |
| `SEI_ORGAO` | Opcional (padrão: `0`) | ID do órgão na API REST |
| `SEI_SIGLA_ORGAO` | Opcional (padrão: `ANTAQ`) | Sigla do órgão no `selOrgao` do SIP |
| `SEI_SIGLA_SISTEMA` | Opcional (padrão: `SEI`) | Parâmetro `sigla_sistema` na URL de login SIP |
| `SEI_SIGLA_ORGAO_SISTEMA` | Opcional (padrão: `SEI_SIGLA_ORGAO`) | Parâmetro `sigla_orgao_sistema` na URL de login SIP (ex: `RO`) |
| `SEI_VERIFY_SSL` | Opcional (padrão: `true`) | Verificação de certificado SSL |

## 6. Alternativas consideradas

1. **Pedir instalação do mod-wssei ao órgão** — fora do nosso controle; meses
   ou nunca. Rejeitada como única via.
2. **Automação por navegador (Playwright)** — resolve JS de verdade (CKEditor,
   autocompletes), mas: pesado (Chromium por sessão), frágil em headless
   server, latência 5–10× maior que httpx. Mantida como **último recurso**
   para o que o scraping puro não alcançar.
3. **Implementar o protocolo SOAP legado (SeiWS)** — existe em mais instâncias
   que o mod-wssei, mas exige `SiglaSistema` + chave de integração concedidas
   pelo administrador do órgão. Vale investigar como **terceiro backend**
   (usuários que conseguem a chave ganham operações server-side estáveis),
   mas não substitui o web para o usuário comum.

## 7. Riscos

| Risco | Mitigação |
|---|---|
| HTML muda entre versões do SEI | Gate por versão + smoke test + parsers tolerantes (regex âncora em `acao=`, não em layout) |
| CAPTCHA/2FA no login | Já detectado e abortado com erro claro; sem workaround (correto) |
| Rate limiting / bloqueio | Sessão única persistente, sem paralelismo agressivo; comportamento idêntico a um usuário no navegador |
| Ações destrutivas erradas | Validação de protocolo feita antes do POST |
| Encoding corrompe acentos | Padrão ISO-8859-1 em todo o pipeline já estabelecido |

## 8. Métricas de sucesso

- **M1**: 100% das tools de fluxo cotidiano (listar, consultar, concluir,
  reabrir, andamento, ciência, anotação, marcador, upload externo, enviar)
  funcionando em SEI-RO.
- **M2**: zero regressão nas instâncias com REST (ANTAQ) — `smoke_web.py`
  passa contra a ANTAQ antes e depois de cada fase (não há suite de testes
  unitários no repo hoje; o smoke test é a linha de base).
- **M3**: tempo médio por operação web ≤ 2 s (hoje: 0.6–1.5 s nas migradas).

## 9. Decisões

1. **`sei_executar_acao` como tool MCP genérica: SIM, com dry-run por padrão.**
   `confirmar=False` é o default — a tool retorna os campos do form sem POSTar;
   o POST exige `confirmar=True` explícito na chamada. Equilibra o poder de
   alcançar ações não mapeadas com proteção contra POST destrutivo acidental.
   (Decidido em 2026-06-11.)
2. **Cache de catálogos: disco** (`~/.cache/todos/`, TTL 24h). O Claude Desktop
   reinicia o servidor MCP com frequência; memória obrigaria re-scrape de
   selects com 400+ opções a cada restart. (Decidido em 2026-06-11.)
3. **SOAP SeiWS: descoberta sim, invocação não (sem chave).** Esclarecimento
   importante: a autenticação SOAP (`SiglaSistema` + `IdentificacaoServico`) é
   **sistema-a-sistema**, validada por chamada contra o cadastro do SIP — é um
   plano de autenticação separado da sessão de usuário. **Login web não
   contorna a chave.** Porém o WSDL costuma ser servido sem autenticação (a
   chave só é validada ao invocar operações), então ele serve para
   **enumerar operações e schemas de parâmetros** — útil para mapear capacidade
   da instância. No SEI-RO, `/sei/ws/SeiWS.php` responde (endpoint existe);
   confirmar o WSDL com `curl` é o primeiro passo do spike. Plano: spike de
   descoberta (WSDL) após Fase 1; invocação como backend 3 só se/quando houver
   chave concedida pelo órgão.
