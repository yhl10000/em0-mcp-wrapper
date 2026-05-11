"""Tests for mem0 API compatibility helpers."""

import importlib.util
import pathlib
import sys

_COMPAT_PATH = pathlib.Path(__file__).resolve().parents[1] / "server" / "mem0_compat.py"
_COMPAT_SPEC = importlib.util.spec_from_file_location("em0_mem0_compat", _COMPAT_PATH)
assert _COMPAT_SPEC is not None
assert _COMPAT_SPEC.loader is not None
mem0_compat = importlib.util.module_from_spec(_COMPAT_SPEC)
sys.modules[_COMPAT_SPEC.name] = mem0_compat
_COMPAT_SPEC.loader.exec_module(mem0_compat)


class FakeMem0Search:
    def __init__(self):
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return {"results": []}


class LegacyOnlySearch:
    def __init__(self):
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if "filters" in kwargs:
            raise TypeError("unexpected filters")
        return {"results": []}


def test_search_memory_uses_filters_for_user_id():
    fake = FakeMem0Search()

    result = mem0_compat.search_memory(fake, query="postgres", user_id="proj", limit=3)

    assert result == {"results": []}
    assert fake.calls == [
        {"query": "postgres", "limit": 3, "filters": {"user_id": "proj"}}
    ]


def test_search_memory_merges_existing_filters():
    fake = FakeMem0Search()

    mem0_compat.search_memory(
        fake,
        query="postgres",
        user_id="proj",
        limit=3,
        filters={"metadata.domain": "backend"},
    )

    assert fake.calls[0]["filters"] == {
        "metadata.domain": "backend",
        "user_id": "proj",
    }


def test_search_memory_falls_back_to_legacy_user_id_shape():
    fake = LegacyOnlySearch()

    result = mem0_compat.search_memory(fake, query="postgres", user_id="proj", limit=3)

    assert result == {"results": []}
    assert fake.calls[-1] == {"query": "postgres", "user_id": "proj", "limit": 3}
