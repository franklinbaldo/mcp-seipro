#!/usr/bin/env python3
"""Smoke test do transporte stdio do servidor MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent


def _text_result(result, tool_name: str) -> str:  # noqa: ANN001
    if result.isError or not result.content:
        msg = f"Chamada {tool_name} falhou: {result}"
        raise RuntimeError(msg)

    content = result.content[0]
    if not isinstance(content, TextContent):
        msg = f"{tool_name} retornou conteudo inesperado: {type(content).__name__}"
        raise TypeError(msg)
    return content.text


async def smoke(*, live: bool = False) -> None:
    """Valida handshake, catalogo de tools e uma chamada sem acesso ao SEI."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "todos"],
        cwd=Path.cwd(),
        env=dict(os.environ),
    )

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        tools = await session.list_tools()
        names = [tool.name for tool in tools.tools]

        print(f"Servidor: {initialized.serverInfo.name} {initialized.serverInfo.version}")  # noqa: T201
        print(f"Protocolo: {initialized.protocolVersion}")  # noqa: T201
        print(f"Tools: {len(names)}")  # noqa: T201

        if "sei_estilos" not in names:
            msg = "Tool sei_estilos nao encontrada."
            raise RuntimeError(msg)

        result = await session.call_tool("sei_estilos", {})
        text = _text_result(result, "sei_estilos")
        print(f"sei_estilos: OK ({len(text)} caracteres)")  # noqa: T201

        if live:
            current_unit = await session.call_tool("sei_unidade_atual", {})
            current_unit_data = json.loads(_text_result(current_unit, "sei_unidade_atual"))
            if "error" in current_unit_data:
                msg = f"sei_unidade_atual: {current_unit_data['error']}"
                raise RuntimeError(msg)
            missing_fields = {"id_unidade", "sigla", "nome"} - current_unit_data.keys()
            if missing_fields:
                msg = f"sei_unidade_atual sem campos: {sorted(missing_fields)}"
                raise RuntimeError(msg)
            unit_code = current_unit_data.get("sigla", "")
            print(f"sei_unidade_atual: OK ({unit_code})")  # noqa: T201

            listing = await session.call_tool("sei_listar_processos", {})
            listing_data = json.loads(_text_result(listing, "sei_listar_processos"))
            if "error" in listing_data:
                msg = f"sei_listar_processos: {listing_data['error']}"
                raise RuntimeError(msg)

            processes = listing_data.get("processos", [])
            print(f"sei_listar_processos: OK ({len(processes)} processos)")  # noqa: T201
            if not processes:
                return

            protocol = processes[0].get("protocolo")
            if not protocol:
                msg = "Primeiro processo nao possui protocolo."
                raise RuntimeError(msg)

            query = await session.call_tool(
                "sei_consultar_processo",
                {"protocolo_formatado": protocol},
            )
            query_data = json.loads(_text_result(query, "sei_consultar_processo"))
            if "error" in query_data:
                msg = f"sei_consultar_processo: {query_data['error']}"
                raise RuntimeError(msg)

            document_count = len(query_data.get("documentos", []))
            print(f"sei_consultar_processo: OK ({document_count} documentos)")  # noqa: T201


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Testa tools de leitura no SEI real")
    args = parser.parse_args()
    asyncio.run(smoke(live=args.live))
