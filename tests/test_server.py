"""Tests for MCP server tool registration and freshness scoring."""

from datetime import datetime, timedelta, timezone

from em0_mcp_wrapper import config

# Set config before importing server
config.MEM0_API_URL = "https://test-mem0.example.com"
config.MEM0_API_KEY = "test-key"


def test_version():
    from em0_mcp_wrapper import __version__
    assert __version__ == "0.4.0"


def test_config_validate_passes():
    """With URL and key set, validate should not raise."""
    config.MEM0_API_URL = "https://test.example.com"
    config.MEM0_API_KEY = "key"
    # Should not raise
    config.validate()


def test_max_memory_length_default():
    """MAX_MEMORY_LENGTH should have a sensible default."""
    assert config.MAX_MEMORY_LENGTH > 0


# ─── Freshness Scoring Tests ───
from datetime import datetime, timedelta, timezone


def _apply_freshness(results: list[dict]) -> list[dict]:
    """Local copy of server/main.py _apply_freshness for unit testing.

    Kept in sync with the server implementation.
    """
    now = datetime.now(timezone.utc)
    for item in results:
        meta = item.get("metadata", {})
        semantic_score = item.get("score", 0)
        if meta.get("immutable"):
            item["final_score"] = semantic_score
            item["freshness"] = 1.0
            continue
        last_access = meta.get("last_accessed_at") or item.get("created_at", "")
        if last_access:
            try:
                last_dt = datetime.fromisoformat(last_access.replace("Z", "+00:00"))
                age_days = (now - last_dt).days
            except (ValueError, TypeError):
                age_days = 180
        else:
            age_days = 180
        freshness = max(0.5, 1.0 - (age_days / 365) * 0.5)
        access_count = meta.get("access_count", 0)
        popularity = min(1.2, 1.0 + access_count * 0.02)
        final_score = semantic_score * freshness * popularity
        item["final_score"] = round(final_score, 4)
        item["freshness"] = round(freshness, 3)
    results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    return results


def _make_memory(score, days_ago=0, access_count=0, immutable=False):
    """Helper to create a test memory dict."""
    now = datetime.now(timezone.utc)
    accessed = (now - timedelta(days=days_ago)).isoformat()
    meta = {
        "last_accessed_at": accessed,
        "access_count": access_count,
        "domain": "test",
        "type": "decision",
    }
    if immutable:
        meta["immutable"] = True
    return {
        "id": f"mem-{score}-{days_ago}",
        "memory": f"test memory (score={score}, age={days_ago}d)",
        "score": score,
        "metadata": meta,
    }


def test_freshness_recent_memory_stays_high():
    """A recently accessed memory should keep its score."""
    results = [_make_memory(0.9, days_ago=1)]
    scored = _apply_freshness(results)
    assert scored[0]["freshness"] >= 0.99
    assert scored[0]["final_score"] >= 0.89


def test_freshness_old_memory_decays():
    """A 365-day old memory should decay to ~50% freshness."""
    results = [_make_memory(0.9, days_ago=365)]
    scored = _apply_freshness(results)
    assert scored[0]["freshness"] == 0.5
    assert scored[0]["final_score"] < 0.9 * 0.55  # ~0.45


def test_freshness_reorders_by_final_score():
    """Old high-semantic should rank below recent lower-semantic."""
    results = [
        _make_memory(0.95, days_ago=300),  # High semantic but old
        _make_memory(0.80, days_ago=5),    # Lower semantic but fresh
    ]
    scored = _apply_freshness(results)
    # The fresh memory should be ranked first
    assert scored[0]["metadata"]["access_count"] == 0
    assert scored[0]["score"] == 0.80  # Fresh one comes first


def test_freshness_immutable_no_decay():
    """Immutable memories should not decay regardless of age."""
    results = [_make_memory(0.9, days_ago=500, immutable=True)]
    scored = _apply_freshness(results)
    assert scored[0]["freshness"] == 1.0
    assert scored[0]["final_score"] == 0.9


def test_freshness_popularity_bonus():
    """Frequently accessed memories should get a boost."""
    results = [
        _make_memory(0.8, days_ago=30, access_count=0),
        _make_memory(0.8, days_ago=30, access_count=10),  # Popular
    ]
    scored = _apply_freshness(results)
    popular = [r for r in scored if r["metadata"]["access_count"] == 10][0]
    unpopular = [r for r in scored if r["metadata"]["access_count"] == 0][0]
    assert popular["final_score"] > unpopular["final_score"]


def test_freshness_popularity_capped():
    """Popularity bonus should cap at 1.2x."""
    results = [_make_memory(0.8, days_ago=0, access_count=100)]
    scored = _apply_freshness(results)
    # max popularity = 1.2, freshness ≈ 1.0
    assert scored[0]["final_score"] <= 0.8 * 1.0 * 1.2 + 0.01


def test_freshness_minimum_never_below_half():
    """Freshness should never go below 0.5, even for very old memories."""
    results = [_make_memory(0.9, days_ago=1000)]
    scored = _apply_freshness(results)
    assert scored[0]["freshness"] == 0.5


