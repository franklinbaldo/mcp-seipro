# todos — Contexto para Claude Code

## O que é

**TOdos Domina O Sei** — MCP Server para o SEI (Sistema Eletrônico de Informações) com arquitetura web-first.
121 tools cobrindo processos, documentos, tramitação, assinatura, blocos, marcadores, acompanhamento, credenciamento, modelos e mais.
Opera via scraper HTTP do frontend web + REST mod-wssei v2 quando disponível. Funciona em qualquer instância SEI 4.0+ — inclusive sem mod-wssei instalado.

## Stack

- Python 3.11+, FastMCP (mcp SDK 1.12+), httpx, BeautifulSoup, markdownify, pdfplumber, pytesseract
- Transport: stdio (local) ou Streamable HTTP + OAuth (remoto/Railway)
- Configuração: variáveis de ambiente (SEI_URL opcional, SEI_USUARIO, SEI_SENHA, SEI_ORGAO)

## Arquivos principais

- `src/todos/server.py` — FastMCP server com 121 tools + helpers (_resolver_documento, _resolver_processo)
- `src/todos/sei_backend.py` — SEIBackend: wrapper que expõe `.rest` (SEIClient), `.web` (SEIWebClient), `.has_rest` — roteia para o backend adequado
- `src/todos/sei_client.py` — Cliente REST assíncrono para mod-wssei v2 (auth automática, auto-reauth 401/403, cache de metadados TTL 1h)
- `src/todos/sei_web_client.py` — Cliente HTTP scraper do frontend web do SEI (login SIP, sessão persistente, parser de inbox/árvore/histórico, upload de documentos externos)
- `src/todos/html_utils.py` — html_to_text, html_to_markdown, pdf_to_text, pdf_to_markdown (com OCR fallback), sanitize_iso8859
- `src/todos/sei_styles.py` — Dicionário de 39 estilos CSS do SEI + helpers (html_referencia_sei, html_destinatario)
- `tests/test_parsers.py` — Testes unitários dos parsers HTML (sem servidor SEI)

## Convenções importantes

### API do SEI
- O `protocoloFormatado` (número SEI que o usuário vê) é DIFERENTE do `id` interno do documento
- A pesquisa Solr (`/processo/pesquisar?palavrasChave=`) funciona em produção para resolver número SEI → processo → id
- Documentos recém-criados podem não estar indexados no Solr ainda
- Paginação usa `start` como número de PÁGINA (0-indexed), não offset
- `listar_usuarios` filtra por unidade com parâmetro `unidade={id}` (a API ignora `filter` para nomes)
- `assuntos` no `criar_processo` precisa ser JSON: `[{"id":"876"}]`
- `alterar_processo` exige TODOS os campos (busca dados atuais primeiro)
- Hipóteses legais com sufixo (S) = sigiloso, sem = restrito

### Estilos CSS do SEI para documentos
- Corpo de Despachos: `Paragrafo_Numerado_Nivel1` (autonumera 1. 2. 3.)
- Títulos de Notas Técnicas: `Item_Nivel1/2/3/4` (≈ H1/H2/H3/H4)
- Alíneas: `Item_Alinea_Letra` — NUNCA escrever a) b) no texto
- Incisos: `Item_Inciso_Romano` — NUNCA escrever I - II - no texto
- Destinatário: `Texto_Alinhado_Esquerda` com span `ancoraSei interessadoSeiPro data-id`
- Referências SEI: span `ancoraSei` com `id="lnkSei{id_documento}"`
- `sei_editar_secao` preenche seções somenteLeitura automaticamente

### Compatibilidade de versão do mod-wssei
- **Todos os 121 tools funcionam em qualquer SEI 4.0+** — os endpoints REST existem desde o mod-wssei 2.0.0
- Única exceção: `sei_listar_relacionamentos` (`GET /processo/{id}/relacionamentos`) requer **mod-wssei 3.0.2+** (SEI 5.0.x)
- Tabela de compatibilidade SEI ↔ mod-wssei:
  - SEI 4.0.x → mod-wssei 2.0.x (131 endpoints)
  - SEI 4.1.1 → mod-wssei 2.2.0 (131 endpoints, correções de bugs)
  - SEI 5.0.x → mod-wssei 3.0.1 (131 endpoints, compat PHP 8.2)
  - SEI 5.0.x → mod-wssei 3.0.2 (132 endpoints, +relacionamentos)
