"""Shared pytest fixtures."""

import pytest

from em0_mcp_wrapper import client, config


@pytest.fixture(autouse=True)
def reset_em0_test_config(monkeypatch):
    """Keep global config mutations from leaking between tests."""
    monkeypatch.setattr(config, "MEM0_API_URL", "https://test-mem0.example.com")
    monkeypatch.setattr(config, "MEM0_API_KEY", "test-key")
    monkeypatch.setattr(config, "REQUEST_TIMEOUT", 5)
    monkeypatch.setattr(client, "MAX_RETRIES", 2)
    monkeypatch.setattr(client, "RETRY_DELAY", 0)
