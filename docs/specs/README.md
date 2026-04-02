# em0 v0.5.0 — Feature Specs

## Overview
7 yeni feature ile em0'yu Obsidian+Claude Code yaklaşımının çok ötesine taşıyacak geliştirmeler.

## Specs

| # | Feature | Spec | Maliyet | Zorluk | Bağımlılık |
|---|---------|------|---------|--------|------------|
| 01 | [Auto-Context Resource](01-auto-context-resource.md) | MCP Resource ile session başı otomatik context | ~$0.01/ay | Düşük | - |
| 02 | [Memory Compaction](02-memory-compaction.md) | Benzer hafızaları LLM ile birleştir | ~$0.01/ay | Orta | - |
| 03 | [Conflict Detection](03-conflict-detection.md) | add_memory'de çelişki uyarısı | ~$0.001/ay | Orta | - |
| 04 | [Freshness Scoring](04-freshness-scoring.md) | Temporal decay + popularity scoring | $0.00 | Düşük | - |
| 05 | [MCP Resources](05-mcp-resources.md) | Hafızayı resource olarak sun | $0.00 | Düşük | Spec 01 |
| 06 | [Cross-Project Graph](06-cross-project-graph.md) | Projeler arası entity bağlantıları | $0.00 | Yüksek | Neo4j |
| 07 | [Webhook Events](07-webhook-events.md) | Event-driven bildirimler | $0.00 | Orta | - |

## Toplam Aylık Ekstra Maliyet

| Kaynak | Maliyet |
|--------|---------|
| Azure OpenAI gpt-4o-mini (compaction) | ~$0.01 |
| Azure OpenAI text-embedding-3-small (search) | ~$0.01 |
| Neo4j AuraDB Free | $0.00 |
| Slack Webhooks | $0.00 |
| **TOPLAM** | **~$0.02/ay** |

## Uygulama Sırası (Önerilen)
1. **Spec 04** — Freshness Scoring (en kolay, sıfır maliyet, hemen fark yaratır)
2. **Spec 01 + 05** — Auto-Context + MCP Resources (birlikte anlamlı)
3. **Spec 03** — Conflict Detection (hafıza kalitesini artırır)
4. **Spec 02** — Memory Compaction (hafıza büyüdükçe gerekli olacak)
5. **Spec 07** — Webhook Events (takım kullanımı için)
6. **Spec 06** — Cross-Project Graph (en karmaşık, en son)