- Diferenças entre versões são majoritariamente correções de bugs e encoding, não endpoints novos
- v3.0.x corrigiu `iconv()` → `mb_convert_encoding()` para compatibilidade PHP 8.2
- v3.0.2 adicionou campo `dataHora` na resposta de `listar_assinaturas`
- Se um endpoint falhar com erro inesperado, usar `sei_versao` para verificar a versão instalada
- Funcionalidades que dependem do core SEI (ex: credenciamento) podem não funcionar se o órgão não habilitou processos sigilosos

### Arquitetura web-first
- **SEIWebClient** (`sei_web_client.py`) é o backend primário — faz login via formulário SIP, captura `infra_hash` da cadeia de redirects e mantém sessão persistente
- Login web requer enviar `sbmLogin=Acessar` (par name=value do botão submit) — sem ele o backend PHP ignora o POST silenciosamente
- O token CSRF é dinâmico (`hdnToken<hash>`) e precisa ser capturado do GET inicial da página de login
- `infra_hash` é `sha256(params + sessionSecret)` — válido enquanto a sessão SIP viver, reaproveitado entre chamadas
- Visualização Detalhada forçada via POST `hdnTipoVisualizacao=D` no form de procedimento_controlar
- Especificação extraída do `onmouseover` do link do processo (`infraTooltipMostrar('Especificação','Tipo')`) — disponível INDEPENDENTE da configuração de colunas do painel
- Labels de documentos parseados via regex: "Despacho GPF 2874369" → tipo=Despacho, sigla=GPF, numero=2874369
- **`hdnAnexos` encoding**: separador é `±` (U+00B1), encoding ISO-8859-1 como `%B1` — NÃO usar `#`. Construir POST manual (`content=body.encode("ascii")`) para evitar double-encoding pelo httpx
- **`hdnFlagDocumentoCadastro`**: JS `submeter()` muda `'1'→'2'` antes do submit; obrigatório ser `'2'` no POST
- Padrão REST-first: todos os tools usam `backend.has_rest` para preferir REST quando disponível e cair para web scraping caso contrário
- `sei_consultar_processo` é híbrido: REST para dados ricos + web para documentos[] em paralelo via asyncio.gather
- `sei_resumo_processos` é REST-only (precisa dos flags estruturados de status para agrupamento correto)
- Cache in-memory TTL 1h no SEIClient para: `pesquisar_tipos_processo`, `listar_unidades_usuario`, `pesquisar_marcadores`

### Limitações conhecidas
- Cancelar assinatura: a função `DocumentoRN::cancelarAssinaturaInternoControlado` existe no core SEI (linha 4026) mas NÃO está exposta na API REST
- `sei_marcar_nao_lido` usa workaround de enviar processo para a própria unidade
- Web scraper aborta se detectar CAPTCHA ou 2FA na página de login
- Colunas da Detalhada dependem da configuração do painel do usuário (mas especificação sempre vem do tooltip)
- `sei_listar_documentos` e `sei_arvore_processo` via web não retornam flags de status (assinado, cancelado, etc.) — para isso usar `sei_consultar_documento_externo` ou `sei_consultar_documento_interno` (REST) por documento

## Ambientes testados

- Produção ANTAQ: https://sei.antaq.gov.br/sei/modulos/wssei/controlador_ws.php/api/v2
- SEI-RO (sem mod-wssei): https://sei.sistemas.ro.gov.br — web-only, funciona com SEIWebClient

## Paridade web — implementado

A paridade web completa foi implementada (Fases 1–11). Ver `docs/rfc/0001-web-first.md` para o histórico completo e a lista de tools permanentemente REST-only (assinatura PKI, credenciamento, `sei_versao`, `sei_resumo_processos`, `sei_listar_relacionamentos`).
