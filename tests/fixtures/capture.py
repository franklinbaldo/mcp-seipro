"""Capture real SEI HTML pages and save anonymized fixtures.

Usage:
    uv run python -m tests.fixtures.capture [--output tests/fixtures/sei]

Requires the usual SEI_WEB_URL / SEI_USUARIO / SEI_SENHA / SEI_ORGAO env vars
(reads from .env automatically via python-dotenv if present).

Each captured page is:
  1. Fetched from the live SEI instance
  2. Scrubbed of PII (CPFs, names, emails)
  3. Written to <output>/<page-slug>.html

Run once whenever SEI page structure changes significantly enough to warrant
updating the test corpus. Commit the resulting .html files.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import httpx

from todos.exceptions import SEIError
from todos.sei_web_client import SEIWebClient

from .scrub import scrub

CAPTURE_PROCESS = "0016.269301/2020-39"


async def _capture_all(client: SEIWebClient) -> dict[str, str]:
    """Capture all target pages and return {slug: scrubbed_html}."""
    pages: dict[str, str] = {}

    async def save(slug: str, coro: Awaitable[str]) -> None:
        """Fetch one page and store its scrubbed HTML, skipping on failure."""
        try:
            html: str = await coro
            pages[slug] = scrub(html)
            sys.stdout.write(f"  ✓ {slug}\n")
        except (SEIError, httpx.HTTPError, OSError) as exc:
            sys.stderr.write(f"  ✗ {slug}: {exc}\n")

    await client.ensure_authenticated()

    await save("inbox", client._get_inbox_html())
    await save("arvore", client._get_arvore_html(CAPTURE_PROCESS))
    await save("historico", client._get_historico_html(CAPTURE_PROCESS))
    await save("procedimento_consultar", client._get_procedimento_consultar_html(CAPTURE_PROCESS))
    await save(
        "documento_interno",
        client.visualizar_documento_interno_web(CAPTURE_PROCESS, "13931287"),
    )

    return pages


async def _run(url: str, usuario: str, senha: str, orgao: str) -> dict[str, str]:
    """Authenticate and capture all pages."""
    async with SEIWebClient(
        base_url=url,
        usuario=usuario,
        senha=senha,
        sigla_orgao=orgao,
    ) as client:
        sys.stdout.write(f"Capturing from {url} ...\n")
        return await _capture_all(client)


def main() -> None:
    """Entry point for CLI invocation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "sei",
        help="Directory to write captured HTML files",
    )
    parser.add_argument(
        "--process",
        default=CAPTURE_PROCESS,
        help="Process protocol to use as capture target",
    )
    args = parser.parse_args()

    url = os.environ.get("SEI_WEB_URL") or os.environ.get("SEI_URL", "")
    usuario = os.environ.get("SEI_USUARIO", "")
    senha = os.environ.get("SEI_SENHA", "")
    orgao = os.environ.get("SEI_ORGAO", "RO")

    if not (url and usuario and senha):
        sys.stderr.write("ERROR: SEI_WEB_URL, SEI_USUARIO, SEI_SENHA must be set.\n")
        sys.exit(1)

    pages = asyncio.run(_run(url, usuario, senha, orgao))

    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)

    for slug, html in pages.items():
        dest = output / f"{slug}.html"
        dest.write_text(html, encoding="utf-8")
        sys.stdout.write(f"  → {dest} ({len(html):,} bytes)\n")

    sys.stdout.write(f"\nDone. {len(pages)} fixtures written to {output}/\n")


if __name__ == "__main__":
    main()
