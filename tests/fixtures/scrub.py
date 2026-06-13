"""PII scrubber for SEI HTML fixtures.

Replaces personally identifiable information with deterministic anonymous
tokens so the scrubbed HTML can be committed to the repository safely.

Anonymization is consistent within a single scrub() call: the same CPF always
maps to the same replacement, preserving structural validity for parser tests.
"""

from __future__ import annotations

import re
from hashlib import md5


def _token(kind: str, value: str) -> str:
    """Deterministic 4-digit suffix so repeated occurrences stay consistent."""
    suffix = md5(value.encode(), usedforsecurity=False).hexdigest()[:4].upper()
    return f"{kind}-{suffix}"


# ---------------------------------------------------------------------------
# Individual scrubbers
# ---------------------------------------------------------------------------

_CPF_RE = re.compile(r"\b(\d{3})\.?(\d{3})\.?(\d{3})-?(\d{2})\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# Names inside alert() calls — onclick and script blocks
_ALERT_NAME_RE = re.compile(r"""(alert\(['"'])([^'"]{3,80})(['"])""")
# infraTooltipMostrar calls expose names as first arg
_TOOLTIP_RE = re.compile(r"""(infraTooltipMostrar\(['"'])([^'"]{3,80})(['"])""")
# Data attributes and hidden fields that may carry CPF
_VALUE_CPF_RE = re.compile(r"""(value=['"])(\d{11})(['"])""")


def _scrub_cpf(html: str) -> str:
    seen: dict[str, str] = {}

    def replace(m: re.Match) -> str:
        raw = m.group(0)
        if raw not in seen:
            seen[raw] = f"000.000.{_token('CPF', raw)}"
        return seen[raw]

    return _CPF_RE.sub(replace, html)


def _scrub_email(html: str) -> str:
    seen: dict[str, str] = {}

    def replace(m: re.Match) -> str:
        raw = m.group(0)
        if raw not in seen:
            seen[raw] = f"usuario-{_token('USR', raw).lower()}@anonimo.gov.br"
        return seen[raw]

    return _EMAIL_RE.sub(replace, html)


def _scrub_alert_names(html: str) -> str:
    seen: dict[str, str] = {}

    def replace(m: re.Match) -> str:
        name = m.group(2)
        if name not in seen:
            seen[name] = f"NOME {_token('NM', name)}"
        return m.group(1) + seen[name] + m.group(3)

    return _ALERT_NAME_RE.sub(replace, html)


def _scrub_tooltip_names(html: str) -> str:
    seen: dict[str, str] = {}

    def replace(m: re.Match) -> str:
        name = m.group(2)
        if name not in seen:
            seen[name] = f"NOME {_token('NM', name)}"
        return m.group(1) + seen[name] + m.group(3)

    return _TOOLTIP_RE.sub(replace, html)


def _scrub_value_cpf(html: str) -> str:
    seen: dict[str, str] = {}

    def replace(m: re.Match) -> str:
        raw = m.group(2)
        if raw not in seen:
            seen[raw] = f"00000{_token('CPF', raw)}"
        return m.group(1) + seen[raw] + m.group(3)

    return _VALUE_CPF_RE.sub(replace, html)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrub(html: str) -> str:
    """Remove PII from a SEI HTML page, returning anonymized HTML.

    Scrubbing is deterministic: the same input always produces the same output.
    The same value is always replaced by the same token within the call,
    preserving cross-reference consistency (e.g., same CPF in two places
    becomes the same replacement in both).
    """
    html = _scrub_cpf(html)
    html = _scrub_value_cpf(html)
    html = _scrub_email(html)
    html = _scrub_alert_names(html)
    return _scrub_tooltip_names(html)
