# Spec 01: Auto-Context MCP Resource

## Problem
em0'dan bilgi çekmek için her session'da manuel olarak `search_memory` çağırmak gerekiyor.
Obsidian yaklaşımı tüm dosyaları yüklüyor (token israfı), biz sadece ilgili olanları çekeceğiz.

## Çözüm
MCP Resource olarak `memory://context/auto` endpoint'i. Session başında Claude Code (veya herhangi bir MCP client) bu resource'u okur, server tarafında akıllı sorgu oluşturur, sadece ilgili 5-10 hafızayı döner.

## Akış
```
Client session başlar
    ↓
MCP Resource okur: memory://context/{project_id}
    ↓
Server tarafında:
  1. project_id'den son eklenen/erişilen memoryleri çek
  2. Proje bazlı en yüksek scorelu kararları çek
  3. Immutable (bug lessons) hafızaları her zaman dahil et
  4. Sonuçları markdown formatında döndür
    ↓
Client context'ine ~500-1000 token enjekte edilir
```

## Teknik Detay

### Server tarafı (server/main.py)
```python
@app.get("/v1/context/{project_id}")
def auto_context(project_id: str, authorization: str = Header("")):
    """Proje için otomatik context oluştur."""
    _check_auth(authorization)
    m = _get_memory()

    # 1. Son 5 karar/mimari hafıza
    recent = m.search(
        query=f"{project_id} decisions architecture",
        user_id=project_id,
        limit=5,
    )

    # 2. Immutable hafızalar (bug lessons) — her zaman dahil
    immutables = m.get_all(user_id=project_id)
    immutable_items = [
        mem for mem in immutables.get("results", [])
        if mem.get("metadata", {}).get("immutable") is True
    ]

    # 3. Birleştir ve formatla
    return {
        "recent_decisions": recent,
        "immutable_lessons": immutable_items,
        "project": project_id,
    }
```

### MCP Wrapper tarafı (server.py)
```python
@mcp.resource("memory://context/{project_id}")
async def auto_context(project_id: str) -> str:
    """Session başında otomatik yüklenen proje context'i."""
    result = await client.request("GET", f"/v1/context/{project_id}")
    # Markdown formatında döndür
    lines = [f"# em0 Context: {project_id}\n"]

    decisions = result.get("recent_decisions", {}).get("results", [])
    if decisions:
        lines.append("## Son Kararlar")
        for d in decisions:
            lines.append(f"- {d.get('memory', '')}")

    immutables = result.get("immutable_lessons", [])
    if immutables:
        lines.append("\n## Dikkat (Immutable)")
        for im in immutables:
            lines.append(f"- ⚠ {im.get('memory', '')}")

    return "\n".join(lines)
```

## Maliyet Etkisi
| Kaynak | Birim Maliyet | Session Başına | Aylık (30 session/gün) |
|--------|--------------|----------------|----------------------|
| Embedding search | $0.02/1M token | ~500 token = $0.00001 | $0.009 |
| **Toplam** | | | **~$0.01/ay** |

**Ekstra maliyet: Yok denecek kadar az.** Zaten var olan search endpoint'ini çağırıyor.

## Uygulama Adımları
1. `server/main.py` → `/v1/context/{project_id}` endpoint ekle
2. `src/em0_mcp_wrapper/client.py` → `get_context()` fonksiyonu ekle
3. `src/em0_mcp_wrapper/server.py` → `@mcp.resource()` olarak expose et
4. Test yaz

## Bağımlılıklar
- Yok (mevcut altyapı yeterli)
