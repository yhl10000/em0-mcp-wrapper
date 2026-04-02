# Spec 05: MCP Resources

## Problem
em0 şu an sadece tool'lar sunuyor. MCP'nin `resources` özelliği kullanılmıyor.
Tool'lar "aksiyon al" için, resource'lar "context sağla" için tasarlanmış.
Hafıza doğası gereği context — resource olarak sunulması daha doğru.

## Çözüm
Hafızayı hem tool (aktif arama) hem de resource (pasif context) olarak sun.
Client'lar ihtiyaç duydukça resource'u okuyabilir.

## Resource'lar

### 1. `memory://context/{project_id}` — Auto Context
Spec 01 ile birlikte gelir. Session başı otomatik context.

### 2. `memory://project/{project_id}/summary` — Proje Özeti
Projedeki tüm hafızaların domain bazlı özeti.

```
# em0 Project Summary: centauri-ios

## Stats
- Total memories: 47
- Domains: auth (12), backend (8), frontend (15), infra (7), ui (5)
- Last updated: 2026-04-01

## Key Decisions
- Auth: JWT + refresh token, Keychain storage
- Backend: FastAPI + PostgreSQL
- Frontend: SwiftUI + Combine
```

### 3. `memory://project/{project_id}/graph` — Graph Özeti
Knowledge graph'ın metin özeti.

```
# Knowledge Graph: centauri-ios

## Entities (23)
- [Service] AuthService, APIClient, UserManager
- [Database] PostgreSQL, Redis
- [Person] Erkut, Kaan

## Key Relations
- AuthService ──USES──→ JWT
- APIClient ──CONNECTS_TO──→ PostgreSQL
- Erkut ──DECIDED──→ SwiftUI
```

### 4. `memory://domains` — Tüm Domain'ler
Hangi domain'lerde hafıza var, kaçar tane.

## Teknik Detay

### Server Endpoint'leri (server/main.py)
```python
@app.get("/v1/resources/summary/{project_id}")
def project_summary(project_id: str, authorization: str = Header("")):
    _check_auth(authorization)
    m = _get_memory()
    all_mems = m.get_all(user_id=project_id).get("results", [])

    # Domain bazlı gruplama
    domains = {}
    for mem in all_mems:
        domain = mem.get("metadata", {}).get("domain", "uncategorized")
        domains.setdefault(domain, []).append(mem)

    # Son kararları çek (type=decision)
    decisions = [
        mem for mem in all_mems
        if mem.get("metadata", {}).get("type") == "decision"
    ]

    return {
        "project": project_id,
        "total_memories": len(all_mems),
        "domains": {k: len(v) for k, v in domains.items()},
        "key_decisions": [d.get("memory", "")[:200] for d in decisions[:10]],
        "last_updated": max(
            (m.get("updated_at", m.get("created_at", "")) for m in all_mems),
            default="unknown",
        ),
    }


@app.get("/v1/resources/graph-summary/{project_id}")
def graph_summary(project_id: str, authorization: str = Header("")):
    _check_auth(authorization)
    if not NEO4J_URI:
        return {"error": "Graph not enabled"}
    m = _get_memory()
    filters = {"user_id": project_id} if project_id else {}
    graph_data = m.graph.get_all(filters=filters)

    entities = {}
    relations = []
    if isinstance(graph_data, list):
        for item in graph_data:
            src = item.get("source", "")
            src_type = item.get("source_type", "entity")
            tgt = item.get("target", "")
            tgt_type = item.get("target_type", "entity")
            if src:
                entities.setdefault(src_type, []).append(src)
            if tgt:
                entities.setdefault(tgt_type, []).append(tgt)
            relations.append({
                "source": src,
                "relation": item.get("relation", ""),
                "target": tgt,
            })

    # Deduplicate entity lists
    entities = {k: list(set(v)) for k, v in entities.items()}

    return {
        "project": project_id,
        "entity_types": {k: len(v) for k, v in entities.items()},
        "entities": entities,
        "relations": relations[:50],  # Cap at 50
        "total_relations": len(relations),
    }
```

### MCP Resource'ları (server.py)
```python
@mcp.resource("memory://project/{project_id}/summary")
async def project_summary(project_id: str) -> str:
    """Projenin hafıza özeti — domain dağılımı ve anahtar kararlar."""
    result = await client.request("GET", f"/v1/resources/summary/{project_id}")
    lines = [f"# em0 Summary: {result.get('project', '?')}\n"]
    lines.append(f"Total memories: {result.get('total_memories', 0)}")
    lines.append(f"Last updated: {result.get('last_updated', '?')}\n")

    domains = result.get("domains", {})
    if domains:
        lines.append("## Domains")
        for d, count in sorted(domains.items(), key=lambda x: -x[1]):
            lines.append(f"- {d}: {count} memories")

    decisions = result.get("key_decisions", [])
    if decisions:
        lines.append("\n## Key Decisions")
        for d in decisions:
            lines.append(f"- {d}")

    return "\n".join(lines)


@mcp.resource("memory://project/{project_id}/graph")
async def graph_overview(project_id: str) -> str:
    """Knowledge graph özeti — entity'ler ve ilişkiler."""
    result = await client.request("GET", f"/v1/resources/graph-summary/{project_id}")
    lines = [f"# Knowledge Graph: {result.get('project', '?')}\n"]

    entities = result.get("entities", {})
    for etype, names in entities.items():
        lines.append(f"## [{etype}] ({len(names)})")
        lines.append(", ".join(names[:20]))

    relations = result.get("relations", [])
    if relations:
        lines.append(f"\n## Relations ({result.get('total_relations', 0)} total)")
        for r in relations[:20]:
            lines.append(f"  {r['source']} ──{r['relation']}──→ {r['target']}")

    return "\n".join(lines)
```

## Maliyet Etkisi
| Kaynak | Birim Maliyet | Etki |
|--------|--------------|------|
| API calls | Mevcut endpoint'ler | $0.00 |
| **Toplam** | | **$0.00/ay** |

**Ekstra maliyet: Sıfır.** Mevcut verinin farklı bir formatla sunulması.

## Uygulama Adımları
1. `server/main.py` → `/v1/resources/summary/{project_id}` endpoint
2. `server/main.py` → `/v1/resources/graph-summary/{project_id}` endpoint
3. `src/em0_mcp_wrapper/server.py` → 3-4 `@mcp.resource()` tanımı
4. `src/em0_mcp_wrapper/client.py` → helper fonksiyonları
5. Test

## Bağımlılıklar
- FastMCP 3.x resource desteği (zaten kullanılıyor)
