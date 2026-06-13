# RFC 0004 — Hierarquia de exceções + remoção de código defensivo

**Status**: Concluído · **Atualizado**: 2026-06-13
**Data**: 2026-06-13
**Autores**: Franklin Baldo (com Claude Code)

## 1. Problema

O codebase tem dois padrões que mascaram erros reais e inflamam o código:

### 1.1 Catches cegos em todos os lugares

`server.py` tem 28 blocos `except Exception as e: raise ToolError(str(e)) from e`
com `# noqa: BLE001`. O agente recebe mensagens como
`"401 Client Error: Unauthorized for url: …"` — a string técnica do httpx — em vez de
`"Sessão SEI expirada. Use sei_status para verificar."`.

`sei_web_client.py` e `sei_client.py` têm 13 `except Exception` internos que silenciam
erros de parsing, autenticação e rede no mesmo balde.

### 1.2 Código defensivo desnecessário

`sei_web_client.py` faz 92 verificações `isinstance(x, Tag)` em sequência. A maioria
protege contra `NavigableString` retornado por `soup.find()` — mas `find()` com
argumento de tag/classe/id nunca retorna `NavigableString`. São guardas que
achatam o código sem proteger nada real.

Exemplos de defensividade desnecessária:
```python
# Padrão atual — guarda impossível
el = soup.find("table", id="tblProcessos")
if el and isinstance(el, Tag):          # find() com id= nunca devolve NavigableString
    ...

# Padrão atual — retorna silenciosamente em vez de propagar
row0 = a.find_parent("tr")
if row0 is None or not isinstance(row0, Tag):
    continue                            # mascara HTML inesperado como resultado vazio
```

## 2. Proposta

### 2.1 Hierarquia de exceções — `src/todos/exceptions.py`

```python
class SEIError(Exception):
    """Erro base do servidor SEI. Sempre tem mensagem legível por humanos."""

class SEIAuthError(SEIError):
    """Sessão expirada, login recusado, 401/403."""

class SEINotFoundError(SEIError):
    """Processo ou documento não existe no SEI."""

class SEIPermissionError(SEIError):
    """Acesso negado — documento restrito/sigiloso sem credenciamento."""

class SEIConnectionError(SEIError):
    """Falha de rede, timeout, instância inacessível."""

class SEIParseError(SEIError):
    """HTML da resposta não tem a estrutura esperada."""

class SEIValidationError(SEIError):
    """Parâmetros inválidos detectados antes de qualquer chamada HTTP."""
```

### 2.2 `sei_client.py` e `sei_web_client.py` — levantar exceções específicas

| Situação atual | Nova exceção |
|---|---|
| `httpx.HTTPStatusError` com status 401/403 | `SEIAuthError` |
| `httpx.HTTPStatusError` com status 404 | `SEINotFoundError` |
| `httpx.TimeoutException` / `httpx.ConnectError` | `SEIConnectionError` |
| Página de login detectada no scraper | `SEIAuthError` |
| `soup.find(...)` retorna `None` em elemento obrigatório | `SEIParseError` |
| Parâmetro obrigatório vazio / inválido | `SEIValidationError` |

### 2.3 `server.py` — catches específicos por tipo

Antes (28 tools idênticas):
```python
except Exception as e:  # noqa: BLE001
    raise ToolError(str(e)) from e
```

Depois — um helper central + catches específicos onde a mensagem importa:

```python
# helpers.py (ou no próprio server.py)
def _to_tool_error(e: Exception) -> ToolError:
    match e:
        case SEIAuthError():
            return ToolError(f"Sessão SEI expirada ou inválida. {e}. Use sei_status para reconectar.")
        case SEINotFoundError():
            return ToolError(f"Não encontrado no SEI: {e}")
        case SEIPermissionError():
            return ToolError(f"Acesso negado: {e}. Verifique credenciamento ou nível de acesso.")
        case SEIConnectionError():
            return ToolError(f"SEI inacessível: {e}. Verifique a rede e SEI_URL.")
        case SEIValidationError():
            return ToolError(f"Parâmetro inválido: {e}")
        case _:
            return ToolError(str(e))

# Em cada tool:
except SEIError as e:
    raise _to_tool_error(e) from e
except Exception as e:           # genuinamente inesperado — stack trace vai para logs
    raise ToolError(str(e)) from e
```

Com isso, o `# noqa: BLE001` desaparece dos 28 tools que só têm `SEIError` como
exceção esperada. O catch genérico só fica onde existe razão.

### 2.4 Remoção de `isinstance(x, Tag)` desnecessários

Regra de substituição:

```python
# Antes — guarda redundante
el = soup.find("div", class_="foo")
if not isinstance(el, Tag):
    return {}

# Depois — assert remove a guarda, propaga ParseError em HTML inesperado
el = soup.find("div", class_="foo")
if el is None:
    raise SEIParseError("div.foo ausente na resposta SEI")
# ty sabe que el é Tag aqui, nenhum isinstance necessário
```

Para loops (`for sib in row0.next_siblings`), onde `NavigableString` pode aparecer:

```python
# Antes
for sib in row0.next_siblings:
    if not (isinstance(sib, Tag) and sib.name == "tr"):
        continue

# Depois — semântica idêntica, mais explícito
for sib in row0.find_next_siblings("tr"):  # find_next_siblings já filtra por tag
    ...
```

`find_next_siblings("tr")` retorna apenas `ResultSet[Tag]` — sem isinstance necessário.

## 3. Fora de escopo

- Reescrever a lógica de retry/reauth do `sei_client.py` (funciona, apenas encapsula melhor).
- Adicionar testes de integração para exceções (requer servidor SEI ao vivo).
- Substituir todos os `isinstance(x, Tag)` de uma vez — fazer por módulo para facilitar revisão.

## 4. Plano de implementação

| Fase | Arquivo | Esforço |
|---|---|---|
| **A** | Criar `src/todos/exceptions.py` | 15 min |
| **B** | `sei_client.py` — levantar exceções específicas | 30 min |
| **C** | `sei_web_client.py` — levantar exceções específicas + remover isinstance redundantes | 2 h |
| **D** | `server.py` — helper `_to_tool_error` + catches específicos | 1 h |

As fases são sequenciais (cada uma depende da anterior).

## 5. Critérios de aceitação

- [ ] `pytest tests/ -v` passa sem regressões.
- [ ] `ruff check src/` passa sem `# noqa: BLE001` novos.
- [ ] `ty check src/` passa (isinstance removidos não introduzem erros de tipo).
- [ ] Erro de sessão expirada retorna mensagem com instrução de reconexão, não stack do httpx.
- [ ] `vulture src/` não reporta `SEIError` subclasses como código morto.
