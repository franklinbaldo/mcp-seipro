# RFC 0003 — Ergonomia FastMCP: anotações, erros e progressão

**Status**: Concluído · **Atualizado**: 2026-06-13
**Data**: 2026-06-13
**Autores**: Franklin Baldo (com Claude Code)

## 1. Problema

O servidor expõe 121 tools ao agente de IA, mas aproveita uma fração pequena
das primitivas do protocolo MCP e do framework FastMCP 3.4.2. Três lacunas
concretas degradam a experiência:

### 1.1 Todas as tools parecem iguais para o cliente

O agente não tem como distinguir uma tool de leitura (`sei_consultar_processo`)
de uma operação destrutiva (`sei_excluir_documento`) antes de executá-la.
Nenhuma tool declara `readOnlyHint`, `destructiveHint` ou `idempotentHint`.
Clientes MCP que respeitam essas anotações (Claude Desktop, Claude Code) não
conseguem aplicar confirmações automáticas nem exibir contexto de segurança.

### 1.2 Erros voltam como JSON de sucesso

O helper `_error(msg)` retorna `{"error": "..."}` **como string de conteúdo**,
não como falha MCP. Para o protocolo, a tool _teve sucesso_ — ela simplesmente
devolveu um texto que começa com `{"error"`. O agente precisa analisar o
conteúdo para perceber que houve falha, e nem sempre faz isso de forma correta.
O FastMCP tem `ToolError` para sinalizar falha controlada pelo canal certo.

### 1.3 Operações longas rodam sem feedback

Tools como `sei_converter_pdf`, `sei_arvore_processo` (pode buscar dezenas de
documentos), `sei_pesquisar_processos` (paginação longa) e `sei_resumo_processos`
(agrega vários processos) executam em silêncio. O cliente não recebe nenhum
sinal de progresso — apenas espera, sem saber se a operação está andando ou
travou.

## 2. Objetivo

1. Anotar todas as 121 tools com a semântica de segurança correta.
2. Substituir `_error()` por `ToolError` para que falhas sejam reportadas pelo
   protocolo, não como conteúdo de sucesso.
3. Adicionar `ctx.report_progress()` nas tools com latência conhecida (> ~2 s).
4. (Opcional) Expor dados de referência estáticos via `@mcp.resource` para
   reduzir chamadas de ferramenta desnecessárias.

## 3. Proposta

### 3.1 Anotações de tool

Categorizar as 121 tools em quatro perfis e decorar com `ToolAnnotations`:

| Perfil | `readOnly` | `destructive` | `idempotent` | Exemplos |
|---|---|---|---|---|
| **Leitura** | `True` | `False` | `True` | `sei_consultar_processo`, `sei_listar_processos`, `sei_arvore_processo`, `sei_pesquisar_*`, `sei_listar_*` |
| **Escrita idempotente** | `False` | `False` | `True` | `sei_criar_anotacao` (re-criar sobrescreve), `sei_alterar_*` |
| **Escrita não-idempotente** | `False` | `False` | `False` | `sei_criar_processo`, `sei_criar_documento_*`, `sei_registrar_andamento`, `sei_enviar_processo` |
| **Destrutiva** | `False` | `True` | `False` | `sei_excluir_documento`, `sei_cancelar_disponibilizacao_bloco`, `sei_remover_marcador` |

Implementação — um decorator por perfil para não repetir os parâmetros:

```python
from fastmcp import ToolAnnotations

_READ  = {"annotations": ToolAnnotations(readOnlyHint=True, idempotentHint=True)}
_WRITE = {"annotations": ToolAnnotations(readOnlyHint=False, idempotentHint=False)}
_IDEM  = {"annotations": ToolAnnotations(readOnlyHint=False, idempotentHint=True)}
_DEST  = {"annotations": ToolAnnotations(readOnlyHint=False, destructiveHint=True)}

@mcp.tool(**_READ)
async def sei_consultar_processo(...): ...

@mcp.tool(**_DEST)
async def sei_excluir_documento(...): ...
```

Custo: adicionar `**_READ` / `**_WRITE` / `**_IDEM` / `**_DEST` a cada
`@mcp.tool()`. Nenhuma mudança na lógica das tools.

### 3.2 `ToolError` para erros controlados

Substituir o helper `_error` pela exceção correta do protocolo:

