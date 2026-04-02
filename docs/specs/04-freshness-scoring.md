# Spec 04: Freshness Scoring (Temporal Decay)

## Problem
6 ay önceki bir karar ile dünkü bug lesson aynı semantic score ile dönüyor.
Eski, erişilmeyen hafızalar zamanla daha az ilgili hale gelir ama search bunu yansıtmıyor.

## Çözüm
Her hafızaya `last_accessed_at` ve `access_count` metadata'sı ekle.
Search sonuçlarında `final_score = semantic_score * freshness_weight` formülü uygula.

## Freshness Formülü
```
age_days = (now - last_accessed_at).days
freshness = max(0.5, 1.0 - (age_days / 365) * 0.5)

# Örnekler:
#   Bugün erişildi     → freshness = 1.0
#   30 gün önce        → freshness = 0.96
#   90 gün önce        → freshness = 0.88
#   180 gün önce       → freshness = 0.75
#   365+ gün önce      → freshness = 0.50 (minimum, asla 0 olmaz)

# Access count bonus (sık erişilen hafızalar değerli)
popularity = min(1.2, 1.0 + access_count * 0.02)

# Final score
final_score = semantic_score * freshness * popularity
```

**Not:** Immutable hafızalar freshness decay'den muaf — her zaman `freshness = 1.0`.

## Teknik Detay

### Metadata Tracking (server/main.py)
```python
from datetime import datetime, timezone

def _track_access(m, memory_ids: list[str]):
    """Erişilen hafızaların metadata'sını güncelle."""
    now = datetime.now(timezone.utc).isoformat()
    for mid in memory_ids:
        try:
            mem = m.get(mid)
            meta = mem.get("metadata", {})
            meta["last_accessed_at"] = now
            meta["access_count"] = meta.get("access_count", 0) + 1
            m.update(mid, metadata=meta)
        except Exception:
            pass  # Non-blocking


def _apply_freshness(results: list[dict]) -> list[dict]:
    """Search sonuçlarına freshness scoring uygula."""
    now = datetime.now(timezone.utc)

    for item in results:
        meta = item.get("metadata", {})
        semantic_score = item.get("score", 0)

        # Immutable → no decay
        if meta.get("immutable"):
            item["final_score"] = semantic_score
            item["freshness"] = 1.0
            continue

        # Age calculation
        last_access = meta.get("last_accessed_at") or item.get("created_at", "")
        if last_access:
            try:
                last_dt = datetime.fromisoformat(last_access.replace("Z", "+00:00"))
                age_days = (now - last_dt).days
            except (ValueError, TypeError):
                age_days = 180  # Fallback
        else:
            age_days = 180

        freshness = max(0.5, 1.0 - (age_days / 365) * 0.5)

        # Popularity bonus
        access_count = meta.get("access_count", 0)
        popularity = min(1.2, 1.0 + access_count * 0.02)

        final_score = semantic_score * freshness * popularity
        item["final_score"] = round(final_score, 4)
        item["freshness"] = round(freshness, 3)

    # Re-sort by final_score
    results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    return results
```

### Search Endpoint Güncelleme
```python
@app.post("/v1/memories/search/")
def search_memory(req: SearchRequest, authorization: str = Header("")):
    # ... mevcut search kodu ...
    results = m.search(**kwargs)
    items = results.get("results", [])

    # Freshness scoring uygula
    items = _apply_freshness(items)

    # Erişim takibi (async, non-blocking)
    memory_ids = [i.get("id") for i in items if i.get("id")]
    _track_access(m, memory_ids)

    results["results"] = items
    return results
```

### MCP Wrapper Güncelleme (server.py)
```python
# search_memory formatlamada:
lines.append(
    f"{i}. [{domain_tag}/{type_tag}] {m.get('memory', '')}\n"
    f"   score={m.get('final_score', m.get('score', '?')):.2f}"
    f" (semantic={m.get('score', '?'):.2f}, fresh={m.get('freshness', '?')})"
    f" | source={source} | id={m.get('id', '?')}"
)
```

## Maliyet Etkisi
| Kaynak | Birim Maliyet | Etki |
|--------|--------------|------|
| Hesaplama | CPU only | $0.00 |
| metadata update | mem0 write | Var olan API, ekstra maliyet yok |
| **Toplam** | | **$0.00/ay** |

**Ekstra maliyet: Sıfır.** Pure computation — API çağrısı yok.

## Uygulama Adımları
1. `server/main.py` → `_apply_freshness()` helper
2. `server/main.py` → `_track_access()` helper
3. `server/main.py` → search endpoint'ine entegre et
4. `src/em0_mcp_wrapper/server.py` → display formatını güncelle
5. Test: eski vs yeni hafıza sıralaması

## Bağımlılıklar
- Yok

## Edge Cases
- `last_accessed_at` metadata'sı olmayan eski hafızalar → default 180 gün
- access_count overflow → popularity 1.2'de cap'leniyor
- Çok yeni ama düşük semantic score → yine düşük kalır (freshness amplifier, replacement değil)
