# RFC 0001 — Web-first: Paridade via Scraper para Instâncias sem mod-wssei

**Status:** Proposta  
**Data:** 2026-06-11  
**Autores:** franklinbaldo  

---

## Problema

Das ~116 tools do `todos`, todas dependem da API REST mod-wssei. Instâncias SEI
sem o módulo instalado (ex: SEI-RO, SEI-TO) ficam com 100% das tools falhando.

O scraper web (`SEIWebClient`) já prova que é possível replicar operações de
leitura via HTTP — `listar_processos`, `arvore_processo`, `listar_documentos` e
`listar_atividades` são 10–23× mais rápidas via web do que via REST. O próximo
passo é estender esse padrão para **escrita** e para **todas as tools de leitura**
que hoje só existem no caminho REST.

---

## Invariantes Descobertos na Implementação

Estes invariantes foram aprendidos com custo alto e **nunca devem ser revertidos**:

| Invariante | Descrição |
|---|---|
| `hdnAnexos = "%B1"` | Valor literal esperado pelo backend PHP para campos de anexo vazios |
| `hdnFlagDocumentoCadastro` | Campo obrigatório no form de inclusão de doc externo; ausência silencia o POST |
| Encoding ISO-8859-1 | Todo POST ao SEI deve ser enviado em ISO-8859-1; UTF-8 corrompe acentos |
| `infra_hash` | SHA-256(params + sessionSecret); válido enquanto a sessão SIP viver; reutilizável |
| `sbmLogin` / botão submit | O PHP exige o par `name=value` do botão submit no POST; sem ele ignora o form |
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
- Upload de documento externo (multipart + campos interdependentes)
- Assinatura via web
- Tramitação de processo

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
| `SEI_SIGLA_ORGAO_SISTEMA` | Opcional | Parâmetro `sigla_orgao_sistema` na URL de login SIP |

Quando `SEI_URL` está presente, `sei_root` é derivado dela (tudo antes de `/sei/`).
Quando `SEI_WEB_URL` está presente, tem precedência e é usado diretamente como `sei_root`.

---

## Compatibilidade

Instâncias com mod-wssei continuam funcionando exatamente como antes — o
`SEIBackend` usa REST quando disponível. Instâncias sem mod-wssei ganham
cobertura progressiva conforme as fases forem implementadas.