```python
# Antes
def _error(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)

# Depois — ainda manter _error() para compatibilidade com callers REST
# que devolvem dicts com "error"; o try/except da tool converte para ToolError
```

O catch central de cada tool passa a re-lançar:

```python
from fastmcp.exceptions import ToolError

# Dentro do except:
except Exception as e:  # noqa: BLE001
    raise ToolError(str(e)) from e
```

Para erros de negócio esperados (processo não encontrado, acesso negado),
lançar `ToolError` diretamente em vez de deixar propagar como `Exception`.
Isso permite ao agente distinguir "falha esperada com mensagem útil" de
"erro de sistema inesperado".

> **Impacto em testes**: os testes atuais (`tests/test_parsers.py`) testam
> parsers puros, não tools. A mudança não quebra nenhum teste existente.
> Novos testes de integração de tool podem checar `pytest.raises(ToolError)`.

### 3.3 Progressão em tools lentas

Adicionar `ctx.report_progress()` nas seguintes tools (lista não exaustiva):

| Tool | Trigger | Ponto de progresso |
|---|---|---|
| `sei_converter_pdf` | Cada página OCR | `current=pg, total=total_pgs` |
| `sei_arvore_processo` | Cada documento buscado | `current=i, total=len(nos)` |
| `sei_pesquisar_processos` | Cada página paginada | `current=pagina, total=total_pgs` |
| `sei_resumo_processos` | Cada processo agregado | `current=i, total=len(ids)` |
| `sei_gerar_zip_processo` | Upload/download | `0 → 50 → 100` |

Padrão proposto:

```python
async def sei_arvore_processo(protocolo: str, ctx: Context | None = None) -> str:
    ...
    nos = await backend.web.listar_nos_arvore(protocolo)
    for i, no in enumerate(nos):
        if ctx:
            await ctx.report_progress(current=i, total=len(nos))
        ...
```

O `ctx` já existe em todas as tools; basta propagar a chamada.

### 3.4 Resources para dados de referência (opcional)

Expor via `@mcp.resource` dados que o agente consulta frequentemente mas que
raramente mudam:

```python
@mcp.resource("sei://estilos-css")
async def sei_estilos_resource() -> str:
    """Lista todos os estilos CSS disponíveis para documentos SEI."""
    from todos.sei_styles import ESTILOS
    return json.dumps(ESTILOS, ensure_ascii=False, indent=2)

@mcp.resource("sei://hipoteses-legais")
async def sei_hipoteses_resource(ctx: Context) -> str:
    backend = _get_backend(ctx)
    data = await backend.rest.pesquisar_hipoteses_legais()
    return json.dumps(data, ensure_ascii=False, indent=2)
```

Isso permite ao agente _ler_ os dados de referência sem consumir uma tool call
no limite de chamadas por turno.

## 4. Fora de escopo

- `ctx.log()` substituindo `logging.getLogger`: benefício marginal para este
  servidor (logs já vão para stderr/Railway); o padrão stdlib é suficiente.
- Pydantic models como parâmetros de tool: os parâmetros atuais são simples e
  as docstrings já documentam o schema; overhead de migração não justifica.
- `@mcp.prompt()`: não há templates de prompt reutilizáveis candidatos.
- `ctx.session.state` em vez de `lifespan_context`: o padrão atual funciona e
  a migração não traria benefício funcional.

## 5. Plano de implementação

| Fase | Conteúdo | Esforço estimado |
|---|---|---|
| **A** | Definir `_READ/_WRITE/_IDEM/_DEST` + anotar todas as 121 tools | ~1 h (mecânico) |
| **B** | Substituir `_error()` por `raise ToolError` no try/except central | ~30 min |
| **C** | `ctx.report_progress()` nas 5 tools lentas identificadas | ~1 h |
| **D** | Resources `sei://estilos-css` e `sei://hipoteses-legais` | ~30 min |

As fases são independentes e podem ser entregues em PRs separados.

## 6. Critérios de aceitação

- [ ] `ruff check src/` passa sem novos erros.
- [ ] `pytest tests/ -v` passa (sem regressões).
- [ ] Um cliente MCP (ex: Claude Desktop) distingue visualmente tools de leitura
      de tools destrutivas antes da execução.
- [ ] Chamar uma tool que falha retorna um erro MCP (não uma string JSON com
      `"error"`), visível como falha no log do cliente.
- [ ] `sei_arvore_processo` com processo de 10+ documentos exibe progresso no
      cliente durante a execução.
