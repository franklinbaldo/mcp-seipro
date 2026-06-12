# todos

[![PyPI](https://img.shields.io/pypi/v/mcp-sei)](https://pypi.org/project/mcp-sei/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-sei)](https://pypi.org/project/mcp-sei/)
[![License](https://img.shields.io/pypi/l/mcp-sei)](https://pypi.org/project/mcp-sei/)

**TOdos Domina O Sei** — MCP Server para o SEI (Sistema Eletrônico de Informações) com arquitetura **web-first**: scraper HTTP do frontend + REST mod-wssei v2 quando disponível. Funciona em qualquer instância SEI 4.0+ — **inclusive sem mod-wssei instalado**.

**121 tools** para gerenciar processos, documentos, tramitação, assinatura, blocos, marcadores, acompanhamento, credenciamento, modelos e mais. Operações de leitura críticas usam scraper web (**23×** mais rápido que REST). Catálogos estáticos usam cache em disco com TTL de 24h.

## Origem

Este projeto é um fork de [**mcp-seipro**](https://github.com/SEI-Pro/mcp-seipro), criado e mantido por [@opedrosoares](https://github.com/opedrosoares) como parte do ecossistema [SEI Pro](https://github.com/SEI-Pro/sei-pro).

Um agradecimento especial ao Pedro Soares pela dedicação em construir o mcp-seipro do zero — sem esse trabalho pioneiro, este fork não existiria.

**Por que o fork?** O mcp-seipro depende exclusivamente da API REST mod-wssei — um módulo opcional que precisa ser instalado pelo administrador do SEI. O [SEI de Rondônia](https://sei.sistemas.ro.gov.br) (e diversas outras instâncias públicas) não tem o mod-wssei instalado, tornando todas as 116 tools inoperantes nessas instâncias.

A solução foi implementar um **scraper HTTP do próprio frontend web do SEI** como backend primário: sem dependência de módulo extra, sem configuração no servidor, funciona em qualquer instância que o usuário consiga acessar pelo navegador. O projeto upstream está focado na API REST; este fork mantém compatibilidade total com ela quando disponível e adiciona paridade web completa para quem não tem.

## Instalação

### Opção 1: Claude Desktop (extensão com um clique)

Baixe o arquivo [`todos.mcpb`](https://github.com/franklinbaldo/todos/releases/latest) e abra com duplo-clique. O Claude Desktop instala automaticamente e pede suas credenciais.

### Opção 2: PyPI (pip)

```bash
pip install mcp-sei
```

### Opção 3: Instalador interativo (Recomendado)

O instalador interativo faz o setup completo do MCP do `todos` em todos os ambientes suportados de forma automática, realizando:
1. Instalação do gerenciador de pacotes `uv` (se necessário).
2. Instalação da CLI `todos` globalmente via `uv tool`.
3. Armazenamento seguro de credenciais no Keyring nativo do OS (sem expor senhas em arquivos).
4. Registro do servidor MCP automaticamente no **Claude Code**, **Claude Desktop**, **Antigravity IDE** e no workspace local (`.mcp.json`).

Para executar a instalação automatizada:

* **No Windows (PowerShell):**
  ```powershell
  powershell -ExecutionPolicy Bypass -Command "if (-not (Get-Command 'uv' -ErrorAction SilentlyContinue)) { Write-Host '[*] Instalando uv...'; irm https://astral.sh/uv/install.ps1 | iex; $env:PATH += ';$env:USERPROFILE\.local\bin' }; uv tool install git+https://github.com/franklinbaldo/todos.git --force; $env:PATH += ';$env:USERPROFILE\.local\bin'; todos setup"
  ```

* **No Linux / macOS (Terminal):**
  ```bash
  if ! command -v uv &> /dev/null; then echo "[*] Instalando uv..."; curl -LsSf https://astral.sh/uv/install.sh | sh; source $HOME/.local/bin/env; fi; uv tool install git+https://github.com/franklinbaldo/todos.git --force; export PATH="$HOME/.local/bin:$PATH"; todos setup
  ```

O assistente interativo solicitará suas credenciais do SEI de forma segura (com a senha oculta) e aplicará as configurações.

## Configuração

> [!TIP]
> **Dica de Ouro**: O comando `todos setup` (descrito na Opção 3 de Instalação) já configura automaticamente todos os ambientes abaixo (Claude Code, Claude Desktop, Antigravity IDE e workspace local) e armazena sua senha de forma criptografada no Keyring do sistema. Use as seções abaixo apenas se preferir realizar a configuração manual.

### Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|----------|-------------|-----------|
| `SEI_URL` | Não | URL base da API mod-wssei v2 (deixe em branco se a instância não tiver mod-wssei) |
| `SEI_USUARIO` | Sim | Usuário para autenticação |
| `SEI_SENHA` | Sim | Senha para autenticação |
| `SEI_ORGAO` | Não | Código do órgão (padrão: `0`) |
| `SEI_CONTEXTO` | Não | Contexto opcional |
| `SEI_VERIFY_SSL` | Não | `true` (padrão) ou `false` |
| `SEI_OCR_LANG` | Não | Idioma do OCR (padrão: `por`) |
| `SEI_PERMITIR_RESTRITOS` | Não | `false` (padrão) ou `true`. Ver "Privacidade e dados restritos" |

> **Dica: como obter `SEI_URL` e `SEI_ORGAO` direto pelo SEI**
>
> Na barra lateral do SEI (menu à esquerda), role até o final — você verá um QR Code para o aplicativo móvel. Esse QR Code contém um link com todas as informações necessárias:
>
> ```
> https://sei.orgao.gov.br/sei/modulos/wssei/controlador_ws.php/api/v2;siglaorgao: ORGAO;orgao: 0;contexto:
> ```
>
> - **`SEI_URL`** — a URL antes do `;` (ex: `https://sei.orgao.gov.br/sei/modulos/wssei/controlador_ws.php/api/v2`)
> - **`SEI_ORGAO`** — o valor após `orgao:` (ex: `0`)
>
> Você pode escanear o QR Code com a câmera do celular para copiar o link, ou simplesmente anotar os dados a partir do menu.
>
> **Sem mod-wssei?** Deixe `SEI_URL` em branco. O servidor opera via scraper web e todas as tools cotidianas funcionam normalmente.

### Registro no Claude Code

Adicione ao `.mcp.json` do projeto ou `~/.claude.json` (global):

```json
{
  "mcpServers": {
    "todos": {
      "command": "todos",
      "env": {
        "SEI_URL": "https://sei.orgao.gov.br/sei/modulos/wssei/controlador_ws.php/api/v2",
        "SEI_USUARIO": "seu.usuario",
        "SEI_SENHA": "sua-senha",
        "SEI_ORGAO": "0"
      }
    }
  }
}
```

### Registro no Claude Desktop (manual)

Edite `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "todos": {
      "command": "todos",
      "env": {
        "SEI_URL": "https://sei.orgao.gov.br/sei/modulos/wssei/controlador_ws.php/api/v2",
        "SEI_USUARIO": "seu.usuario",
        "SEI_SENHA": "sua-senha",
        "SEI_ORGAO": "0"
      }
    }
  }
}
```

## Exemplos de uso

Com o todos configurado, basta conversar com o Claude em linguagem natural:

### Consultas

- *"O que diz o processo 50300.018905/2018-67?"*
- *"Leia o documento SEI 2843449 e me faça um resumo"*
- *"Qual foi o último andamento do processo de Auditoria TCU que está na unidade GPF?"*
- *"Liste para mim os processos da caixa GPF no SEI"*
- *"Quais processos estão atribuídos a mim na unidade SFC?"*

### Ações

- *"Crie um despacho no processo 50300.001234/2024-01 aprovando o pedido"*
- *"Tramite o processo 50300.005678/2024-02 para a unidade SFC com prazo de 5 dias"*
- *"Assine todos os documentos do bloco de assinatura 'Contratos Março'"*
- *"Marque o processo como acompanhamento especial com o grupo 'Urgentes'"*
- *"Crie um marcador vermelho chamado 'Pendente Resposta' e aplique no processo"*

### Análise

- *"Me dê um resumo dos processos da minha caixa agrupados por tipo"*
- *"Quais processos da unidade GPF estão sem movimentação há mais de 30 dias?"*
- *"Compare o conteúdo dos documentos 2843449 e 2843450"*

## Tools disponíveis (116)

### Sistema e metadados (3)

| Tool | Descrição |
|------|-----------|
| `sei_versao` | Retorna versão do SEI e do mod-wssei instalado |
| `sei_listar_orgaos` | Lista órgãos da instalação do SEI |
| `sei_listar_contextos` | Lista contextos disponíveis para um órgão |

### Navegação e contexto (8)

| Tool | Descrição |
|------|-----------|
| `sei_unidade_atual` | Informa ID, sigla e nome da unidade/setor ativo na sessão |
| `sei_listar_unidades` | Lista unidades acessíveis pelo usuário |
| `sei_trocar_unidade` | Troca a unidade ativa por ID ou sigla, via interface web |
| `sei_pesquisar_unidades` | Pesquisa unidades por nome/sigla |
| `sei_pesquisar_outras_unidades` | Pesquisa unidades excluindo a atual |
| `sei_pesquisar_textos_padrao` | Pesquisa textos padrão internos da unidade |
| `sei_listar_usuarios` | Lista usuários (filtra por unidade ativa e nome) |
| `sei_pesquisar_usuarios` | Busca usuários por palavra-chave no órgão |

### Processos — consulta (11)

| Tool | Descrição |
|------|-----------|
| `sei_listar_processos` | Lista caixa da unidade via scraper web (~23× mais rápido que REST). Suporta `apenas_meus`, `tipo`, `filtro` |
| `sei_pesquisar_processos` | Pesquisa por texto, descrição, datas, unidade geradora, assunto ou grupo de acompanhamento |
| `sei_consultar_processo` | **Híbrido**: REST (especificacao, assuntos, interessados, observacoes) + Web (lista de documentos da árvore) em paralelo |
| `sei_resumo_processos` | Resumo agrupado por 17 campos (usa REST direto para flags estruturadas) |
| `sei_listar_unidades_processo` | Lista unidades onde o processo está aberto |
| `sei_consultar_atribuicao` | Consulta quem é responsável pelo processo |
| `sei_verificar_acesso` | Verifica se o usuário tem acesso ao processo |
| `sei_listar_relacionamentos` | Lista processos relacionados (mod-wssei 3.0.2+) |
| `sei_listar_atividades` | Histórico de atividades/andamentos via scraper web (~2× mais rápido) |
| `sei_listar_interessados` | Lista interessados do processo |
| `sei_listar_sobrestamentos` | Lista histórico de sobrestamentos |

### Processos — gestão (13)

| Tool | Descrição |
|------|-----------|
| `sei_criar_processo` | Cria novo processo (público ou restrito) |
| `sei_alterar_processo` | Altera metadados (nível de acesso, especificação) |
| `sei_enviar_processo` | Tramita para outra(s) unidade(s) — aceita sigla |
| `sei_concluir_processo` | Conclui na unidade atual |
| `sei_reabrir_processo` | Reabre processo concluído |
| `sei_receber_processo` | Confirma recebimento na unidade |
| `sei_atribuir_processo` | Atribui a um usuário (aceita nome) |
| `sei_remover_atribuicao` | Remove atribuição de processo |
| `sei_marcar_nao_lido` | Marca processo como não lido na unidade |
| `sei_sobrestar_processo` | Sobresta processo (motivo obrigatório) |
| `sei_remover_sobrestamento` | Remove sobrestamento |
| `sei_pesquisar_tipos_processo` | Pesquisa tipos de processo |
| `sei_pesquisar_hipoteses_legais` | Pesquisa hipóteses legais (restrito/sigiloso) |

### Processos — assuntos (2)

| Tool | Descrição |
|------|-----------|
| `sei_pesquisar_assuntos` | Pesquisa assuntos disponíveis |
| `sei_sugestao_assuntos_processo` | Sugestões de assunto para um tipo de processo |

### Processos sigilosos — credenciamento (4)

| Tool | Descrição |
|------|-----------|
| `sei_listar_credenciamentos` | Lista credenciamentos de acesso ao processo |
| `sei_conceder_credenciamento` | Concede acesso a um usuário |
| `sei_renunciar_credenciamento` | Renuncia ao próprio acesso |
| `sei_cassar_credenciamento` | Revoga acesso de um usuário |

### Documentos — leitura (8)

| Tool | Descrição |
|------|-----------|
| `sei_arvore_processo` | Árvore completa via scraper web (~10× mais rápido que REST). Aceita protocolo formatado |
| `sei_buscar_documento` | Busca documento pelo número SEI (via Solr) |
| `sei_listar_documentos` | Lista documentos via scraper web (~10× mais rápido). Aceita protocolo formatado |
| `sei_ler_documento` | Lê documento (HTML ou PDF/OCR) em Markdown |
| `sei_baixar_anexo` | Baixa documento externo em base64 (max 10MB) |
| `sei_consultar_documento_externo` | Consulta metadados de documento externo |
| `sei_listar_assinaturas` | Lista assinaturas de um documento |
| `sei_listar_blocos_documento` | Lista blocos de assinatura do documento |

### Documentos — escrita (10)

| Tool | Descrição |
|------|-----------|
| `sei_criar_documento` | Cria documento interno vazio |
| `sei_criar_documento_externo` | Cria documento externo com upload de arquivo |
| `sei_alterar_documento_interno` | Altera metadados de documento interno |
| `sei_alterar_documento_externo` | Altera metadados/arquivo de documento externo |
| `sei_listar_secoes` | Lista seções editáveis de um documento |
| `sei_editar_secao` | Altera conteúdo HTML (preenche somenteLeitura auto) |
| `sei_assinar_documento` | Assinatura eletrônica |
| `sei_cancelar_assinatura` | Tenta cancelar assinatura via edição |
| `sei_gerar_referencia` | Gera hiperlink dinâmico para documento citado |
| `sei_estilos` | Consulta dicionário de 39 estilos CSS do SEI |

### Documentos — tipos e modelos (7)

| Tool | Descrição |
|------|-----------|
| `sei_pesquisar_tipos_documento` | Pesquisa tipos de documento (séries) |
| `sei_pesquisar_tipos_documento_externo` | Tipos aplicáveis a documentos externos |
| `sei_pesquisar_tipos_conferencia` | Tipos de conferência (cópia, original, autenticada) |
| `sei_sugestao_assuntos_documento` | Sugestões de assunto para um tipo de documento |
| `sei_listar_grupos_modelos` | Lista grupos de modelos de documento |
| `sei_listar_modelos` | Lista modelos de documento disponíveis |
| `sei_parametros_upload` | Extensões/tamanhos permitidos para upload |

### Assinantes (2)

| Tool | Descrição |
|------|-----------|
| `sei_listar_assinantes` | Lista cargos/funções para assinatura |
| `sei_listar_orgaos_assinante` | Lista órgãos disponíveis para assinatura |

### Ciência e andamento (3)

| Tool | Descrição |
|------|-----------|
| `sei_dar_ciencia` | Dá ciência em documento ou processo |
| `sei_listar_ciencias` | Lista ciências registradas |
| `sei_registrar_andamento` | Registra andamento/atividade no processo |

### Anotação e observação (2)

| Tool | Descrição |
|------|-----------|
| `sei_criar_anotacao` | Cria anotação (post-it) individual no processo |
| `sei_criar_observacao` | Cria observação da unidade no processo |

### Contatos (2)

| Tool | Descrição |
|------|-----------|
| `sei_pesquisar_contatos` | Pesquisa contatos cadastrados |
| `sei_criar_contato` | Cria novo contato |

### Marcador (8)

| Tool | Descrição |
|------|-----------|
| `sei_criar_marcador` | Cria marcador (lista cores se omitida) |
| `sei_excluir_marcador` | Exclui marcador(es) |
| `sei_desativar_marcador` | Desativa marcador(es) sem excluir |
| `sei_reativar_marcador` | Reativa marcador(es) desativados |
| `sei_marcar_processo` | Adiciona marcador a um processo |
| `sei_pesquisar_marcadores` | Lista marcadores disponíveis |
| `sei_consultar_marcador_processo` | Consulta marcadores ativos de um processo |
| `sei_historico_marcador_processo` | Histórico de marcadores do processo |

### Acompanhamento especial (8)

| Tool | Descrição |
|------|-----------|
| `sei_acompanhar_processo` | Adiciona acompanhamento especial |
| `sei_alterar_acompanhamento` | Altera acompanhamento existente |
| `sei_remover_acompanhamento` | Remove acompanhamento |
| `sei_listar_meus_acompanhamentos` | Lista processos acompanhados pelo usuário |
| `sei_listar_acompanhamentos_unidade` | Lista acompanhamentos da unidade |
| `sei_listar_grupos_acompanhamento` | Lista grupos de acompanhamento |
| `sei_criar_grupo_acompanhamento` | Cria grupo de acompanhamento |
| `sei_excluir_grupo_acompanhamento` | Exclui grupo de acompanhamento |

### Bloco interno (10)

| Tool | Descrição |
|------|-----------|
| `sei_criar_bloco_interno` | Cria bloco interno |
| `sei_alterar_bloco_interno` | Altera descrição do bloco |
| `sei_excluir_bloco_interno` | Exclui bloco(s) |
| `sei_concluir_bloco_interno` | Conclui bloco(s) |
| `sei_reabrir_bloco_interno` | Reabre bloco concluído |
| `sei_incluir_processo_bloco_interno` | Inclui processo(s) no bloco |
| `sei_retirar_processo_bloco_interno` | Remove processo(s) do bloco |
| `sei_listar_processos_bloco_interno` | Lista processos do bloco |
| `sei_anotar_processo_bloco_interno` | Cria anotação em processo do bloco |
| `sei_alterar_anotacao_bloco_interno` | Altera anotação do bloco |

### Bloco de assinatura (16)

| Tool | Descrição |
|------|-----------|
| `sei_criar_bloco_assinatura` | Cria bloco (aceita sigla de unidades) |
| `sei_alterar_bloco_assinatura` | Altera descrição do bloco |
| `sei_excluir_bloco_assinatura` | Exclui bloco(s) |
| `sei_concluir_bloco_assinatura` | Conclui bloco(s) |
| `sei_reabrir_bloco_assinatura` | Reabre bloco concluído |
| `sei_retornar_bloco_assinatura` | Retorna bloco para unidade de origem |
| `sei_incluir_documento_bloco_assinatura` | Inclui documento(s) no bloco |
| `sei_retirar_documentos_bloco_assinatura` | Remove documento(s) do bloco |
| `sei_listar_documentos_bloco_assinatura` | Lista documentos do bloco |
| `sei_disponibilizar_bloco_assinatura` | Disponibiliza bloco para assinatura |
| `sei_cancelar_disponibilizacao_bloco` | Cancela disponibilização |
| `sei_pesquisar_blocos_assinatura` | Pesquisa blocos existentes |
| `sei_assinar_bloco` | Assina todos os documentos de um bloco |
| `sei_assinar_documentos_bloco` | Assina documentos específicos de um bloco |
| `sei_anotar_documento_bloco_assinatura` | Cria anotação em documento do bloco |
| `sei_alterar_anotacao_bloco_assinatura` | Altera anotação do bloco |

## Compatibilidade

### Instâncias sem mod-wssei

O todos está migrando para **web-first**. Hoje, as seguintes tools funcionam via scraper do frontend web, sem depender do mod-wssei:

- `sei_unidade_atual`, `sei_listar_unidades`, `sei_trocar_unidade`
- `sei_listar_processos`, `sei_arvore_processo`, `sei_listar_documentos`, `sei_listar_atividades`
- `sei_incluir_documento_externo` (upload de arquivos)
- `sei_gerar_pdf_processo`, `sei_gerar_zip_processo`
- `sei_consultar_processo` (híbrida — parte web; a parte REST requer mod-wssei)

As demais tools (tramitação, conclusão, ciência, anotação, marcadores, blocos, assinatura etc.) ainda dependem do mod-wssei. A paridade completa via scraper é o objetivo do [RFC 0001](docs/rfc/0001-web-first.md) e será implementada em fases.

Para instâncias sem mod-wssei, configure `SEI_WEB_URL` (raiz do SEI, ex: `https://sei.orgao.gov.br`) no lugar de `SEI_URL`.

### Versões do SEI

Todos os **116 endpoints funcionam desde o mod-wssei 2.0.0** (SEI 4.0.x), exceto um:

| Tool | Versão mínima |
|------|---------------|
| `sei_listar_relacionamentos` | mod-wssei **3.0.2+** (SEI 5.0.x) |

Tabela de compatibilidade SEI ↔ mod-wssei:

| Versão SEI | mod-wssei | Observações |
|---|---|---|
| 4.0.x | 2.0.x | Base completa (131 rotas) |
| 4.1.1 | 2.2.0 | Correções de bugs |
| 5.0.x | 3.0.1 | Compatibilidade PHP 8.2 |
| 5.0.x | **3.0.2** | +`relacionamentos`, +`dataHora` em assinaturas |

Se algum endpoint falhar com erro inesperado, use `sei_versao` para verificar a versão do mod-wssei instalada na sua instância do SEI.

> **Nota:** a API mod-wssei v2 não expõe endpoint para **cancelar assinatura** de documentos em nenhuma versão (verificado até v3.0.2). A função existe no core do SEI (`DocumentoRN::cancelarAssinaturaInternoControlado`) mas não está exposta via REST. O `sei_cancelar_assinatura` usa o workaround de forçar uma edição mínima no documento.

## Arquitetura web-first

O todos opera primariamente via **scraping HTTP do frontend web do SEI** — o mesmo caminho que o navegador do usuário usa. O REST mod-wssei v2 é usado como complemento quando disponível.

| Tool | Estratégia | Ganho medido |
|---|---|---|
| `sei_listar_processos` | Scraper web puro (`procedimento_controlar.php` em modo Detalhada) | ~14.7 s → ~625 ms (**23×**) |
| `sei_consultar_processo` | Híbrido: REST `/processo/consultar/{id}` + scraper `arvore_montar.php` em paralelo | combina dados complementares |
| `sei_arvore_processo` | Scraper web (`arvore_montar.php`) | ~12 s → ~1.1 s (**10×**) |
| `sei_listar_documentos` | Scraper web (`arvore_montar.php`) | ~9.7 s → ~1.1 s (**10×**) |
| `sei_listar_atividades` | Scraper web (`procedimento_consultar_historico.php`) | ~2.5 s → ~1.2 s (**2×**) |
| `pesquisar_tipos_processo` | Cache em disco TTL 24h | ~4.2 s → instant |
| `pesquisar_tipos_documento` | Cache em disco TTL 24h | chamadas repetidas instantâneas |
| `listar_unidades_usuario` | Cache em disco TTL 24h | ~3.0 s → instant |

O cache usa o `DiskStore` do ecossistema FastMCP, persistido por padrão em
`~/.cache/todos/`. Defina `TODOS_CACHE_DIR` para usar outro diretório.

O scraper:

- Mantém uma **sessão SIP autenticada** persistente (login custa ~3 s, uma vez por conexão MCP).
- Reaproveita o `infra_hash` capturado da cadeia de redirects pós-login (válido enquanto a sessão SIP viver).
- Cacheia o action e os hidden fields do form principal de `procedimento_controlar` para POSTs subsequentes.
- Re-loga automaticamente se detectar que a sessão expirou.
- Funciona com qualquer instância SEI 4.0+/5.0+ (frontend web padrão).

Para o roteiro completo de migração web-first, veja [docs/rfc/0001-web-first.md](docs/rfc/0001-web-first.md).

## Funcionalidades

### Resolução automática

| Parâmetro | Aceita | Exemplo |
|-----------|--------|---------|
| Documento | Número SEI ou id interno | `sei_ler_documento("2843449")` |
| Processo | Protocolo ou IdProcedimento | `sei_criar_anotacao(processo="50300.018905/2018-67")` |
| Unidade | Sigla ou ID | `sei_enviar_processo(unidades_destino="SFC")` |
| Usuário | Nome ou ID | `sei_atribuir_processo(usuario="Karina")` |

### Leitura universal de documentos

- **Internos (HTML)** → Markdown (tabelas limpas, sem colunas vazias)
- **PDFs com texto** → Markdown via pdfplumber
- **PDFs escaneados** → Markdown via OCR (tesseract, limite 20 páginas)

### Estilos CSS do SEI

**Despachos:** `Paragrafo_Numerado_Nivel1` (corpo), âncora SEI no destinatário

**Notas Técnicas:** `Item_Nivel1/2/3/4` (H1/H2/H3/H4), `Item_Alinea_Letra` (a, b, c), `Item_Inciso_Romano` (I, II, III)

**Regra:** toda numeração usa classes CSS, nunca texto manual.

## Privacidade e dados restritos

O SEI classifica processos e documentos em três níveis: público (`nivelAcesso=0`), restrito (`1`) e sigiloso (`2`). O MCP usa as credenciais do usuário, então acessa o que o usuário enxergaria no SEI — incluindo restritos. Sigilosos exigem credenciamento prévio no próprio SEI.

Como conteúdo restrito pode trafegar para um provedor LLM (que talvez logue, retenha ou treine modelos com ele), o MCP impõe um **gate de consentimento** nas duas tools que entregam conteúdo bruto:

- `sei_ler_documento` — markdown/texto/HTML do documento
- `sei_baixar_anexo` — base64 do arquivo

**Comportamento padrão (mais seguro):** se o documento tem `nivelAcesso` 1 ou 2 e a chamada **não** trouxe `confirmar_acesso_restrito=true`, o MCP responde com um JSON estruturado em pt-BR (`consentimento_necessario=true`, lista de `riscos[]` cobrindo LGPD/LAI/treinamento de modelos/sigilo funcional, e `como_liberar`). **O conteúdo bruto não é entregue.**

Existem duas formas de liberar:

| Forma | Escopo | Quando usar |
|-------|--------|-------------|
| `confirmar_acesso_restrito=true` na chamada | Per-call | Decisão pontual do usuário ao usar o LLM |
| `SEI_PERMITIR_RESTRITOS=true` (env var) | Servidor inteiro | Operador do MCP libera previamente |

Em ambos os casos, o conteúdo entregue vem com um **disclaimer prefixado** lembrando o nível de acesso, a hipótese legal e os riscos.

As demais tools (`sei_consultar_processo`, `sei_consultar_documento_externo`, etc.) **não bloqueiam metadados** — apenas anexam um campo `_aviso_acesso` quando detectam restrição, para o LLM repassar a informação ao usuário.

> O gate trata restrito e sigiloso de forma idêntica. Sigiloso já tem proteção adicional do SEI (credenciamento). Se quiser regras diferentes, abra um issue.

## Execução remota

O servidor usa **FastMCP 3**. Sem `PORT`, executa apenas o transporte local
`stdio`. Com `PORT`, carrega o runtime HTTP/OAuth isolado em `todos.remote`,
para uso via Claude no celular, na web ou em qualquer cliente MCP remoto.

Railway é apenas uma opção de hospedagem. O mesmo container pode rodar em
qualquer plataforma que forneça uma porta HTTP e uma URL pública.

Variáveis obrigatórias no modo remoto:

| Variável | Finalidade |
|----------|------------|
| `PORT` | Ativa o transporte Streamable HTTP |
| `BASE_URL` | URL pública usada na descoberta OAuth |
| `JWT_SECRET` | Assina os tokens que carregam as credenciais SEI |

As sessões SEI remotas são mantidas separadamente por sessão MCP. O runtime
local não importa `uvicorn`, Starlette nem o provedor OAuth.

### Exemplo com Railway

### 1. Criar conta no Railway

1. Acesse [railway.com](https://railway.com?referralCode=jJJ7Xz) e clique em **Sign Up**
2. Faça login com GitHub, GitLab ou e-mail

### 2. Instalar o Railway CLI

```bash
npm install -g @railway/cli
```

### 3. Clonar e fazer deploy

```bash
git clone https://github.com/franklinbaldo/todos.git
cd todos
railway login
railway init -n todos
railway add --service todos
railway variables set \
  JWT_SECRET="$(openssl rand -base64 48)" \
  BASE_URL="https://SEU-PROJETO.up.railway.app"
railway domain
railway up
```

### 4. Conectar no Claude

1. Acesse [claude.ai](https://claude.ai) → **Settings** → **Connectors**
2. Clique em **Adicionar conector personalizado**
3. Cole a URL do seu servidor: `https://SEU-PROJETO.up.railway.app/mcp`
4. Preencha a URL da API do SEI, usuário e senha

### Como funciona

| Ambiente | Variável `PORT` | Transporte | Uso |
|----------|-----------------|------------|-----|
| Local | ausente | stdio | Claude Code / Claude Desktop |
| Host remoto | presente | Streamable HTTP + OAuth | Claude mobile / web / remoto |

## Requisitos de sistema

- Python >= 3.11
- Qualquer instância do SEI 4.0+ (com ou sem mod-wssei)
- Claude Code, Claude Desktop, ou qualquer cliente MCP

**Para OCR de PDFs escaneados (opcional):**
- `tesseract-ocr` e `tesseract-ocr-por`
- `poppler-utils`

## Links

- [mcp-seipro](https://github.com/SEI-Pro/mcp-seipro) — Projeto upstream (fork origin), por [@opedrosoares](https://github.com/opedrosoares)
- [SEI Pro](https://github.com/SEI-Pro/sei-pro) — Extensão de navegador para o SEI
- [PyPI](https://pypi.org/project/mcp-sei/)
- [Repositório](https://github.com/franklinbaldo/todos)
- [RFC 0001 — Web-first](docs/rfc/0001-web-first.md)
- [Railway](https://railway.com?referralCode=jJJ7Xz) — Plataforma de deploy na nuvem

## Licença

MIT
