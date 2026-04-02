# Spec 03: Conflict Detection

## Problem
Birbirine çelişen kararlar birikebilir. Örneğin:
- Hafıza A: "Auth için JWT kullanıyoruz"
- Hafıza B: "Session-based auth'a geçtik"

İkisi de sistemde kalır, hangi kararın güncel olduğu belirsizdir.

## Çözüm
`add_memory` sırasında otomatik semantic search. Yüksek benzerlikli ama farklı içerikli hafıza varsa uyarı dön, güncelleme öner.

## Akış
```
add_memory("MongoDB'ye geçiyoruz") çağrılır
    ↓
Otomatik semantic search → "PostgreSQL kullanıyoruz" bulunur (score: 0.87)
    ↓
İçerik farklı + score yüksek = potansiyel çelişki
    ↓
Response'a uyarı eklenir:
  "⚠ Conflict: Existing memory says 'PostgreSQL kullanıyoruz' (id: abc123).
   Consider updating that memory instead, or mark it as superseded."
    ↓
Hafıza yine kaydedilir (bloklama yok), sadece uyarı
```

## Teknik Detay

### Server tarafı (server/main.py)
```python
def _check_conflicts(
    m, content: str, user_id: str, threshold: float = 0.80
) -> list[dict]:
    """Yeni içerikle çelişebilecek mevcut hafızaları bul."""
    try:
        results = m.search(query=content, user_id=user_id, limit=3)
        items = results.get("results", []) if isinstance(results, dict) else results

        conflicts = []
        for item in items:
            score = item.get("score", 0)
            existing = item.get("memory", "")

            if score < threshold:
                continue

            # Aynı içerikse çelişki değil (dedup)
            if _normalize(content) == _normalize(existing):
                continue

            # Yüksek benzerlik + farklı içerik = potansiyel çelişki
            conflicts.append({
                "existing_memory": existing,
                "existing_id": item.get("id", "?"),
                "similarity_score": round(score, 3),
                "suggestion": "Consider updating this memory or marking as superseded.",
            })

        return conflicts
    except Exception as e:
        logger.warning("Conflict check failed (non-blocking): %s", e)
        return []


def _normalize(text: str) -> str:
    """Basit normalizasyon — karşılaştırma için."""
    return " ".join(text.lower().split())


# add_memory endpoint'inde:
@app.post("/v1/memories/")
def add_memory(req: AddMemoryRequest, authorization: str = Header("")):
    _check_auth(authorization)
    m = _get_memory()

    content = req.messages[0]["content"] if req.messages else ""

    # Conflict detection (non-blocking)
    conflicts = _check_conflicts(m, content, req.user_id)

    # Normal add işlemi (mevcut kod)
    result = m.add(content, user_id=req.user_id, metadata=req.metadata)

    # Çelişki varsa response'a ekle
    if conflicts:
        if isinstance(result, dict):
            result["conflicts"] = conflicts
            result["conflict_warning"] = (
                f"⚠ {len(conflicts)} potential conflict(s) found. "
                "Review existing memories before proceeding."
            )
        
    return result
```

### MCP Wrapper tarafı (server.py)
```python
# add_memory fonksiyonunda mevcut response formatting'e ek:
if "conflicts" in result:
    lines = ["\n⚠ POTENTIAL CONFLICTS:"]
    for c in result["conflicts"]:
        lines.append(
            f"  - Existing: \"{c['existing_memory'][:100]}...\""
            f"\n    id={c['existing_id']} score={c['similarity_score']}"
            f"\n    → {c['suggestion']}"
        )
    # Response'un sonuna ekle
```

## Maliyet Etkisi
| Kaynak | Birim Maliyet | add_memory Başına | Aylık (günde 10 add) |
|--------|--------------|-------------------|---------------------|
| Embedding search | $0.02/1M token | ~200 token = $0.000004 | $0.001 |
| **Toplam** | | | **~$0.001/ay** |

**Ekstra maliyet: Sıfıra yakın.** Her add_memory'de 1 search çağrısı.

## Uygulama Adımları
1. `server/main.py` → `_check_conflicts()` helper
2. `server/main.py` → `add_memory` endpoint'ine conflict check ekle
3. `src/em0_mcp_wrapper/server.py` → response formatting güncelle
4. Test: çelişen hafıza ekleme senaryosu

## Bağımlılıklar
- Yok (mevcut search altyapısını kullanıyor)

## Edge Cases
- Immutable hafızalarla çelişki → ekstra uyarı: "This conflicts with an IMMUTABLE memory"
- Aynı session'da ardışık add → kendi eklediğiyle çelişki false positive olabilir
- threshold çok düşükse gürültü artar, çok yüksekse kaçırır → 0.80 başlangıç, config'e al
