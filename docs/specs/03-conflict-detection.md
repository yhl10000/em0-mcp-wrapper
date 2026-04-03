# Spec 03: Conflict Detection

**Durum: ✅ TAMAMLANDI** | Impl: v0.5.0 | Deploy: v51+

## Problem
Birbirine çelişen kararlar birikebilir. Örneğin:
- Hafıza A: "Auth için JWT kullanıyoruz"
- Hafıza B: "Session-based auth'a geçtik"

İkisi de sistemde kalır, hangi kararın güncel olduğu belirsizdir.

## Çözüm
`add_memory` sırasında otomatik semantic search. Yüksek benzerlikli ama farklı içerikli hafıza varsa uyarı dön. Non-blocking — hafıza yine kaydedilir, sadece uyarı verilir.

## Akış
```
add_memory("MongoDB'ye geçiyoruz") çağrılır
    ↓
_check_conflicts() → semantic search (limit=3, threshold=0.80)
    ↓
"PostgreSQL kullanıyoruz" bulunur (score: 0.87)
    ↓
İçerik farklı + score yüksek = potansiyel çelişki
    ↓
Hafıza kaydedilir + response'a conflict uyarısı eklenir
    ↓
Webhook: memory.conflict event gönderilir (Spec 07)
```

## Implementasyon

### Helpers — `server/main.py:400-444`
```python
_normalize_text(text: str) -> str          # Lowercase + whitespace normalize
_check_conflicts(m, content, user_id, threshold=0.80) -> list[dict]
```

Logic:
1. `m.search(query=content, user_id=user_id, limit=3)` çağrılır
2. Her sonuç için:
   - `score < threshold` → atla
   - Normalize edilmiş içerik aynı → dedup, atla
   - Farklı içerik + yüksek score → **conflict**
   - Immutable hafızayla çelişki → **extra warning**

### add_memory Entegrasyonu — `server/main.py:466-498`
- Conflict check `m.add()` çağrısından **önce** yapılır
- Conflict bulunursa response'a `conflicts` ve `conflict_warning` eklenir
- `memory.conflict` webhook event'i tetiklenir (Spec 07)

### MCP Tool Formatting — `server.py:97-109`
```
POTENTIAL CONFLICTS:
  - Existing: "PostgreSQL kullanıyoruz"
    id=abc123 similarity=0.870
    -> Consider updating this memory instead.
```
Immutable çelişki:
```
    -> IMMUTABLE memory — cannot be updated. Verify this new information is correct.
```

### Config
```bash
CONFLICT_THRESHOLD=0.80  # env variable, default 0.80
```

### Testler — `test_server.py`
- `test_conflict_detected_high_similarity` — Yüksek benzerlik = conflict
- `test_conflict_not_detected_low_similarity` — Düşük benzerlik = no conflict
- `test_conflict_dedup_same_content` — Aynı içerik = dedup, not conflict
- `test_conflict_immutable_extra_warning` — Immutable ile çelişki = extra warning
- `test_conflict_multiple_matches` — Birden fazla çelişki
- `test_conflict_threshold_boundary` — Tam threshold sınırı (0.80 dahil, 0.79 hariç)

## Maliyet Etkisi
| Kaynak | add_memory Başına | Aylık (günde 10 add) |
|--------|-------------------|---------------------|
| Embedding search | ~200 token = $0.000004 | ~$0.001 |

## Bağımlılıklar
- Spec 07 (Webhooks) — `memory.conflict` event
