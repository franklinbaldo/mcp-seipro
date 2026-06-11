"""Cache persistente para catálogos estáveis do SEI."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from key_value.aio.stores.disk import DiskStore

logger = logging.getLogger(__name__)

CATALOG_CACHE_TTL = 24 * 60 * 60


class CatalogCache:
    """Armazena respostas JSON em disco com TTL, sem bloquear chamadas ao SEI."""

    def __init__(self, directory: Path) -> None:
        """Inicialize o armazenamento no diretório informado."""
        self.directory = directory
        self._store = DiskStore(directory=directory, default_collection="catalogs")

    @staticmethod
    def make_key(namespace: dict[str, str], key: str) -> str:
        """Gera chave estável sem expor usuário ou URLs no banco."""
        payload = json.dumps(
            {"namespace": namespace, "key": key},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    async def get(self, namespace: dict[str, str], key: str) -> Any:  # noqa: ANN401
        """Retorna um valor válido ou None em miss/falha do cache."""
        try:
            entry = await self._store.get(self.make_key(namespace, key))
        except Exception:  # noqa: BLE001
            logger.warning("Falha ao ler cache de catalogos", exc_info=True)
            return None
        return entry.get("value") if entry is not None else None

    async def set(self, namespace: dict[str, str], key: str, value: Any) -> None:  # noqa: ANN401
        """Persista uma resposta bem-sucedida pelo TTL padrão."""
        try:
            await self._store.put(
                self.make_key(namespace, key),
                {"value": value},
                ttl=CATALOG_CACHE_TTL,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Falha ao gravar cache de catalogos", exc_info=True)

    async def ttl(self, namespace: dict[str, str], key: str) -> float | None:
        """Retorne o TTL restante de uma entrada."""
        try:
            _, ttl = await self._store.ttl(self.make_key(namespace, key))
        except Exception:  # noqa: BLE001
            logger.warning("Falha ao consultar TTL do cache de catalogos", exc_info=True)
            return None
        return ttl

    async def close(self) -> None:
        """Feche o armazenamento em disco."""
        await self._store.close()


@lru_cache(maxsize=1)
def get_catalog_cache() -> CatalogCache:
    """Retorna o cache compartilhado pelo processo."""
    configured = os.environ.get("TODOS_CACHE_DIR")
    directory = Path(configured).expanduser() if configured else Path.home() / ".cache" / "todos"
    return CatalogCache(directory)
