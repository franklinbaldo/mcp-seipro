"""Cache em disco com TTL para dados de catálogo do SEI.

Armazena JSON em ~/.cache/todos/ (criado automaticamente).
Padrão: TTL de 24 h — catálogos raramente mudam mas o servidor MCP
pode ser reiniciado com frequência (Claude Desktop).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

_CACHE_DIR = Path("~/.cache/todos").expanduser()
DEFAULT_TTL = 86_400  # 24 h


class DiskCache:
    """Cache JSON em disco com TTL simples."""

    def __init__(self, ttl: int = DEFAULT_TTL) -> None:  # noqa: D107
        self.ttl = ttl
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return _CACHE_DIR / f"{safe}.json"

    def get(self, key: str) -> object | None:
        """Retorna valor cacheado ou None se ausente/expirado."""
        path = self._path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - data["ts"] > self.ttl:
                return None
            return data["value"]
        except Exception:  # noqa: BLE001
            return None

    def set(self, key: str, value: object) -> None:
        """Write value to disk with current timestamp."""
        import contextlib  # noqa: PLC0415

        path = self._path(key)
        with contextlib.suppress(Exception):
            path.write_text(
                json.dumps({"ts": time.time(), "value": value}, ensure_ascii=False),
                encoding="utf-8",
            )

    def delete(self, key: str) -> None:
        """Remove entrada do cache."""
        self._path(key).unlink(missing_ok=True)


_default_cache = DiskCache()


def cache_get(key: str, ttl: int = DEFAULT_TTL) -> object | None:
    """Atalho de módulo para leitura do cache padrão."""
    c = _default_cache if ttl == DEFAULT_TTL else DiskCache(ttl)
    return c.get(key)


def cache_set(key: str, value: object) -> None:
    """Atalho de módulo para escrita no cache padrão."""
    _default_cache.set(key, value)
