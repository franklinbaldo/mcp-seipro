"""Test fixtures for SEI HTML parser tests."""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "sei"


def load(slug: str) -> str:
    """Load a scrubbed HTML fixture by slug name (no extension).

    Raises FileNotFoundError with a clear message if the fixture is missing,
    reminding the developer to run the capture script.
    """
    path = FIXTURES_DIR / f"{slug}.html"
    if not path.exists():
        msg = (
            f"Fixture '{slug}.html' not found in {FIXTURES_DIR}.\n"
            "Run: uv run python -m tests.fixtures.capture"
        )
        raise FileNotFoundError(msg)
    return path.read_text(encoding="utf-8")


def available() -> list[str]:
    """Return slugs of all available fixtures."""
    return [p.stem for p in sorted(FIXTURES_DIR.glob("*.html"))]
