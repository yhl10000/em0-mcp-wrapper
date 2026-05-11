"""Tests for production server dependency declarations."""

import pathlib


def test_server_requirements_include_neo4j_driver():
    requirements = (
        pathlib.Path(__file__).resolve().parents[1] / "server" / "requirements.txt"
    ).read_text()

    assert any(line.startswith("neo4j") for line in requirements.splitlines())
