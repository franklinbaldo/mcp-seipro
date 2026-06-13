"""Unit tests for setup_wizard._detect_modsei_url.

All tests are offline — httpx.Client is patched so no live server is needed.

Detection strategy: POST /autenticar with empty credentials.
  - Module present  → PHP returns 400/422  → confirmed=True
  - Module absent   → Apache returns 404   → url="", confirmed=False
  - Cloudflare WAF  → 403 + cf-ray header  → confirmed=False, url=best-guess candidate

Reference instances (confirmed live):
  - SEI-RO  https://sei.sistemas.ro.gov.br  — no mod-wssei (all endpoints → Apache 404)
  - ANTAQ   https://sei.antaq.gov.br        — has mod-wssei, behind Cloudflare
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from todos.setup_wizard import _detect_modsei_url, _is_cloudflare_response, _ModseiDetection

SEI_RO = "https://sei.sistemas.ro.gov.br"
SEI_ANTAQ = "https://sei.antaq.gov.br"


def _ctx_mock(*responses: tuple[int, dict]) -> MagicMock:
    """Build a mock httpx.Client context manager.

    Each element of *responses is (status_code, headers_dict).
    .post() returns them in order.
    """
    mocks = []
    for status, headers in responses:
        r = MagicMock()
        r.status_code = status
        r.headers = headers
        mocks.append(r)
    mock_client = MagicMock()
    mock_client.post.side_effect = mocks
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_client)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _php(status: int) -> tuple[int, dict]:
    """Response coming from the PHP application (no Cloudflare headers)."""
    return (status, {})


def _cf(status: int = 403) -> tuple[int, dict]:
    """Response coming from Cloudflare's edge."""
    return (status, {"cf-ray": "abc123", "server": "cloudflare"})


# ---------------------------------------------------------------------------
# _is_cloudflare_response
# ---------------------------------------------------------------------------


class TestIsCloudflareResponse:
    def test_cf_ray_header_detected(self) -> None:
        resp = MagicMock()
        resp.headers = {"cf-ray": "abc123-GRU"}
        assert _is_cloudflare_response(resp) is True

    def test_server_cloudflare_detected(self) -> None:
        resp = MagicMock()
        resp.headers = {"server": "cloudflare"}
        assert _is_cloudflare_response(resp) is True

    def test_apache_not_cloudflare(self) -> None:
        resp = MagicMock()
        resp.headers = {"server": "Apache/2.4"}
        assert _is_cloudflare_response(resp) is False

    def test_empty_headers_not_cloudflare(self) -> None:
        resp = MagicMock()
        resp.headers = {}
        assert _is_cloudflare_response(resp) is False


# ---------------------------------------------------------------------------
# _detect_modsei_url
# ---------------------------------------------------------------------------


class TestDetectModseiUrl:
    @patch("todos.setup_wizard.httpx.Client")
    def test_ro_not_installed_both_paths_404(self, mock_cls: MagicMock) -> None:
        # SEI-RO: Apache returns 404 for both paths — module absent.
        mock_cls.return_value = _ctx_mock(_php(404), _php(404))
        result = _detect_modsei_url(SEI_RO, verify_ssl=True)
        assert result == _ModseiDetection(url="", confirmed=False)

    @patch("todos.setup_wizard.httpx.Client")
    def test_antaq_wssei_confirmed(self, mock_cls: MagicMock) -> None:
        # PHP responds 422 (bad creds) → confirmed present at wssei/ path.
        mock_cls.return_value = _ctx_mock(_php(422))
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.confirmed is True
        assert "/wssei/" in result.url
        assert result.url.endswith("/api/v2")

    @patch("todos.setup_wizard.httpx.Client")
    def test_alt_modwssei_path_confirmed(self, mock_cls: MagicMock) -> None:
        # wssei/ → 404, mod-wssei/ → 422: returns mod-wssei path confirmed.
        mock_cls.return_value = _ctx_mock(_php(404), _php(422))
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.confirmed is True
        assert "/mod-wssei/" in result.url

    @patch("todos.setup_wizard.httpx.Client")
    def test_cloudflare_both_paths_unconfirmed(self, mock_cls: MagicMock) -> None:
        # Both paths blocked by Cloudflare → unconfirmed, first candidate returned.
        mock_cls.return_value = _ctx_mock(_cf(), _cf())
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.confirmed is False
        assert "/wssei/" in result.url  # first candidate returned as best-guess

    @patch("todos.setup_wizard.httpx.Client")
    def test_cloudflare_first_then_php_confirms(self, mock_cls: MagicMock) -> None:
        # wssei/ → Cloudflare, mod-wssei/ → PHP 422: second path confirmed wins.
        mock_cls.return_value = _ctx_mock(_cf(), _php(422))
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.confirmed is True
        assert "/mod-wssei/" in result.url

    @patch("todos.setup_wizard.httpx.Client")
    def test_cloudflare_first_then_absent(self, mock_cls: MagicMock) -> None:
        # wssei/ → Cloudflare, mod-wssei/ → 404: returns unconfirmed wssei guess.
        mock_cls.return_value = _ctx_mock(_cf(), _php(404))
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.confirmed is False
        assert "/wssei/" in result.url

    @patch("todos.setup_wizard.httpx.Client")
    def test_network_error_returns_empty(self, mock_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.post.side_effect = httpx.ConnectError("connection refused")
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=mock_client)
        ctx.__exit__ = MagicMock(return_value=False)
        mock_cls.return_value = ctx
        assert _detect_modsei_url(SEI_RO, verify_ssl=True) == _ModseiDetection(
            url="", confirmed=False
        )

    @patch("todos.setup_wizard.httpx.Client")
    def test_501_treated_as_absent(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = _ctx_mock(_php(501), _php(501))
        assert _detect_modsei_url(SEI_RO, verify_ssl=True).url == ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_verify_ssl_passed_through(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = _ctx_mock(_php(404), _php(404))
        _detect_modsei_url(SEI_RO, verify_ssl=False)
        _, kwargs = mock_cls.call_args
        assert kwargs.get("verify") is False

    @pytest.mark.parametrize("status", [400, 401, 403, 422])
    @patch("todos.setup_wizard.httpx.Client")
    def test_php_non_404_confirms_presence(self, mock_cls: MagicMock, status: int) -> None:
        # Plain PHP responses (no Cloudflare headers) → confirmed regardless of status.
        mock_cls.return_value = _ctx_mock(_php(status))
        result = _detect_modsei_url(SEI_ANTAQ, verify_ssl=True)
        assert result.confirmed is True
        assert result.url != ""

    @patch("todos.setup_wizard.httpx.Client")
    def test_probe_uses_post_autenticar(self, mock_cls: MagicMock) -> None:
        mock_cls.return_value = _ctx_mock(_php(404), _php(404))
        _detect_modsei_url(SEI_RO, verify_ssl=True)
        mock_client = mock_cls.return_value.__enter__.return_value
        assert mock_client.post.called
        assert not mock_client.get.called
        called_url = mock_client.post.call_args_list[0][0][0]
        assert called_url.endswith("/autenticar")
