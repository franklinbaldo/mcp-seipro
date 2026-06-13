# RFC 0005 — Conformidade total ruff: constantes, acessadores e decomposição

**Status**: Proposta
**Data**: 2026-06-13
**Autores**: Franklin Baldo (com Claude Code)

## 1. Problema

O CLAUDE.md proíbe `# noqa` explicitamente ("Proibido `# noqa` — nunca suprima uma
violação com `# noqa` ou `# type: ignore`. Se o ruff sinalizar, corrija o padrão.").
Após as RFCs 0001–0004, o codebase ainda tem **372 comentários `# noqa`** distribuídos
em 11 arquivos. Cada um suprime um code smell sem corrigi-lo.

### 1.1 PLR2004 — magic values (≈ 130 instâncias)

`sei_web_client.py` usa literais numéricos mágicos em toda comparação HTTP:

```python
if r.status_code != 200:   # noqa: PLR2004   ← repetido 45× só neste arquivo
    raise SEIConnectionError(...)
if len(tds) >= 4:          # noqa: PLR2004
    ...
```

`200` é um número com semântica global (HTTP OK). Comparações de comprimento como
`>= 4`, `< 2`, `>= 3` representam invariantes de layout HTML do SEI que deveriam
ter nome.

### 1.2 SLF001 — acesso a atributos privados (7 instâncias)

`server.py` e `setup_wizard.py` acessam atributos privados de `SEIWebClient`:

```python
# server.py:301
nome_usuario = web._nome_usuario        # noqa: SLF001
id_usuario   = web._id_usuario          # noqa: SLF001
orgao_usuario = web._orgao_usuario      # noqa: SLF001
total = int(web._form_hidden.get(...))  # noqa: SLF001

# setup_wizard.py:288
"nome": web_client._nome_usuario,       # noqa: SLF001
web_client._senha = ""                  # noqa: SLF001
```

`SEIWebClient` não expõe esses dados por nenhum método público. Callers precisam
violar o encapsulamento ou suprimir a violação.

### 1.3 PLR0911/PLR0913/PLR0915 — sei_ler_documento (1 instância, alto impacto)

`sei_ler_documento` (`server.py:1049`) é a tool mais complexa do servidor:

```python
async def sei_ler_documento(  # noqa: PLR0911, PLR0913, PLR0915
    id_documento: str,
    tipo_documento: Literal["auto", "I", "X"] = "auto",
    formato: Literal["markdown", "texto", "html", "base64"] = "markdown",
    confirmar_acesso_restrito: bool = False,  # noqa: FBT001, FBT002
    resolucao_ocr: int = 150,
    timeout_ocr: float = 30.0,
    ...
```

- 13 caminhos de retorno (PLR0911 — limite 6)
- 8 parâmetros (PLR0913 — limite 5)
- >50 statements (PLR0915 — limite 50)

A função mistura: resolução de ID, leitura REST, leitura web, conversão PDF→texto,
OCR fallback e filtragem de conteúdo, tudo em um único corpo.

### 1.4 G004 — f-string em logger (3 instâncias)

```python
logger.warning(f"web login falhou, seguindo só com REST: {e}")   # noqa: G004
logger.debug(f"elicit falhou ({type(e).__name__}: {e}) — fallback JSON")  # noqa: G004
```

f-strings em chamadas de log forçam interpolação mesmo quando o nível de log está
desabilitado. A correção é usar `%s` (lazy evaluation).

### 1.5 S110/S112 — except-pass e except-continue (3 instâncias)

```python
except Exception:  # noqa: S110
    pass

except Exception:  # noqa: S112
    continue
```

`contextlib.suppress(Exception)` é semântica idêntica sem `# noqa`.

### 1.6 ERA001 — código comentado (2 instâncias em sei_web_client.py)

```python
# objTabelaAnexos.adicionar([arr[...], ..., 'CPF', 'SIGLA'])  # noqa: ERA001
# onmouseover="return infraTooltipMostrar('Especificação','Tipo')"  # noqa: ERA001
```

Histórico pertence ao git, não ao fonte.

### 1.7 FBT001/FBT002 — bool posicional em métodos públicos (14 instâncias)

```python
# sei_web_client.py
async def listar_processos_web(
    detalhada: bool = True,     # noqa: FBT001, FBT002
    apenas_meus: bool = False,  # noqa: FBT001, FBT002
```

Parâmetros bool devem ser keyword-only (adicionar `*` antes deles).

## 2. Proposta

### 2.1 Helper `_raise_unless_ok` em vez de constante (Phase A)

Em vez de só nomear o literal `200`, centralizar toda a verificação em um helper
que lança `SEIConnectionError` diretamente. O literal desaparece do código:

```python
def _raise_unless_ok(r: httpx.Response, context: str = "") -> None:
    """Lança SEIConnectionError se a resposta não for HTTP 200.

    Elimina o padrão repetido 'if r.status_code != 200: raise ...' em todos os
    métodos do web client e garante mensagens de erro consistentes.
    """
    if r.status_code != httpx.codes.OK:  # usa a constante do próprio httpx
        prefix = f"{context}: " if context else ""
        raise SEIConnectionError(
            f"{prefix}SEI retornou HTTP {r.status_code} (esperado 200)"
        )
```

Uso nos métodos:

```python
# Antes (45× em sei_web_client.py)
if r.status_code != 200:   # noqa: PLR2004
    raise SEIConnectionError(f"Falha ao listar processos: {r.status_code}")

# Depois — zero literais, zero noqa, mensagem contextual uniforme
_raise_unless_ok(r, "listar processos")
```

Para comparações de comprimento de lista (`len(tds) >= 4`, `len(rows) < 2`, etc.)
onde o significado é invariante de layout HTML documentável, definir constantes:

```python
_COLS_PROCESSO_INBOX   = 2   # protocolo + especificação
_COLS_PROCESSO_DETALHE = 4   # protocolo + tipo + data + unidade
_COLS_ATIVIDADE_MIN    = 4   # data + ação + unidade + usuário
_COLS_MARCADOR_MIN     = 2   # nome + cor
```

Onde a comparação é demasiado local para nomear com sentido, adicionar
`PLR2004` ao `per-file-ignores` de `sei_web_client.py` (com comentário
justificando) em vez de espalhá-lo linha a linha.

### 2.2 Acessadores públicos em `SEIWebClient` (Phase B)

Adicionar propriedades em `sei_web_client.py`:

```python
@property
def nome_usuario(self) -> str:
    """Nome do usuário autenticado, vazio antes do login."""
    return self._nome_usuario

@property
def id_usuario(self) -> str:
    """ID interno do usuário no SEI."""
    return self._id_usuario or self._usuario

@property
def orgao_usuario(self) -> str:
    """Sigla do órgão/unidade do usuário."""
    return self._orgao_usuario

@property
def itens_painel(self) -> int:
    """Total de itens no painel (0 antes do primeiro listar_processos)."""
    return int(self._form_hidden.get("hdnDetalhadoNroItens", "0") or "0")

def limpar_senha(self) -> None:
    """Sobrescreve a senha em memória após uso."""
    self._senha = ""
```

Atualizar `server.py` e `setup_wizard.py` para usar as propriedades públicas.
Remover todos os `# noqa: SLF001`.

### 2.3 Decomposição de `sei_ler_documento` (Phase C)

Extrair quatro sub-funções privadas:

```python
async def _ler_documento_rest(
    backend: SEIBackend,
    id_doc: str,
    formato: str,
    *,
    confirmar_acesso_restrito: bool,
) -> str | None:
    """Tenta leitura via REST. Retorna None se backend não tem REST."""

async def _ler_documento_web(
    backend: SEIBackend,
    id_doc: str,
    formato: str,
    *,
    confirmar_acesso_restrito: bool,
) -> str:
    """Leitura via web scraper (fallback)."""

def _converter_conteudo_documento(
    conteudo: bytes | str,
    mime: str,
    formato: str,
    *,
    resolucao_ocr: int,
    timeout_ocr: float,
) -> str:
    """Converte bytes/html → markdown/texto/base64 conforme formato pedido."""

def _filtrar_conteudo_documento(texto: str, id_doc: str) -> str:
    """Remove cabeçalhos SEI e normaliza espaços em branco."""
```

A função pública `sei_ler_documento` fica com ≤ 40 linhas e ≤ 5 return paths:
`None` (REST ok), `str` (web ok), ou `raise ToolError`.
Remover `# noqa: PLR0911, PLR0913, PLR0915` e a FBT001/FBT002 em
`confirmar_acesso_restrito` (mover para keyword-only).

### 2.4 Correções pontuais (Phase D)

**G004 — logger com %s:**
```python
# Antes
logger.warning(f"web login falhou: {e}")          # noqa: G004
# Depois
logger.warning("web login falhou: %s", e)
```

**S110/S112 — contextlib.suppress:**
```python
# Antes
try:
    ...
except Exception:  # noqa: S110
    pass

# Depois
with contextlib.suppress(Exception):
    ...
```

**ERA001 — delete comentários:**
Remover as 2 linhas de código comentado; a explicação contextual fica em
docstring ou comentário não-código se ainda for necessária.

**FBT001/FBT002 — keyword-only bools:**
```python
# Antes
async def listar_processos_web(
    detalhada: bool = True,     # noqa: FBT001, FBT002
    apenas_meus: bool = False,  # noqa: FBT001, FBT002

# Depois
async def listar_processos_web(
    *,
    detalhada: bool = True,
    apenas_meus: bool = False,
```
Verificar e atualizar call sites para passar como keyword args.

## 3. Fora de escopo

- **D205/ANN/D401**: Violações de docstring e anotação em `sei_client.py` — volume
  alto, impacto baixo. Candidatas para RFC 0006 dedicada a "polish".
- **PLC0415** (imports dentro de função) para dependências opcionais pesadas
  (`pytesseract`, `pdfplumber`, `keyring`): esses imports precisam ficar
  condicionais — mover para o topo quebraria ambientes sem as dependências.
  Candidato a `per-file-ignores` com justificativa documentada.
- **ASYNC230/PTH123** (I/O síncrono em async): requer `asyncio.to_thread()` ou
  `aiofiles` — mudança arquitetural separada.
- **C901/PLR0912/PLR0915** em funções de `sei_web_client.py` que não são
  `sei_ler_documento` (ex: `login`, `pesquisar_processos_web`): decomposição
  desses é mais complexa e requer análise individual.

## 4. Plano de implementação

| Fase | Alvo | Noqa removidos | Esforço |
|---|---|---|---|
| **A** | `_raise_unless_ok()` + constantes de layout em `sei_web_client.py` | ~80 PLR2004 | 45 min |
| **B** | Acessadores públicos em `SEIWebClient` | 7 SLF001 | 30 min |
| **C** | Decomposição de `sei_ler_documento` | PLR0911/PLR0913/PLR0915 + 2 FBT | 90 min |
| **D** | G004 + S110/S112 + ERA001 + FBT em métodos públicos do web client | ~20 | 45 min |

Total estimado: **3h30** — ≈ 110 `# noqa` removidos, ≈ 260 restantes (maioria D/ANN
em docstrings de `sei_client.py` e condicionais de dependências opcionais).

## 5. Critérios de aceitação

- [ ] `uv run ruff check .` passa sem novos `# noqa` introduzidos.
- [ ] `uv run ruff format --check .` passa.
- [ ] `uv run pytest tests/ -q` — 181 testes passam sem regressões.
- [ ] `uv run ty check src/` passa (propriedades públicas tipadas corretamente).
- [ ] `uv run vulture src/` — sem dead code introduzido.
- [ ] `sei_ler_documento` tem ≤ 6 caminhos de retorno e ≤ 6 parâmetros.
- [ ] `SEIWebClient` expõe `nome_usuario`, `id_usuario`, `orgao_usuario`,
  `itens_painel`, `limpar_senha()` como API pública.
- [ ] Nenhum `# noqa: SLF001` em `server.py` ou `setup_wizard.py`.
- [ ] Nenhum `# noqa: G004` em `server.py`.
- [ ] Nenhum `# noqa: ERA001` em qualquer arquivo.
