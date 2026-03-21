#!/usr/bin/env python3
"""Migrate existing memories to graph — re-ingests so Neo4j extracts entities/relations.

Usage:
    python infra/migrate-to-graph.py

Reads all memories via list_memories, then re-sends each one via add_memory.
mem0 will deduplicate the text (no pgvector duplicates) but WILL extract
entities/relations into Neo4j this time.

Safe to run multiple times — idempotent.
"""

import asyncio
import os
import sys
import time

# Add src to path for local development
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from em0_mcp_wrapper import client, config  # noqa: E402

config.MEM0_API_URL = os.environ.get("MEM0_API_URL", config.MEM0_API_URL)
config.MEM0_API_KEY = os.environ.get("MEM0_API_KEY", config.MEM0_API_KEY)


async def migrate(user_id: str) -> None:
    print(f"=== Migrating memories for '{user_id}' to graph ===\n")

    # Step 1: Get all existing memories
    print("[1/2] Fetching existing memories...")
    result = await client.list_memories(user_id=user_id)

    if "error" in result:
        print(f"  Error: {result['error']}")
        sys.exit(1)

    memories = result.get("results", [])
    if not memories:
        print("  No memories found. Nothing to migrate.")
        return

    print(f"  Found {len(memories)} memories.\n")

    # Step 2: Re-ingest each memory
    print("[2/2] Re-ingesting for graph extraction...")
    success = 0
    skipped = 0
    failed = 0

    for i, mem in enumerate(memories, 1):
        text = mem.get("memory", "")
        meta = mem.get("metadata", {})
        mem_id = mem.get("id", "?")

        if not text.strip():
            print(f"  [{i}/{len(memories)}] SKIP (empty) id={mem_id}")
            skipped += 1
            continue

        # Retry with backoff for rate limiting
        for attempt in range(3):
            try:
                res = await client.add_memory(
                    content=text,
                    user_id=user_id,
                    metadata=meta,
                )
                if "error" in res:
                    err = res["error"]
                    if "500" in str(err) and attempt < 2:
                        wait = 10 * (attempt + 1)
                        print(f"  [{i}/{len(memories)}] RETRY ({attempt+1}/3) waiting {wait}s...")
                        time.sleep(wait)
                        continue
                    print(f"  [{i}/{len(memories)}] FAIL id={mem_id}: {err}")
                    failed += 1
                else:
                    events = res.get("results", [])
                    event = events[0].get("event", "?") if events else "DEDUP"
                    print(f"  [{i}/{len(memories)}] OK ({event}) {text[:60]}...")
                    success += 1
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                print(f"  [{i}/{len(memories)}] ERROR id={mem_id}: {e}")
                failed += 1
                break

        # Delay between each request to avoid Azure OpenAI rate limiting
        time.sleep(5)

    print(f"\n=== Migration complete ===")
    print(f"  Success: {success}")
    print(f"  Skipped: {skipped}")
    print(f"  Failed:  {failed}")
    print(f"  Total:   {len(memories)}")


def main():
    user_id = sys.argv[1] if len(sys.argv) > 1 else config.DEFAULT_USER_ID
    config.validate()
    asyncio.run(migrate(user_id))


if __name__ == "__main__":
    main()
