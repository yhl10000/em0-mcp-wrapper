"""Scoring helpers for the mem0 REST server."""

from datetime import datetime, timezone


def apply_freshness(results: list[dict]) -> list[dict]:
    """Apply temporal decay and popularity scoring to search results."""
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
