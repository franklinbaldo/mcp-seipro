"""Coordenador de backends REST vs. web para o SEI.

Decide qual backend usar com base na disponibilidade do mod-wssei:
- SEI_URL configurada → REST disponível (tenta REST primeiro)
- Sem SEI_URL ou REST falha → web scraper (SEIWebClient)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from todos.sei_client import SEIClient
    from todos.sei_web_client import SEIWebClient


class SEIBackend:
    """Wrapper que encapsula REST + web e expõe qual está disponível."""

    def __init__(self, rest: SEIClient, web: SEIWebClient) -> None:  # noqa: D107
        self.rest = rest
        self.web = web

    @property
    def has_rest(self) -> bool:
        """True quando o cliente REST tem base_url configurada (mod-wssei disponível)."""
        return bool(self.rest.base_url)