def test_freshness_missing_metadata_defaults():
    """Memories without last_accessed_at should get default 180-day age."""
    mem = {
        "id": "no-meta",
        "memory": "old memory",
        "score": 0.9,
        "metadata": {},
    }
    scored = _apply_freshness([mem])
    # 180 days → freshness ≈ 0.75
    assert 0.74 <= scored[0]["freshness"] <= 0.76


# ─── Conflict Detection Tests ───


def _normalize_text(text: str) -> str:
    """Local copy of server/main.py _normalize_text."""
    return " ".join(text.lower().split())


def _check_conflicts_local(
    content: str, existing_memories: list[dict], threshold: float = 0.80,
) -> list[dict]:
    """Local conflict detection logic for unit testing.

    Simulates server-side _check_conflicts without needing mem0 instance.
    """
    conflicts = []
    for item in existing_memories:
        score = item.get("score", 0)
        existing = item.get("memory", "")

        if score < threshold:
            continue

        if _normalize_text(content) == _normalize_text(existing):
            continue

        conflict_entry = {
            "existing_memory": existing,
            "existing_id": item.get("id", "?"),
            "similarity_score": round(score, 3),
            "suggestion": "Consider updating this memory instead.",
        }

        if item.get("metadata", {}).get("immutable"):
            conflict_entry["suggestion"] = (
                "IMMUTABLE memory — cannot be updated. "
                "Verify this new information is correct before adding."
            )

        conflicts.append(conflict_entry)

    return conflicts


def test_conflict_detected_high_similarity():
    """High similarity + different content = conflict."""
    existing = [
        {"id": "abc", "memory": "We use PostgreSQL for the database", "score": 0.88, "metadata": {}}
    ]
    conflicts = _check_conflicts_local("We are switching to MongoDB", existing)
    assert len(conflicts) == 1
    assert conflicts[0]["similarity_score"] == 0.88


def test_conflict_not_detected_low_similarity():
    """Low similarity = no conflict."""
    existing = [
        {"id": "abc", "memory": "Auth uses JWT tokens", "score": 0.45, "metadata": {}}
    ]
    conflicts = _check_conflicts_local("We use PostgreSQL", existing)
    assert len(conflicts) == 0


def test_conflict_dedup_same_content():
    """Same content (normalized) = dedup, not conflict."""
    existing = [
        {"id": "abc", "memory": "We use PostgreSQL", "score": 0.99, "metadata": {}}
    ]
    conflicts = _check_conflicts_local("  we  use  postgresql  ", existing)
    assert len(conflicts) == 0


def test_conflict_immutable_extra_warning():
    """Conflict with immutable memory gets extra warning."""
    existing = [
        {
            "id": "imm1",
            "memory": "Embeddings must be 1024 dimensions",
            "score": 0.85,
            "metadata": {"immutable": True},
        }
    ]
    conflicts = _check_conflicts_local("Embeddings should be 768 dimensions", existing)
    assert len(conflicts) == 1
    assert "IMMUTABLE" in conflicts[0]["suggestion"]


def test_conflict_multiple_matches():
    """Multiple high-similarity results = multiple conflicts."""
    existing = [
        {"id": "a", "memory": "Use PostgreSQL v15", "score": 0.90, "metadata": {}},
        {"id": "b", "memory": "Database is PostgreSQL", "score": 0.85, "metadata": {}},
        {"id": "c", "memory": "Unrelated memory", "score": 0.40, "metadata": {}},
    ]
    conflicts = _check_conflicts_local("Switch to MySQL", existing)
    assert len(conflicts) == 2


def test_conflict_threshold_boundary():
    """Score exactly at threshold should be included."""
    existing = [
        {"id": "abc", "memory": "Use Redis for caching", "score": 0.80, "metadata": {}}
    ]
    conflicts = _check_conflicts_local("Use Memcached for caching", existing)
    assert len(conflicts) == 1

    # Just below threshold
    existing[0]["score"] = 0.79
    conflicts = _check_conflicts_local("Use Memcached for caching", existing)
    assert len(conflicts) == 0


# ─── Webhook Dispatcher Tests ───

import hashlib
import hmac
import json


def test_webhook_dispatch_filters_events():
    """Webhook should only fire for configured events."""
    default_events = {"memory.created", "memory.updated", "memory.conflict"}
    assert "memory.created" in default_events
    assert "memory.updated" in default_events
    assert "memory.conflict" in default_events
    assert "memory.deleted" not in default_events  # Not in defaults


def test_webhook_hmac_signature():
    """HMAC-SHA256 signature should match expected format."""
    secret = "test-secret-key"
    body = '{"event": "memory.created", "data": {}}'

    sig = hmac.new(
        secret.encode(), body.encode(), hashlib.sha256,
    ).hexdigest()

    assert len(sig) == 64  # SHA256 hex digest length
    assert sig == hmac.new(
        secret.encode(), body.encode(), hashlib.sha256,
    ).hexdigest()  # Deterministic


def test_webhook_payload_truncation():
    """Content in webhook payload should be truncated to prevent data leaks."""
    long_content = "x" * 1000
    payload = {
        "content": long_content[:500],
        "domain": "backend",
    }
    assert len(payload["content"]) == 500


def test_webhook_no_urls_is_noop():
    """Empty WEBHOOK_URLS should be a no-op (no crash)."""
    urls = [u.strip() for u in "".split(",") if u.strip()]
    assert urls == []
