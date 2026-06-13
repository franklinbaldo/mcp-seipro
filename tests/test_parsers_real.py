"""Parser tests against real (anonymized) SEI HTML fixtures.

These tests complement test_parsers.py (which uses hand-crafted synthetic HTML)
by running the same parsers on actual pages captured from a live SEI instance
and anonymized with tests/fixtures/scrub.py.

Fixtures are NOT committed by default — generate them locally with:
    uv run python -m tests.fixtures.capture

Tests skip automatically if the fixtures directory is empty.
"""

from __future__ import annotations

import re

import pytest

import tests.fixtures as fx
from tests.fixtures.scrub import scrub
from todos.sei_web_client import (
    _extrair_erro_sei,
    parse_arvore_nos,
    parse_inbox,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AVAILABLE = fx.available()


def fixture(slug: str) -> pytest.param:
    """Parametrize helper — skips if fixture file is absent."""
    if slug in _AVAILABLE:
        return pytest.param(fx.load(slug), id=slug)
    return pytest.param(
        pytest.skip(f"fixture '{slug}' not captured yet — run tests/fixtures/capture.py"),
        id=slug,
    )


# ---------------------------------------------------------------------------
# Inbox parser
# ---------------------------------------------------------------------------


@pytest.mark.skipif("inbox" not in _AVAILABLE, reason="inbox fixture not captured")
class TestParseInboxReal:
    def test_returns_list(self) -> None:
        _layout, processos = parse_inbox(fx.load("inbox"))
        assert isinstance(processos, list)

    def test_each_process_has_protocol(self) -> None:
        _layout, processos = parse_inbox(fx.load("inbox"))
        for p in processos:
            assert "protocolo" in p, f"missing 'protocolo' in {p}"

    def test_no_pii_cpf(self) -> None:
        html = fx.load("inbox")
        real_cpf = re.compile(r"\b(?!000)\d{3}\.\d{3}\.\d{3}-\d{2}\b")
        assert not real_cpf.search(html), "Found un-scrubbed CPF in inbox fixture"

    def test_no_pii_name_in_alert(self) -> None:
        html = fx.load("inbox")
        raw_name = re.compile(r"alert\('[A-ZÁÀÃÉÍÓÚÇ][a-záàãéíóúç]+ [A-ZÁÀÃÉÍÓÚÇ]")
        assert not raw_name.search(html), "Found un-scrubbed name in alert() in inbox fixture"


# ---------------------------------------------------------------------------
# Tree (arvore) parser
# ---------------------------------------------------------------------------


@pytest.mark.skipif("arvore" not in _AVAILABLE, reason="arvore fixture not captured")
class TestParseArvoreReal:
    def test_returns_list(self) -> None:
        nos = parse_arvore_nos(fx.load("arvore"))
        assert isinstance(nos, list)
        assert len(nos) > 0

    def test_each_node_has_id(self) -> None:
        nos = parse_arvore_nos(fx.load("arvore"))
        for no in nos:
            assert "id" in no, f"missing 'id' in node {no}"

    def test_each_node_has_label(self) -> None:
        nos = parse_arvore_nos(fx.load("arvore"))
        for no in nos:
            assert "label" in no or "nome_composto" in no, f"missing label in {no}"


# ---------------------------------------------------------------------------
# Error extractor — should find no false positives in real pages
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _AVAILABLE,
    reason="no fixtures captured yet",
)
@pytest.mark.parametrize("slug", _AVAILABLE)
def test_extrair_erro_sei_no_false_positive(slug: str) -> None:
    """_extrair_erro_sei must return None for all normal (non-error) pages."""
    html = fx.load(slug)
    result = _extrair_erro_sei(html)
    assert result is None, (
        f"_extrair_erro_sei returned a false positive on '{slug}' fixture:\n{result!r}"
    )


# ---------------------------------------------------------------------------
# Scrubber unit tests (no fixture files needed)
# ---------------------------------------------------------------------------


class TestScrubber:
    def test_cpf_formatted(self) -> None:
        out = scrub("CPF: 123.456.789-09")
        assert "123.456.789-09" not in out
        assert "000.000." in out

    def test_cpf_unformatted(self) -> None:
        out = scrub("cpf=12345678909")
        assert "12345678909" not in out

    def test_email(self) -> None:
        out = scrub("enviar para joao.silva@pge.ro.gov.br amanhã")
        assert "joao.silva@pge.ro.gov.br" not in out
        assert "@anonimo.gov.br" in out

    def test_alert_name(self) -> None:
        out = scrub("""onclick="alert('Maria Silva')" """)
        assert "Maria Silva" not in out
        assert "NOME" in out

    def test_same_value_same_token(self) -> None:
        html = "123.456.789-09 e novamente 123.456.789-09"
        out = scrub(html)
        parts = out.split(" e novamente ")
        assert parts[0] == parts[1], "Same CPF must produce same token"

    def test_different_values_different_tokens(self) -> None:
        out = scrub("111.111.111-11 e 222.222.222-22")
        tokens = [p.strip() for p in out.split(" e ")]
        assert tokens[0] != tokens[1], "Different CPFs must produce different tokens"
