# RFC 0001 — Web-first: Paridade via Scraper para Instâncias sem mod-wssei

**Status:** Proposta  
**Data:** 2026-06-11  
**Autores:** franklinbaldo  

---

## Problema

Das ~116 tools do `todos`, ~105 dependem exclusivamente da API REST mod-wssei.
Instâncias SEI sem o módulo instalado (ex: SEI-RO) ficam com essas tools falhando.

O scraper web (`SEIWebClient`) já prova que é possível replicar operações via
HTTP — `listar_processos` (23×), `arvore_processo` (10×), `listar_documentos`
(10×) e `listar_atividades` (2×) são mais rápidas via web do que via REST, e
`incluir_documento_externo` demonstrou que **escrita** via scraper também é
viável (upload multipart + form complexo). O próximo passo é estender esse
padrão para as demais tools de escrita e leitura que hoje só existem no
caminho REST.

---

## Invariantes Descobertos na Implementação

Estes invariantes foram aprendidos com custo alto e **nunca devem ser revertidos**:

| Invariante | Descrição |
|---|---|
| Separador `%B1` em `hdnAnexos` | Os campos do anexo são unidos por `±` URL-encoded como ISO-8859-1 (`%B1`); o PHP divide em `\xB1`. Não pode ser duplo-codificado (o byte alto UTF-8 `%C2` quebra o split) |
| `hdnFlagDocumentoCadastro = "2"` | O HTML traz `value="1"`, mas o JS `submeter()` altera para `"2"` antes do submit; enviar `"1"` silencia o cadastro |
| Encoding ISO-8859-1 | Respostas do SEI são ISO-8859-1; POSTs devem respeitar esse encoding — UTF-8 corrompe acentos |
| `infra_hash` | SHA-256(params + sessionSecret); válido enquanto a sessão SIP viver; reutilizável entre chamadas |
| Botão submit no login | O PHP exige o par `name=value` do botão submit no POST; sem ele ignora o form. O nome varia por instância (`sbmLogin=Acessar` na ANTAQ, `sbmAcessar=ACESSAR` no RO) — detectar dinamicamente do form |
| Token CSRF dinâmico | `hdnToken<hash>` — deve ser capturado do GET da página, não reutilizado |
| Visualização Detalhada | Requer `hdnTipoVisualizacao=D` no POST do form `procedimento_controlar` |

---

## Proposta: `SEIBackend` com Detecção Automática de Capacidade

```python
class SEIBackend:
    """Abstração sobre REST + web com detecção automática de capacidade."""

    def __init__(self, rest: SEIClient | None, web: SEIWebClient) -> None:
        self._rest = rest
        self._web = web
        self._has_rest = rest is not None

    async def listar_processos(self, ...) -> list[dict]:
        # web é sempre mais rápido para listagem
        return await self._web.listar_processos(...)

    async def incluir_documento_interno(self, ...) -> dict:
        if self._has_rest:
            return await self._rest.incluir_documento_interno(...)
        return await self._web.incluir_documento_interno(...)  # fase 3
```

Também propõe um helper genérico `executar_acao_processo` que encapsula o
padrão de POST para qualquer ação do SEI web, parametrizando apenas o
`hdnAcao` e os campos adicionais.

---

## Plano de Implementação (4 Fases)

### Fase 1 — Ações simples (sem form de dados)
Ações que apenas requerem `infra_hash` + `hdnAcao`:
- Marcar processo como lido/não lido
- Atualizar andamento
- Controle de acesso básico

### Fase 2 — Forms de escrita (com campos de dados)
- `incluir_documento_interno` via web (editor HTML do SEI)
- `alterar_processo` via web
- `atribuir_processo` via web

### Fase 3 — Scrapers de leitura faltantes
Tools de leitura que só existem no caminho REST hoje:
- `consultar_documento_externo`, `consultar_documento_interno` (metadados)
- `listar_assinaturas`, `listar_andamentos`
- `pesquisar_tipos_processo`, `listar_marcadores`

### Fase 4 — Forms complexos
- Assinatura via web
- Tramitação de processo
- Edição de documento interno (editor HTML)

(O upload de documento externo — multipart + campos interdependentes — já foi
implementado e serviu de prova de conceito para os invariantes acima.)

---

## Variáveis de Ambiente

| Variável | Obrigatoriedade | Descrição |
|---|---|---|
| `SEI_URL` | Opcional | URL REST mod-wssei (ex: `https://sei.org.gov.br/sei/modulos/wssei/.../api/v2`) |
| `SEI_WEB_URL` | Obrigatória se `SEI_URL` ausente | Raiz web do SEI (ex: `https://sei.org.gov.br`) |
| `SEI_USUARIO` | Obrigatória | Usuário SEI/SIP |
| `SEI_SENHA` | Obrigatória | Senha SEI/SIP |
| `SEI_ORGAO` | Opcional (padrão: `0`) | ID do órgão na API REST |
| `SEI_SIGLA_ORGAO` | Opcional (padrão: `ANTAQ`) | Sigla do órgão no selOrgao do SIP |
| `SEI_SIGLA_SISTEMA` | Opcional (padrão: `SEI`) | Parâmetro `sigla_sistema` na URL de login SIP |
| `SEI_SIGLA_ORGAO_SISTEMA` | Opcional (padrão: `SEI_SIGLA_ORGAO`) | Parâmetro `sigla_orgao_sistema` na URL de login SIP (ex: `RO`) |
| `SEI_VERIFY_SSL` | Opcional (padrão: `true`) | Verificação de certificado SSL |

Quando `SEI_URL` está presente, `sei_root` é derivado dela (tudo antes de `/sei/`).
Quando `SEI_WEB_URL` está presente, tem precedência e é usado diretamente como `sei_root`.

---

## Compatibilidade

Instâncias com mod-wssei continuam funcionando exatamente como antes — o
`SEIBackend` usa REST quando disponível. Instâncias sem mod-wssei ganham
cobertura progressiva conforme as fases forem implementadas.
