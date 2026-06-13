# RFC 0002 — Ergonomia da pesquisa web: parsing correto e contrato de tool

**Status**: Concluído
**Data**: 2026-06-11 · **Atualizado**: 2026-06-13
**Autores**: Franklin Baldo (com Claude Code)

## 1. Problema

A `sei_pesquisar_processos` ganhou fallback web no PR #10, mas dois problemas
reduzem sua utilidade em produção para agentes LLM:

### 1.1 Campo `unidade` corrompido

O parser assume que o campo `meta` do resultado usa `|` como separador:

```python
unidade_m = re.search(r"Unidade:\s*([^|]+)", meta)
```

O HTML real do SEI-RO não usa pipes — o texto é contíguo:

```
Unidade: SESDEC-GCI Usuário: 69194840210 Inclusão: 06/01/2026
```

Resultado: `unidade` retorna `"SESDEC-GCI Usuário: 69194840210 Inclusão: 06/01/2026"`
em vez de `"SESDEC-GCI"`. O campo `usuario` (CPF do responsável) se perde.

### 1.2 Docstring não explica a semântica da busca web

A docstring atual descreve os parâmetros REST mas cala sobre:

- **Aspas funcionam para frase exata**: `"NOME COMPLETO"` retorna resultados
  muito mais precisos do que palavras soltas. O agente que não sabe disso
  gera buscas vagas que retornam dezenas de falsos positivos.
- **Busca full-text cross-unit**: no caminho web a pesquisa não é filtrada
  pela unidade do usuário — varre todo o SEI. Útil (encontra processos de
  outras secretarias), mas inesperado para quem vem do REST.
- **`fonte: "web"` no retorno** significa que os filtros estruturais
  (`id_unidade_geradora`, `id_assunto`, `grupo`, `sta_tipo_data`) foram
  silenciados — o agente precisa saber disso para não confiar em uma busca
  por unidade quando o aviso estiver presente.
- **Limite de 10 por página** no caminho web, independente de `limit`.

Sem esse conhecimento o agente:
1. Chama com `id_unidade_geradora` achando que filtra por unidade
2. Não usa aspas e recebe resultados irrelevantes
3. Não sabe interpretar o `aviso` no retorno

## 2. Evidência

Sessão de 2026-06-11: busca por `VALDEVINO ALVES DE MIRANDA aposentadoria`
(sem aspas) retornou 9 resultados, todos com `unidade` corrompida. A busca
com aspas (`"VALDEVINO ALVES DE MIRANDA" aposentadoria`) identificou
corretamente três processos tipados como Aposentadoria entre os candidatos.
O agente não tentou aspas porque a docstring não menciona esse padrão.

## 3. Mudanças propostas

### 3.1 Fix de parsing — separar `unidade`, `usuario`, `inclusao`

**Arquivo**: `src/todos/sei_web_client.py`

Substituir:

```python
unidade_m = re.search(r"Unidade:\s*([^|]+)", meta)
inclusao_m = re.search(r"Inclusão:\s*(\S+)", meta)

results.append({
    "protocoloFormatado": prot,
    "tipo": tipo,
    "trecho": trecho,
    "unidade": unidade_m.group(1).strip() if unidade_m else "",
    "inclusao": inclusao_m.group(1) if inclusao_m else "",
})
```

Por:

```python
unidade_m  = re.search(r"Unidade:\s*(.+?)(?=\s+Usuário:|\s+Inclusão:|$)", meta)
usuario_m  = re.search(r"Usuário:\s*(\S+)", meta)
inclusao_m = re.search(r"Inclusão:\s*(\S+)", meta)

results.append({
    "protocoloFormatado": prot,
    "tipo": tipo,
    "trecho": trecho,
    "unidade": unidade_m.group(1).strip() if unidade_m else "",
    "usuario": usuario_m.group(1) if usuario_m else "",
    "inclusao": inclusao_m.group(1) if inclusao_m else "",
})
```

### 3.2 Docstring da tool `sei_pesquisar_processos`

**Arquivo**: `src/todos/server.py`

Adicionar seção explicando o caminho web:

```
Busca via web (instâncias sem mod-wssei):
- Quando REST não está disponível (sem SEI_URL ou mod-wssei ausente), a
  busca é feita via scraping do formulário de pesquisa avançada do SEI.
- Use aspas para frase exata: palavras_chave='"NOME COMPLETO" aposentadoria'
  é muito mais preciso do que palavras soltas.
- A busca web é full-text em todo o SEI (não filtrada por unidade).
- Filtros estruturais (id_unidade_geradora, id_assunto, grupo, sta_tipo_data)
  são ignorados no caminho web — quando descartados, o campo "aviso" no
  retorno lista os filtros que não foram aplicados.
- Retorno inclui campo "fonte": "web" quando o caminho web foi usado.
- Máximo de 10 resultados por página no caminho web.
```

### 3.3 Docstring do método `pesquisar_processos_web`

**Arquivo**: `src/todos/sei_web_client.py`

Acrescentar ao docstring do método os campos retornados (com `usuario`) e
a nota sobre aspas para frase exata.

## 4. Fora de escopo

- Implementar os filtros estruturais no caminho web (requer mapear os
  campos do formulário avançado para cada filtro — trabalho separado).
- Paginação automática (buscar todas as páginas e concatenar).
- Parsear o campo `trecho` (atualmente retorna `"..."` quando SEI não
  indexou o conteúdo — comportamento do Solr, não do scraper).

## 5. Impacto

- Nenhuma quebra de contrato: `usuario` é campo novo, os demais ficam.
- `unidade` passa a retornar apenas a sigla da unidade (correção de bug).
- Docstring é informativa, não muda assinatura.
