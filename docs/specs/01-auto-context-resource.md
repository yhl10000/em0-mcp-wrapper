# Spec 01: Auto-Context MCP Resource

**Durum: ✅ TAMAMLANDI** | Impl: v0.5.0 | Deploy: v51+

## Problem
em0'dan bilgi çekmek için her session'da manuel olarak `search_memory` çağırmak gerekiyor.
Obsidian yaklaşımı tüm dosyaları yüklüyor (token israfı), biz sadece ilgili olanları çekiyoruz.

## Çözüm
MCP Resource olarak `memory://context/{project_id}` endpoint'i. Session başında Claude Code (veya herhangi bir MCP client) bu resource'u okur, server tarafında akıllı sorgu oluşturur, sadece ilgili 5-10 hafızayı döner.

## Akış
```
Client session başlar
    ↓
MCP Resource okur: memory://context/{project_id}
    ↓
Server tarafında:
  1. Semantic search → son 5 karar/mimari hafıza (freshness scoring uygulanır)
  2. get_all → immutable hafızaları filtrele (bug lessons, her zaman dahil)
  3. Neo4j graph → ilk 15 entity relation (graph etkinse)
  4. Sonuçları markdown formatında döndür
    ↓
Client context'ine ~500-1000 token enjekte edilir
```

## Implementasyon

### Server Endpoint — `server/main.py:930-1002`
```
GET /v1/context/{project_id}
```
- Semantic search: `m.search(query="{project_id} decisions architecture conventions", user_id=project_id, limit=5)`
- Freshness scoring: `_apply_freshness()` uygulanır (Spec 04)
- Immutable filter: `metadata.immutable == True` olanlar her zaman dahil
- Graph relations: `m.graph.get_all(filters={"user_id": project_id})` ilk 15 relation
- Graceful degradation: Neo4j kapalıysa graph bölümü atlanır

Response:
```json
{
  "project": "centauri",
  "recent_decisions": [...],
  "immutable_lessons": [...],
  "graph_relations": [...],
  "stats": {"total_memories": 100, "immutable_count": 3, "graph_relations_count": 15}
}
```

### MCP Resource — `server.py:628-675`
```
memory://context/{project_id}
```
Markdown formatında döner:
```markdown
# em0 Context: centauri

*100 memories, 3 immutable, 15 graph relations*

## Recent Decisions
- [backend/decision] PostgreSQL v15 kullanıyoruz (fresh=0.96)

## Immutable Lessons (always apply)
- [infra] Embedding'ler 1024d olmalı, monkey-patch gerekli

## Key Relations (15)
- PostgreSQL --USED_BY--> centauri
```

### Client — `client.py:164-166`
```python
async def get_context(project_id: str) -> dict
```

### Testler
- `test_client.py::test_get_context` — Response formatı doğrulaması

## Maliyet Etkisi
| Kaynak | Session Başına | Aylık (30 session/gün) |
|--------|----------------|----------------------|
| Embedding search | ~500 token = $0.00001 | ~$0.01 |

## Bağımlılıklar
- Spec 04 (Freshness Scoring) — karar sonuçlarına freshness uygulanıyor
- Neo4j (opsiyonel) — graph relations için
