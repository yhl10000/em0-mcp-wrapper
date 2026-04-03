# Spec 05: MCP Resources

**Durum: ✅ TAMAMLANDI** | Impl: v0.5.0 | Deploy: v51+

## Problem
em0 sadece tool'lar sunuyordu. MCP'nin `resources` özelliği kullanılmıyordu.
Tool'lar "aksiyon al" için, resource'lar "context sağla" için tasarlanmış.

## Çözüm
Hafızayı hem tool (aktif arama) hem de resource (pasif context) olarak sun.
3 MCP Resource tanımı.

## Resource'lar

### 1. `memory://context/{project_id}` — Auto Context (Spec 01)
Session başı otomatik context. Detaylar Spec 01'de.

### 2. `memory://project/{project_id}/summary` — Proje Özeti
Projedeki tüm hafızaların domain bazlı özeti.

### 3. `memory://project/{project_id}/graph` — Graph Özeti
Knowledge graph'ın entity + relation özeti.

## Implementasyon

### Server Endpoint: Project Summary — `server/main.py:1005-1044`
```
GET /v1/resources/summary/{project_id}
```
Response:
```json
{
  "project": "centauri",
  "total_memories": 100,
  "domains": {"auth": 12, "backend": 8, "frontend": 15},
  "key_decisions": ["Use JWT", "PostgreSQL v15"],
  "last_updated": "2026-04-01T..."
}
```

### Server Endpoint: Graph Summary — `server/main.py:1047-1088`
```
GET /v1/resources/graph-summary/{project_id}
```
Response:
```json
{
  "project": "centauri",
  "entity_types": {"person": 2, "service": 3},
  "entities": {"person": ["Erkut", "Kaan"]},
  "relations": [{"source": "Erkut", "relation": "decided", "target": "PostgreSQL"}],
  "total_relations": 45
}
```

### MCP Resource: Project Summary — `server.py:680-702`
```
memory://project/{project_id}/summary
```
Markdown:
```markdown
# em0 Summary: centauri

Total memories: 100
Last updated: 2026-04-01

## Domains
- backend: 35 memories
- auth: 12 memories

## Key Decisions
- Use JWT for authentication
- PostgreSQL v15 as database
```

### MCP Resource: Graph Overview — `server.py:707-733`
```
memory://project/{project_id}/graph
```
Markdown:
```markdown
# Knowledge Graph: centauri

## Entities
- [person] (2): Erkut, Kaan
- [service] (3): AuthService, APIClient, UserManager

## Relations (45 total)
- Erkut --decided--> PostgreSQL
- AuthService --uses--> JWT
```

### Client — `client.py:169-176`
```python
async def get_project_summary(project_id: str) -> dict
async def get_graph_summary(project_id: str) -> dict
```

### Testler
- `test_client.py::test_get_project_summary` — Summary response doğrulaması
- `test_client.py::test_get_graph_summary` — Graph response doğrulaması

## Maliyet Etkisi
**$0.00/ay** — Mevcut verinin farklı formatla sunulması.

## Bağımlılıklar
- Spec 01 (Auto-Context) — ilk resource tanımı
- Neo4j (graph resource için, opsiyonel)
