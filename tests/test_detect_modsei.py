"""Unit tests for setup_wizard._detect_modsei_url.

All tests are offline — httpx.Client is patched so no live server is needed.
The SEI de Rondônia (https://sei.sistemas.ro.gov.br) is the reference instance
without mod-wssei; ANTAQ is the reference instance with it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from todos.setup_wizard import _detect_modsei_url

SEI_RO = "https://sei.sistemas.ro.gov.br"
SEI_ANTAQ = "https://sei.antaq.gov.br"


def _ctx_mock(*status_codes: int) -> MagicMock:
    """Build a mock httpx.Client context manager.

    .get() returns responses with the given status codes in order.
    """
    responses = [MagicMock(status_code=sc) for sc in status_codes]
    mock_client = MagicMock()
    mock_client.get.side_effect = responses
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_client)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


class TestDetectModseiUrl:
    @patch("todos.setup_wizard.httpx.Client")
    def test_ro_not_installed_both_paths_404(self, mock_cls: MagicMock) -> None:
        # SEI-RO has no mod-wssei; both candidate paths return 404.
        mock_cls.return_value = _ctx_mock(404, 404)
        assert _detect_modsei_url(SEI_RO, verify_ssl=True) == ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_antaq_wssei_path_detected(self, mock_cls: MagicMock) -> None:
        # ANTAQ uses the wssei/ install; first probe returns 401 (auth required = present).
        mock_cls.return_value = _ctx_mock(401)
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.startswith(SEI_ANTAQ)
        assert "/wssei/" in result
        assert result.endswith("/api/v2")

    @patch("todos.setup_wizard.httpx.Client")
    def test_alt_modwssei_path_detected(self, mock_cls: MagicMock) -> None:
        # Admin renamed module to mod-wssei/ (issue #46); first path 404, second 401.
        mock_cls.return_value = _ctx_mock(404, 401)
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.startswith(SEI_ANTAQ)
        assert "/mod-wssei/" in result
        assert result.endswith("/api/v2")

    @patch("todos.setup_wizard.httpx.Client")
    def test_200_means_present(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = _ctx_mock(200)
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result != ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_501_treated_as_not_installed(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = _ctx_mock(501, 501)
        assert _detect_modsei_url(SEI_RO, verify_ssl=True) == ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_network_error_returns_empty(self, mock_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.ConnectError("connection refused")
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_cls.return_value = ctx
        assert _detect_modsei_url(SEI_RO, verify_ssl=True) == ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_verify_ssl_passed_through(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = _ctx_mock(404, 404)
        _detect_modsei_url(SEI_RO, verify_ssl=False)
        _, kwargs = mock_cls.call_args
        assert kwargs.get("verify") is False

    @pytest.mark.parametrize("status", [401, 403, 422])
    @patch("todos.setup_wizard.httpx.Client")
    def test_auth_errors_confirm_presence(self, mock_cls: MagicMock, status: int) -> None:
        mock_cls.return_value = _ctx_mock(status)
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result != ""
