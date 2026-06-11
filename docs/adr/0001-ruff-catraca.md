# ADR 0001 — Catraca de qualidade com ruff select=ALL

**Status:** Aceito  
**Data:** 2026-06-11

## Contexto

O projeto acumulou dívida técnica em estilo, segurança e idiomaticidade ao longo de várias iterações rápidas. Queríamos ativar o conjunto máximo de regras do ruff sem travar o desenvolvimento — a abordagem de "ativar tudo e corrigir antes de mergear" não é viável com 1112 violações preexistentes.

## Decisão

Adotamos uma **catraca de qualidade** em duas partes:

### 1. Configuração (`pyproject.toml`)

```toml
[tool.ruff.lint]
select = ["ALL"]
ignore = [
    # Conflitos com ruff format (o formatter é dono dessas regras)
    "W191", "E101", "E111", "E114", "E117",
    "D206", "D300",
    "Q000", "Q001", "Q002", "Q003",
    "COM812", "COM819",
    "ISC001", "ISC002",
    "E501",
    # Pares D mutuamente exclusivos — escolha explícita
    "D203",  # mantemos D211
    "D213",  # mantemos D212
    # Decisões permanentes de projeto
    "RUF001", "RUF002", "RUF003",  # Unicode PT é intencional
    "CPY001",                       # sem copyright headers
    "FIX001-FIX004",                # markers FIXME/HACK ok
    "TD001-TD007",                  # rastreamento de TODOs fora do ruff
    "S104",                         # 0.0.0.0 intencional (Railway/container)
]
```

### 2. Baseline de `# noqa` (piso da catraca)

```bash
ruff check --add-noqa src/
```

Executado uma única vez para adicionar 623 diretivas `# noqa: XYYY` em todas as linhas que já violavam alguma regra. Isso congela o estado atual sem bloquear nada que já existia.

## Consequências

**Regra do jogo a partir de agora:**

| Ação | Resultado |
|------|-----------|
| Novo código com violação | CI falha — bloqueado |
| Remover um `# noqa` e corrigir a linha | Violação eliminada permanentemente |
| Adicionar novo `# noqa` | Permitido somente com comentário justificando |

**Distribuição do débito a eliminar (maiores grupos):**

| Qtd | Código | Tema |
|-----|--------|------|
| 194 | `TRY003` | Mensagens de exceção em classes próprias |
| 155 | `EM102`  | f-string dentro de `raise` → extrair variável |
| 142 | `BLE001` | `except Exception` genérico → tipo específico |
| 123 | `TRY002` | `raise RuntimeError/ValueError` → exceção de domínio |
|  40 | `PLR2004`| Magic numbers → constantes nomeadas |

**Estratégia de limpeza sugerida:**

1. `TRY002` + `TRY003` juntos: criar módulo `src/todos/exceptions.py` com hierarquia `SEIError > SEIAuthError, SEINotFoundError, SEIPermissionError`. Substitui ~317 noqa de uma vez.
2. `EM102` / `EM101`: script de refactoring — extrair mensagem para variável `msg` antes do `raise`.
3. `BLE001`: revisar cada `except Exception` e tipar com a exceção correta ou anotar `# noqa: BLE001` com justificativa explícita.
4. `PLR2004`: concentrar no `sei_client.py` e `sei_web_client.py` onde os status HTTP aparecem repetidos.

## Alternativas consideradas

- **Ignorar regras inteiras globalmente** — manteria o projeto preso em padrões antigos para sempre. Rejeitado.
- **Corrigir tudo antes de ativar** — inviável com 1112 violações e desenvolvimento ativo em paralelo. Rejeitado.
- **`ruff check --exit-zero` no CI** — não traz nenhuma garantia. Rejeitado.

## Referências

- [Ruff: conflicting lint rules com o formatter](https://docs.astral.sh/ruff/formatter/#conflicting-lint-rules)
- [Ruff: `--add-noqa`](https://docs.astral.sh/ruff/linter/#adding-noqa-comments)
- Commit de baseline: `b4c457d` — "chore(ruff): ativar select=ALL + catraca com baseline noqa"
