# Spec 06: Cross-Project Knowledge Graph

**Durum: ✅ TAMAMLANDI** | Impl: v0.5.0 | Deploy: v51+

## Problem
Her proje (user_id) kendi silosunda yaşıyor. Ama bazı entity'ler projeler arası ortak:
- "PostgreSQL" hem centauri'de hem em0'da var
- "Erkut" tüm projelerde karar alıyor
- "Azure" her yerde kullanılıyor

## Çözüm
Mevcut projeden entity'leri al, diğer projelerin graph'larında aynı entity'leri ara,
cross-project relation'ları döndür. Raw Neo4j Cypher kullanmak yerine mem0'un
`graph.get_all()` API'sini kullanarak güvenli çalışır.

## Akış
```
search_cross_project("PostgreSQL", user_id="centauri")
    ↓
1. centauri'nin memory'lerinde "PostgreSQL" ara → context
2. centauri'nin graph'ından tüm entity'leri çek
3. Diğer projeleri bul (m.get_all ile tüm user_id'ler)
4. Her diğer projenin graph'ında aynı entity'leri ara
5. Eşleşen entity'lerin cross-project relation'larını döndür
    ↓
Sonuç:
  PostgreSQL --uses--> pgvector  (project: em0-mcp-wrapper)
  PostgreSQL --version--> v15    (project: happybrain)
```

## Implementasyon

### Server Endpoint — `server/main.py:826-922`
```
POST /v1/search/cross-project
```
Request:
```json
{
  "query": "PostgreSQL",
  "user_id": "centauri",
  "limit": 10
}
```

Logic:
1. `m.search(query, user_id=current_project, limit=5)` → context
2. `m.graph.get_all(filters={"user_id": current_project})` → entity'leri topla
3. `m.get_all()` → tüm user_id'leri keşfet (diğer projeler)
4. Her diğer proje için `m.graph.get_all(filters={"user_id": other})` → entity eşleştir
5. Entity name lowercase match → cross-project relation

Response:
```json
{
  "current_project": "centauri",
  "entities_in_project": 108,
  "other_projects_checked": 4,
  "cross_relations": [
    {
      "entity": "postgresql",
      "relation": "uses",
      "connected_to": "pgvector",
      "other_project": "em0-mcp-wrapper",
      "direction": "outgoing"
    }
  ],
  "search_context": ["PostgreSQL v15 is the database"]
}
```

### MCP Tool — `server.py:508-559` (Tool #13: search_cross_project)
```python
search_cross_project(query, user_id="", limit=10)
```
Output:
```
Cross-Project Search: 'PostgreSQL'

Current project: centauri
Entities in project graph: 108
Other projects checked: 4

Cross-Project Relations (2):
  postgresql -->[uses]--> pgvector  (project: em0-mcp-wrapper)
  erkut -->[decided]--> swiftui  (project: centauri-ios)
```

### Client — `client.py:187-195`
```python
async def search_cross_project(query, user_id, limit=10) -> dict
```

### Testler
- `test_client.py::test_search_cross_project` — Cross-project relations doğrulaması
- `test_client.py::test_search_cross_project_no_results` — Boş sonuç

## Maliyet Etkisi
**$0.00/ay** — Neo4j AuraDB Free veya mevcut self-hosted instance.

## Bağımlılıklar
- Neo4j (zorunlu — graph_enabled=True olmalı)
