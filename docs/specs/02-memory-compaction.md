# Spec 02: Memory Compaction / Summarization

## Problem
Zaman içinde aynı domain+type'ta onlarca benzer hafıza birikiyor.
Örneğin "auth" domain'inde 15 farklı "decision" tipi hafıza olabilir.
Bu hem search sonuçlarını kirletiyor hem de token israfına yol açıyor.

## Çözüm
Periyodik compaction: Aynı domain+type grubundaki benzer hafızaları LLM ile tek bir özete birleştir. Orijinalleri history'de tut.

## Akış
```
Compaction tetiklenir (manuel veya cron)
    ↓
Her (project, domain, type) grubu için:
  1. Gruptaki tüm hafızaları çek
  2. Grup < 3 hafıza ise atla
  3. Semantic similarity ile cluster'la (>0.85 benzerlik)
  4. Her cluster'ı LLM'e gönder → tek bir özet hafıza üret
  5. Orijinal hafızaları "archived" olarak işaretle
  6. Yeni özet hafızayı kaydet (source: "compaction")
    ↓
Hafıza sayısı azalır, kalite artar
```

## Teknik Detay

### Compaction Endpoint (server/main.py)
```python
@app.post("/admin/compact")
def compact_memories(
    user_id: str = Query(""),
    dry_run: bool = Query(True),  # Default: sadece göster, uygulamaz
    min_cluster_size: int = Query(3),
    similarity_threshold: float = Query(0.85),
    authorization: str = Header(""),
):
    """Benzer hafızaları birleştir."""
    _check_auth(authorization)
    m = _get_memory()

    all_mems = m.get_all(user_id=user_id).get("results", [])

    # Domain+type gruplarına ayır
    groups = {}
    for mem in all_mems:
        meta = mem.get("metadata", {})
        key = f"{meta.get('domain', 'unknown')}:{meta.get('type', 'unknown')}"
        groups.setdefault(key, []).append(mem)

    compaction_plan = []
    for key, mems in groups.items():
        if len(mems) < min_cluster_size:
            continue

        # Semantic clustering: search each memory against others
        clusters = _cluster_memories(m, mems, similarity_threshold)

        for cluster in clusters:
            if len(cluster) < min_cluster_size:
                continue
            if dry_run:
                compaction_plan.append({
                    "group": key,
                    "memories_to_merge": len(cluster),
                    "preview": [c.get("memory", "")[:100] for c in cluster],
                })
            else:
                # LLM summarize
                summary = _summarize_cluster(cluster)
                # Add new compacted memory
                m.add(summary, user_id=user_id, metadata={
                    "domain": key.split(":")[0],
                    "type": key.split(":")[1],
                    "source": "compaction",
                    "merged_count": len(cluster),
                    "merged_ids": [c["id"] for c in cluster],
                })
                # Delete originals
                for c in cluster:
                    m.delete(c["id"])
                compaction_plan.append({
                    "group": key,
                    "merged": len(cluster),
                    "summary": summary[:200],
                })

    return {
        "dry_run": dry_run,
        "plan": compaction_plan,
        "total_groups_analyzed": len(groups),
    }


def _summarize_cluster(memories: list[dict]) -> str:
    """LLM ile hafıza cluster'ını tek bir özete birleştir."""
    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=AZURE_OPENAI_KEY,
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_deployment="gpt-4o-mini",
        api_version="2024-02-01",
    )

    contents = "\n".join(
        f"- {m.get('memory', '')}" for m in memories
    )

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "system",
            "content": (
                "You are a knowledge compactor. Merge the following related memories "
                "into a single, concise memory that preserves ALL important information. "
                "Do not lose any decisions, trade-offs, or technical details. "
                "Output only the merged memory text, nothing else."
            ),
        }, {
            "role": "user",
            "content": f"Memories to merge:\n{contents}",
        }],
        max_tokens=500,
        temperature=0.1,
    )
    return response.choices[0].message.content.strip()
```

### MCP Tool (server.py)
```python
@mcp.tool()
async def compact_memories(
    user_id: str = "",
    dry_run: bool = True,
) -> str:
    """Benzer hafızaları birleştirerek hafıza kalitesini artır.

    Önce dry_run=True ile çalıştırarak planı gör,
    sonra dry_run=False ile uygula.

    Args:
        user_id: Proje scope
        dry_run: True=sadece plan göster, False=uygula
    """
    uid = user_id or config.DEFAULT_USER_ID
    result = await client.request(
        "POST", "/admin/compact",
        params={"user_id": uid, "dry_run": dry_run},
    )
    return _dump(result)
```

## Maliyet Etkisi
| Kaynak | Birim Maliyet | Compaction Başına | Aylık (haftalık 1 kez) |
|--------|--------------|-------------------|----------------------|
| gpt-4o-mini input | $0.15/1M token | ~10K token = $0.0015 | $0.006 |
| gpt-4o-mini output | $0.60/1M token | ~2K token = $0.0012 | $0.005 |
| Embedding (re-index) | $0.02/1M token | ~5K token = $0.0001 | $0.0004 |
| **Toplam** | | | **~$0.01/ay** |

**Ekstra maliyet: ~$0.01/ay.** Haftalık 1 compaction ile.

## Uygulama Adımları
1. `server/main.py` → `/admin/compact` endpoint
2. `_summarize_cluster()` helper fonksiyonu
3. `src/em0_mcp_wrapper/server.py` → `compact_memories` tool
4. `src/em0_mcp_wrapper/client.py` → `compact()` fonksiyonu
5. dry_run testi + gerçek compaction testi

## Bağımlılıklar
- Azure OpenAI gpt-4o-mini (zaten var)
