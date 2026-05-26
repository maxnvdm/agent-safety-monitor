"""Shared pytest fixtures and patches for the test suite."""

from __future__ import annotations

from unittest.mock import patch

import pytest


class _FakeEncoding:
    """Minimal tiktoken Encoding stub: encode returns one token per character."""

    def encode(self, text: str, **_kwargs) -> list[int]:
        return list(range(len(text)))


@pytest.fixture(autouse=True)
def _patch_tiktoken():
    """Patch tiktoken.get_encoding to avoid downloading BPE data in CI."""
    fake = _FakeEncoding()
    with patch("tiktoken.get_encoding", return_value=fake):
        yield
