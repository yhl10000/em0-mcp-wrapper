# Spec 07: Webhook / Event System

**Durum: ✅ TAMAMLANDI** | Impl: v0.5.0 | Deploy: v51+

## Problem
Hafıza eklendiğinde veya güncellendiğinde dış sistemler habersiz kalıyor.
Takım üyeleri yeni bir mimari karar kaydedildiğini bilmiyor.

## Çözüm
Memory event'lerinde configurable webhook'lar. HTTP POST ile dış sistemlere bildirim.
Fire-and-forget (daemon thread) — API response'u bloklamaz.

## Desteklenen Event'ler
| Event | Tetiklenme | Entegrasyon Noktası |
|-------|------------|---------------------|
| `memory.created` | Yeni hafıza eklendi | `add_memory` endpoint |
| `memory.updated` | Hafıza güncellendi | `update_memory` endpoint |
| `memory.deleted` | Hafıza silindi | `delete_memory` endpoint |
| `memory.conflict` | Çelişki tespit edildi | `add_memory` + Spec 03 |

**Default event'ler:** `memory.created`, `memory.updated`, `memory.conflict`
(`memory.deleted` default'ta kapalı — env ile açılabilir)

## Implementasyon

### Config — Environment Variables
```bash
WEBHOOK_URLS=https://hooks.slack.com/services/xxx,https://my-agent.com/webhook
WEBHOOK_EVENTS=memory.created,memory.updated,memory.conflict
WEBHOOK_SECRET=whsec_xxx
```
- `server/main.py:49-54` — Config parsing

### Dispatcher — `server/main.py:261-295`
```python
_dispatch_webhook(event: str, payload: dict)
```
- Event filter: `event not in WEBHOOK_EVENTS` → skip
- URL filter: `not WEBHOOK_URLS` → skip
- HMAC-SHA256 signing: `X-Signature-256: sha256={hex}` header
- Fire-and-forget: `threading.Thread(target=_send, daemon=True).start()`
- Timeout: 10 saniye per webhook
- Content truncation: Payload'daki content max 500 char

### Entegrasyon Noktaları

**add_memory** — `server/main.py:500-516`
```python
_dispatch_webhook("memory.created", {
    "user_id": req.user_id,
    "content": content[:500],
    "domain": metadata.get("domain", ""),
    "type": metadata.get("type", ""),
    "immutable": req.immutable,
})

# Conflict varsa ayrı event
if conflicts:
    _dispatch_webhook("memory.conflict", {...})
```

**update_memory** — `server/main.py:695`
```python
_dispatch_webhook("memory.updated", {"memory_id": memory_id, "new_content": req.data[:500]})
```

**delete_memory** — `server/main.py:712`
```python
_dispatch_webhook("memory.deleted", {"memory_id": memory_id})
```

### Webhook Payload Formatı
```json
{
  "event": "memory.created",
  "timestamp": "2026-04-02T20:30:00+00:00",
  "data": {
    "user_id": "centauri",
    "content": "PostgreSQL v15 kullanıyoruz...",
    "domain": "backend",
    "type": "decision"
  }
}
```

### Testler — `test_server.py` (4 test)
- `test_webhook_dispatch_filters_events` — Default event set doğrulaması
- `test_webhook_hmac_signature` — SHA-256 signature deterministic
- `test_webhook_payload_truncation` — Content 500 char truncation
- `test_webhook_no_urls_is_noop` — Boş WEBHOOK_URLS = no crash

## Maliyet Etkisi
**$0.00/ay** — Slack webhooks ücretsiz, HTTP call network cost only.

## Bağımlılıklar
- Spec 03 (Conflict Detection) — `memory.conflict` event
