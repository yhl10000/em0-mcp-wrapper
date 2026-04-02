# Spec 06: Cross-Project Knowledge Graph

## Problem
Her proje (user_id) kendi silolarında yaşıyor. Ama bazı entity'ler projeler arası ortak:
- "PostgreSQL" hem centauri'de hem em0'da var
- "Erkut" tüm projelerde karar alıyor
- "Azure" her yerde kullanılıyor

Bu entity'ler arasındaki projeler arası bağlantılar kayboluyor.

## Çözüm
Neo4j'de entity node'larına `projects` property'si ekle.
Aynı isimli entity farklı projelerden geliyorsa merge et.
`search_graph` ve `get_relations`'a `cross_project=true` parametresi ekle.

## Akış
```
centauri projesinde: "PostgreSQL kullanıyoruz, version 15"
em0 projesinde:     "pgvector PostgreSQL extension kullanıyoruz"
    ↓
Neo4j'de "PostgreSQL" tek bir node:
  - projects: ["centauri", "em0"]
  - İki projeden gelen farklı relation'lar
    ↓
search_graph("PostgreSQL", cross_project=true):
  PostgreSQL ──USED_BY──→ centauri (version 15)
  PostgreSQL ──USED_BY──→ em0 (pgvector extension)
  PostgreSQL ──HAS_EXTENSION──→ pgvector
```

## Teknik Detay

### Neo4j Entity Merge Logic
```python
def _ensure_cross_project_entity(session, entity_name: str, project_id: str):
    """Entity varsa project listesine ekle, yoksa oluştur."""
    session.run("""
        MERGE (e:Entity {name: $name})
        ON CREATE SET e.projects = [$project]
        ON MATCH SET e.projects = 
            CASE 
                WHEN NOT $project IN e.projects 
                THEN e.projects + $project 
                ELSE e.projects 
            END
    """, name=entity_name, project=project_id)
```

### Search Endpoint Güncelleme
```python
@app.post("/v1/memories/search/")
def search_memory(req: SearchRequest, authorization: str = Header("")):
    # ... mevcut kod ...

    # Cross-project graph search
    if req.cross_project and graph_enabled:
        cross_relations = _search_cross_project(
            req.query, req.user_id, req.limit
        )
        if isinstance(results, dict):
            results["cross_project_relations"] = cross_relations


def _search_cross_project(query: str, current_project: str, limit: int):
    """Diğer projelerdeki ilgili entity bağlantılarını bul."""
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
    )
    with driver.session() as session:
        # Mevcut projede bulunan entity'lerin diğer projelerdeki bağlantıları
        result = session.run("""
            MATCH (e:Entity)
            WHERE $project IN e.projects AND size(e.projects) > 1
            MATCH (e)-[r]-(other:Entity)
            WHERE any(p IN other.projects WHERE p <> $project)
            RETURN e.name AS entity, type(r) AS relation, 
                   other.name AS connected_to,
                   [p IN other.projects WHERE p <> $project] AS other_projects
            LIMIT $limit
        """, project=current_project, limit=limit).data()
    driver.close()
    return result
```

### Yeni MCP Tool
```python
@mcp.tool()
async def search_cross_project(
    query: str,
    user_id: str = "",
    limit: int = 5,
) -> str:
    """Projeler arası bilgi graph'ında ara.

    Bir entity'nin diğer projelerde nasıl kullanıldığını gösterir.
    Örn: "PostgreSQL farklı projelerde nasıl configure edilmiş?"

    Args:
        query: Aranacak entity veya konu
        user_id: Mevcut proje (empty = auto-detect)
        limit: Max sonuç
    """
    uid = user_id or config.DEFAULT_USER_ID
    result = await client.request(
        "POST", "/v1/memories/search/",
        json={
            "query": query,
            "user_id": uid,
            "limit": limit,
            "cross_project": True,
        },
    )
    # Format cross-project relations
    cross = result.get("cross_project_relations", [])
    if not cross:
        return _dump({"result": "No cross-project connections found."})

    lines = [f"Cross-Project Relations for '{query}':\n"]
    for r in cross:
        projects = ", ".join(r.get("other_projects", []))
        lines.append(
            f"  {r['entity']} ──{r['relation']}──→ {r['connected_to']}"
            f"  (also in: {projects})"
        )
    return "\n".join(lines)
```

## Maliyet Etkisi
| Kaynak | Birim Maliyet | Etki |
|--------|--------------|------|
| Neo4j queries | AuraDB Free / self-hosted | $0.00 |
| Embedding (merge check) | $0.02/1M token | İhmal edilebilir |
| **Toplam** | | **$0.00/ay** |

**Ekstra maliyet: Sıfır** (Neo4j AuraDB Free veya mevcut self-hosted instance).

## Uygulama Adımları
1. Neo4j entity merge Cypher query'leri
2. `server/main.py` → `_search_cross_project()` helper
3. `server/main.py` → search endpoint'ine `cross_project` param
4. `src/em0_mcp_wrapper/server.py` → `search_cross_project` tool
5. `src/em0_mcp_wrapper/client.py` → client fonksiyonu
6. Mevcut entity'leri migrate et (projects property ekle)

## Bağımlılıklar
- Neo4j aktif olmalı (graph_enabled=True)
- Spec 01 (Auto-Context) ile birlikte daha güçlü

## Riskler
- Entity isim çakışması: "Auth" farklı projelerde farklı şey olabilir
  → Çözüm: Entity type'ı da merge key'e dahil et
- Performans: Çok fazla cross-project relation → limit zorunlu
