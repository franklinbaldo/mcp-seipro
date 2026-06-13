#!/usr/bin/env python3
"""Smoke test do cache persistente de catálogos."""

import asyncio
import tempfile
from pathlib import Path

from todos.catalog_cache import CATALOG_CACHE_TTL, CatalogCache


async def main() -> None:
    """Validate persistence, TTL and namespace hashing."""
    namespace = {
        "base_url": "https://sei.example",
        "usuario": "secret-user",
        "orgao": "1",
        "contexto": "teste",
    }

    with tempfile.TemporaryDirectory() as temp:
        first = CatalogCache(Path(temp))
        await first.set(namespace, "tipos", {"items": [1, 2, 3]})

        second = CatalogCache(Path(temp))
        value = await second.get(namespace, "tipos")
        if value != {"items": [1, 2, 3]}:
            msg = "Valor persistido nao foi recuperado."
            raise RuntimeError(msg)

        key = first.make_key(namespace, "tipos")
        if "secret-user" in key or "sei.example" in key:
            msg = "A chave do cache expoe dados do namespace."
            raise RuntimeError(msg)

        ttl = await second.ttl(namespace, "tipos")
        if ttl is None or not CATALOG_CACHE_TTL - 10 <= ttl <= CATALOG_CACHE_TTL:
            msg = f"TTL inesperado: {ttl}"
            raise RuntimeError(msg)

        print(f"Disk cache: OK (ttl={ttl:.0f}s)")  # noqa: T201
        await first.close()
        await second.close()


if __name__ == "__main__":
    asyncio.run(main())
