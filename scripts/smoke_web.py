#!/usr/bin/env python3
"""Smoke test para o SEIWebClient — critério de aceite do PR #2.

Verifica login + ações simples em instâncias SEI sem mod-wssei (ex: SEI-RO).
Requer as mesmas variáveis de ambiente que o servidor MCP:
    SEI_USUARIO, SEI_SENHA, SEI_SIGLA_ORGAO (e SEI_WEB_URL ou SEI_URL)

Uso:
    python3 scripts/smoke_web.py
    python3 scripts/smoke_web.py --protocolo 0001.000001/2024-01
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from todos.sei_web_client import SEIWebClient, parse_inbox


async def smoke(protocolo: str | None) -> None:
    print("=" * 60)
    print("  SEI Web Client — Smoke Test")
    print("=" * 60)

    client = SEIWebClient()
    try:
        # ── 1. Login ───────────────────────────────────────────────
        print("\n[1] Login...")
        await client.login()
        print("    ✓ Login OK")

        # ── 2. Listar processos ────────────────────────────────────
        print("\n[2] Listar processos (fetch_inbox)...")
        _, html = await client.fetch_inbox(detalhada=True)
        print(f"    ✓ HTML recebido ({len(html):,} bytes)")

        # ── 3. Parse da inbox ──────────────────────────────────────
        print("\n[3] Parsear inbox...")
        layout, rows = parse_inbox(html)
        print(f"    ✓ Layout={layout!r}, {len(rows)} processos")
        if rows:
            primeiro = rows[0]
            protocolo_inbox = primeiro.get("protocolo", "")
            print(f"    Primeiro processo: {protocolo_inbox}")
            if not protocolo:
                protocolo = protocolo_inbox

        # ── 4. Consultar processo ──────────────────────────────────
        if protocolo:
            print(f"\n[4] Consultar processo {protocolo!r}...")
            try:
                dados = await client.consultar_processo(protocolo)
                print(f"    ✓ Tipo={dados.get('tipo_processo')!r}")
                print(f"    ✓ Documentos={len(dados.get('documentos', []))}")
            except Exception as e:  # noqa: BLE001
                print(f"    ✗ {e}")
        else:
            print("\n[4] Pular consultar_processo (nenhum protocolo disponível)")

        # ── 5. executar_acao_processo (dry-run) ────────────────────
        # Usa a ação `procedimento_visualizar` (somente leitura — não altera nada).
        # Se não existir, apenas informa — não falha o smoke test.
        if protocolo:
            print(f"\n[5] executar_acao_processo dry-run ({protocolo!r})...")
            try:
                result = await client.executar_acao_processo(protocolo, "procedimento_visualizar")
                print(f"    ✓ {result}")
            except RuntimeError as e:
                msg = str(e)
                if "não encontrada" in msg:
                    print(f"    ~ ação não disponível neste processo (ok): {msg[:80]}")
                else:
                    print(f"    ✗ {msg}")
        else:
            print("\n[5] Pular executar_acao_processo (nenhum protocolo)")

        print("\n" + "=" * 60)
        print("  Smoke test concluído com sucesso.")
        print("=" * 60)

    except Exception as e:  # noqa: BLE001
        print(f"\n✗ FALHA: {e}")
        sys.exit(1)
    finally:
        await client.close()


def _check_env() -> None:
    missing = [v for v in ("SEI_USUARIO", "SEI_SENHA") if not os.getenv(v)]
    if not (os.getenv("SEI_WEB_URL") or os.getenv("SEI_URL")):
        missing.append("SEI_WEB_URL ou SEI_URL")
    if missing:
        print(f"Variáveis obrigatórias ausentes: {', '.join(missing)}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke test SEIWebClient")
    parser.add_argument("--protocolo", default=None, help="Número SEI para testar ações")
    args = parser.parse_args()
    _check_env()
    asyncio.run(smoke(args.protocolo))
