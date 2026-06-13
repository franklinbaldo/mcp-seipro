"""Unit tests for setup_wizard._detect_modsei_url.

All tests are offline — httpx.Client is patched so no live server is needed.

Detection strategy: POST /autenticar with empty credentials.
  - Module present  → PHP returns 400/422 (bad credentials)
  - Module absent   → Apache returns plain 404 (path doesn't exist)

The SEI de Rondônia (https://sei.sistemas.ro.gov.br) is the reference
instance without mod-wssei (confirmed: all endpoints return Apache 404).
ANTAQ is the reference instance with mod-wssei at the wssei/ path.
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

    .post() returns responses with the given status codes in order.
    """
    responses = [MagicMock(status_code=sc) for sc in status_codes]
    mock_client = MagicMock()
    mock_client.post.side_effect = responses
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_client)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


class TestDetectModseiUrl:
    @patch("todos.setup_wizard.httpx.Client")
    def test_ro_not_installed_both_paths_404(self, mock_cls: MagicMock) -> None:
        # SEI-RO has no mod-wssei; Apache returns 404 for both candidate paths.
        mock_cls.return_value = _ctx_mock(404, 404)
        assert _detect_modsei_url(SEI_RO, verify_ssl=True) == ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_antaq_wssei_path_detected(self, mock_cls: MagicMock) -> None:
        # ANTAQ: wssei/ present, /autenticar rejects empty creds with 422.
        mock_cls.return_value = _ctx_mock(422)
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.startswith(SEI_ANTAQ)
        assert "/wssei/" in result
        assert result.endswith("/api/v2")

    @patch("todos.setup_wizard.httpx.Client")
    def test_alt_modwssei_path_detected(self, mock_cls: MagicMock) -> None:
        # Admin renamed module to mod-wssei/ (issue #46): first path 404, second 422.
        mock_cls.return_value = _ctx_mock(404, 422)
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.startswith(SEI_ANTAQ)
        assert "/mod-wssei/" in result
        assert result.endswith("/api/v2")

    @patch("todos.setup_wizard.httpx.Client")
    def test_400_bad_request_means_present(self, mock_cls: MagicMock) -> None:
        # Some mod-wssei versions return 400 for empty credentials.
        mock_cls.return_value = _ctx_mock(400)
        assert _detect_modsei_url(SEI_ANTAQ, verify_ssl=True) != ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_501_treated_as_not_installed(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = _ctx_mock(501, 501)
        assert _detect_modsei_url(SEI_RO, verify_ssl=True) == ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_network_error_returns_empty(self, mock_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.ConnectError("connection refused")
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

    @pytest.mark.parametrize("status", [400, 401, 403, 422])
    @patch("todos.setup_wizard.httpx.Client")
    def test_non_404_confirms_presence(self, mock_cls: MagicMock, status: int) -> None:
        mock_cls.return_value = _ctx_mock(status)
        assert _detect_modsei_url(SEI_ANTAQ, verify_ssl=True) != ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_autenticar_probe_sends_post(self, mock_cls: MagicMock) -> None:
        # Verify the probe uses POST /autenticar, not GET /versao.
        mock_cls.return_value = _ctx_mock(404, 404)
        _detect_modsei_url(SEI_RO, verify_ssl=True)
        mock_client = mock_cls.return_value.__enter__.return_value
        assert mock_client.post.called
        assert not mock_client.get.called
        called_url = mock_client.post.call_args_list[0][0][0]
        assert called_url.endswith("/autenticar")
