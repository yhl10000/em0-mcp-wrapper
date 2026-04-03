# Spec 04: Freshness Scoring (Temporal Decay)

**Durum: ✅ TAMAMLANDI** | Impl: v0.5.0 | Deploy: v51+

## Problem
6 ay önceki bir karar ile dünkü bug lesson aynı semantic score ile dönüyor.
Eski, erişilmeyen hafızalar zamanla daha az ilgili hale gelir ama search bunu yansıtmıyor.

## Çözüm
Her search'te `final_score = semantic_score * freshness * popularity` formülü uygula.
Erişilen hafızaların `last_accessed_at` ve `access_count` metadata'sını güncelle.

## Formül
```
age_days = (now - last_accessed_at).days
freshness = max(0.5, 1.0 - (age_days / 365) * 0.5)

# Örnekler:
#   Bugün erişildi     → freshness = 1.0
#   30 gün önce        → freshness = 0.96
#   90 gün önce        → freshness = 0.88
#   180 gün önce       → freshness = 0.75
#   365+ gün önce      → freshness = 0.50 (minimum)

popularity = min(1.2, 1.0 + access_count * 0.02)
final_score = semantic_score * freshness * popularity
```

**Immutable hafızalar:** freshness = 1.0, decay yok.

## Implementasyon

### `_apply_freshness()` — `server/main.py:201-240`
- Her search sonucu için `final_score`, `freshness` hesaplar
- Sonuçları `final_score`'a göre yeniden sıralar
- `last_accessed_at` yoksa default 180 gün yaş kabul eder

### `_track_access()` — `server/main.py:243-256`
- Search'ten dönen her hafızanın metadata'sını günceller:
  - `last_accessed_at` → now (ISO format)
  - `access_count` → +1
- Non-blocking: hata olursa sessizce atlanır

### Search Entegrasyonu — `server/main.py:594-649`
- Her iki search path'e de entegre (v1 normal + v2 graph-enhanced)
- `_apply_freshness()` → `_track_access()` sırası

### MCP Display — `server.py:158-172`
```
1. [backend/decision] PostgreSQL v15 kullanıyoruz
   score=0.82 (semantic=0.85, fresh=0.96) | source=implementation | id=abc123
```

### Testler — `test_server.py` (8 test)
- `test_freshness_recent_memory_stays_high` — 1 günlük hafıza yüksek kalır
- `test_freshness_old_memory_decays` — 365 günlük hafıza %50'ye düşer
- `test_freshness_reorders_by_final_score` — Eski yüksek-semantic, yeni düşük-semantic'in altına düşer
- `test_freshness_immutable_no_decay` — Immutable hafıza decay'den muaf
- `test_freshness_popularity_bonus` — Sık erişilen hafıza bonus alır
- `test_freshness_popularity_capped` — Popularity 1.2x'te cap'lenir
- `test_freshness_minimum_never_below_half` — Freshness asla 0.5'in altına düşmez
- `test_freshness_missing_metadata_defaults` — Metadata yoksa 180 gün default

## Maliyet Etkisi
**$0.00/ay** — Pure computation, API çağrısı yok.

## Bağımlılıklar
- Yok (standalone helper, diğer spec'ler buna bağımlı)
