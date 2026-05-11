"""Compatibility helpers around mem0 library API changes."""

from typing import Any


def search_memory(
    memory: Any,
    query: str,
    user_id: str = "",
    limit: int = 5,
    filters: dict | None = None,
    **kwargs: Any,
) -> Any:
    """Search memories across mem0 API versions.

    mem0 2.x rejects top-level user_id in search() and expects it inside
    filters. Older versions accepted top-level user_id. Prefer the 2.x shape,
    then fall back to the legacy call only if necessary.
    """
    merged_filters = dict(filters or {})
    if user_id:
        merged_filters.setdefault("user_id", user_id)

    search_kwargs: dict[str, Any] = {
        "query": query,
        "limit": limit,
        **kwargs,
    }
    if merged_filters:
        search_kwargs["filters"] = merged_filters

    try:
        return memory.search(**search_kwargs)
    except (TypeError, ValueError) as first_error:
        legacy_kwargs: dict[str, Any] = {
            "query": query,
            "user_id": user_id,
            "limit": limit,
            **kwargs,
        }
        if filters:
            legacy_kwargs["filters"] = filters
        try:
            return memory.search(**legacy_kwargs)
        except Exception:
            raise first_error
