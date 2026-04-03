# Spec 02: Memory Compaction / Summarization

**Durum: ✅ TAMAMLANDI** | Impl: v0.5.0 | Deploy: v51+

## Problem
Zaman içinde aynı domain+type'ta onlarca benzer hafıza birikiyor.
Hem search sonuçlarını kirletiyor hem de token israfına yol açıyor.

## Çözüm
Periyodik compaction: Aynı domain+type grubundaki benzer hafızaları LLM ile tek bir özete birleştir. dry_run ile önce plan gör, sonra uygula.

## Akış
```
compact_memories(dry_run=True) çağrılır
    ↓
Her (project, domain, type) grubu için:
  1. Gruptaki tüm hafızaları çek (immutable olanlar hariç)
  2. Grup < min_cluster_size (default: 3) ise atla
  3. Semantic similarity ile cluster'la (threshold: 0.85)
  4. dry_run=True → plan göster / dry_run=False → merge uygula
    ↓
Her cluster için:
  - gpt-4o-mini ile tek bir özet üret
  - Özeti kaydet (source="compaction", merged_ids=[...])
  - Orijinalleri sil
```

## Implementasyon

### Server Endpoint — `server/main.py:1180-1267`
```
POST /admin/compact
```
Request body:
```json
{
  "user_id": "centauri",
  "dry_run": true,
  "min_cluster_size": 3,
  "similarity_threshold": 0.85
}
```

Helpers:
- `_summarize_cluster(memories)` — `server/main.py:1103-1135`
  - Azure OpenAI gpt-4o-mini, temperature=0.1, max_tokens=500
  - System prompt: "Merge memories, preserve ALL decisions and details"
- `_cluster_by_similarity(m, memories, threshold)` — `server/main.py:1136-1177`
  - Semantic search ile benzerlik kontrolü
  - Fallback: text overlap (word intersection)

Response:
```json
{
  "dry_run": true,
  "plan": [{"group": "backend:decision", "memories_to_merge": 4, "preview": [...]}],
  "total_groups_analyzed": 5,
  "total_merged": 0,
  "memories_saved": 0
}
```

### MCP Tool — `server.py:564-618` (Tool #14: compact_memories)
```python
compact_memories(user_id="", dry_run=True, min_cluster_size=3)
```
- İlk çağrıda `dry_run=True` → plan gösterilir
- Kullanıcı onayladıktan sonra `dry_run=False` → uygula

### Client — `client.py:198-214`
```python
async def compact_memories(user_id, dry_run=True, min_cluster_size=3, similarity_threshold=0.85) -> dict
```

### Testler
- `test_client.py::test_compact_memories_dry_run` — Plan response doğrulaması
- `test_client.py::test_compact_memories_apply` — Merge response + request body doğrulaması

## Maliyet Etkisi
| Kaynak | Compaction Başına | Aylık (haftalık 1x) |
|--------|-------------------|---------------------|
| gpt-4o-mini input | ~10K token = $0.0015 | ~$0.006 |
| gpt-4o-mini output | ~2K token = $0.0012 | ~$0.005 |
| **Toplam** | | **~$0.01/ay** |

## Bağımlılıklar
- Azure OpenAI gpt-4o-mini (summarization)
