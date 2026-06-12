"""Cache persistente para catálogos estáveis do SEI."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CATALOG_CACHE_TTL = 24 * 60 * 60


class CatalogCache:
    """Armazena respostas JSON em disco com TTL usando SQLite (sem dependências externas)."""

    def __init__(self, directory: Path) -> None:
        """Inicialize o armazenamento no diretório informado."""
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.directory / "catalogs.db"
        self._init_db()

    def _init_db(self) -> None:
        """Inicializa a tabela SQLite se ela não existir."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS catalogs (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    expires_at REAL
                )
                """
            )

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
            db_key = self.make_key(namespace, key)
            now = time.time()
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT value, expires_at FROM catalogs WHERE key = ?",
                    (db_key,),
                )
                row = cursor.fetchone()
                if row:
                    val_str, expires_at = row
                    if expires_at > now:
                        return json.loads(val_str)
                    # Limpa entrada expirada
                    conn.execute("DELETE FROM catalogs WHERE key = ?", (db_key,))
        except Exception:  # noqa: BLE001
            logger.warning("Falha ao ler cache de catalogos", exc_info=True)
        return None

    async def set(self, namespace: dict[str, str], key: str, value: Any) -> None:  # noqa: ANN401
        """Persista uma resposta bem-sucedida pelo TTL padrão."""
        try:
            db_key = self.make_key(namespace, key)
            val_str = json.dumps(value, ensure_ascii=False)
            expires_at = time.time() + CATALOG_CACHE_TTL
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO catalogs (key, value, expires_at)
                    VALUES (?, ?, ?)
                    """,
                    (db_key, val_str, expires_at),
                )
        except Exception:  # noqa: BLE001
            logger.warning("Falha ao gravar cache de catalogos", exc_info=True)

    async def ttl(self, namespace: dict[str, str], key: str) -> float | None:
        """Retorne o TTL restante de uma entrada."""
        try:
            db_key = self.make_key(namespace, key)
            now = time.time()
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT expires_at FROM catalogs WHERE key = ?",
                    (db_key,),
                )
                row = cursor.fetchone()
                if row:
                    expires_at = row[0]
                    return max(0.0, expires_at - now)
        except Exception:  # noqa: BLE001
            logger.warning("Falha ao consultar TTL do cache de catalogos", exc_info=True)
        return None

    async def close(self) -> None:
        """Feche o armazenamento em disco."""


@lru_cache(maxsize=1)
def get_catalog_cache() -> CatalogCache:
    """Retorna o cache compartilhado pelo processo."""
    configured = os.environ.get("TODOS_CACHE_DIR")
    directory = Path(configured).expanduser() if configured else Path.home() / ".cache" / "todos"
    return CatalogCache(directory)
