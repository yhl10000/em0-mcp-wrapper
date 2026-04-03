# em0 v0.5.0 — Feature Specs

**Durum: 7/7 Spec Tamamlandı** | Production: Azure Container Apps | 45 test passing

## Motivasyon
Obsidian+Claude Code yaklaşımıyla kıyaslama yapıldı. Obsidian düz markdown dosyaları yüklüyor (500+ nota'da token israfı). em0 semantic search + knowledge graph ile sadece ilgili hafızayı çekiyor. Bu spec'ler em0'yu daha da ileriye taşıdı.

## Specs

| # | Feature | Spec | Maliyet | Durum |
|---|---------|------|---------|-------|
| 01 | [Auto-Context Resource](01-auto-context-resource.md) | MCP Resource ile session başı otomatik context | ~$0.01/ay | ✅ Done |
| 02 | [Memory Compaction](02-memory-compaction.md) | Benzer hafızaları LLM ile birleştir | ~$0.01/ay | ✅ Done |
| 03 | [Conflict Detection](03-conflict-detection.md) | add_memory'de çelişki uyarısı | ~$0.001/ay | ✅ Done |
| 04 | [Freshness Scoring](04-freshness-scoring.md) | Temporal decay + popularity scoring | $0.00 | ✅ Done |
| 05 | [MCP Resources](05-mcp-resources.md) | Hafızayı resource olarak sun (3 resource) | $0.00 | ✅ Done |
| 06 | [Cross-Project Graph](06-cross-project-graph.md) | Projeler arası entity bağlantıları | $0.00 | ✅ Done |
| 07 | [Webhook Events](07-webhook-events.md) | Event-driven bildirimler (HMAC signed) | $0.00 | ✅ Done |

## Ek: search_all_projects
Spec sonrası eklendi — mem0 v1.0 `get_all()` breaking change'i nedeniyle.
User_id bilmeden tüm projelerde arama yapar. Neo4j'den dinamik user_id keşfi.
- Server: `POST /v1/memories/search-all/` (`server/main.py:525-590`)
- MCP Tool: `search_all_projects(query, limit)` (`server.py:193-244`)
- Client: `search_all_projects(query, limit)` (`client.py:179-184`)

## Mevcut Envanter

### MCP Tools (15)
| # | Tool | Spec | Endpoint |
|---|------|------|----------|
| 1 | add_memory | - | POST `/v1/memories/` |
| 2 | search_memory | - | POST `/v1/memories/search/` |
| 2b | search_all_projects | Extra | POST `/v1/memories/search-all/` |
| 3 | list_memories | - | GET `/v1/memories/` |
| 4 | get_memory | - | GET `/v1/memories/{id}/` |
| 5 | update_memory | - | PUT `/v1/memories/{id}/` |
| 6 | delete_memory | - | DELETE `/v1/memories/{id}/` |
| 7 | memory_history | - | GET `/v1/memories/{id}/history/` |
| 8 | memory_stats | - | GET `/stats` |
| 9 | get_entities | - | GET `/v1/entities/` |
| 10 | get_relations | - | GET `/v1/relations/` |
| 11 | search_graph | - | POST `/v1/memories/search/` (v2) |
| 12 | delete_entity | - | DELETE `/v1/entities/{name}/` |
| 13 | search_cross_project | 06 | POST `/v1/search/cross-project` |
| 14 | compact_memories | 02 | POST `/admin/compact` |

### MCP Resources (3)
| # | Resource URI | Spec | Endpoint |
|---|-------------|------|----------|
| 1 | `memory://context/{project_id}` | 01+05 | GET `/v1/context/{project_id}` |
| 2 | `memory://project/{project_id}/summary` | 05 | GET `/v1/resources/summary/{project_id}` |
| 3 | `memory://project/{project_id}/graph` | 05 | GET `/v1/resources/graph-summary/{project_id}` |

### Production Stats (2026-04-03)
```
Projeler: 5 (centauri: 100, pal-cms: 75, happybrain: 70, onboarding-survey-engine: 29, em0-mcp-wrapper: 4)
Toplam hafıza: 278
Graph: 1082 node, 1039 edge
```

## Toplam Aylık Ekstra Maliyet
| Kaynak | Maliyet |
|--------|---------|
| Azure OpenAI gpt-4o-mini (compaction) | ~$0.01 |
| Azure OpenAI text-embedding-3-small (search) | ~$0.01 |
| Neo4j (mevcut instance) | $0.00 |
| Slack Webhooks | $0.00 |
| **TOPLAM** | **~$0.02/ay** |

## Notlar
- **mem0 v1.0 Breaking Change (2026-03-28):** `get_all()` artık `user_id` zorunlu tutuyor. Stats endpoint ve search_all_projects bu değişikliğe adapte edildi. Detay: mem0ai kütüphanesi multi-tenant güvenlik için bu zorunluluğu getirdi.
- **Dependency:** `mem0ai[graph]>=0.1.0` — version pin yok, latest takip ediliyor.
