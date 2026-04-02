# Spec 07: Webhook / Event System

## Problem
Hafıza eklendiğinde veya güncellendiğinde dış sistemler habersiz kalıyor.
Takım üyeleri yeni bir mimari karar kaydedildiğini bilmiyor.
Diğer agent'lar hafıza değişikliklerini takip edemiyor.

## Çözüm
Memory event'lerinde configurable webhook'lar. HTTP POST ile dış sistemlere bildirim.

## Desteklenen Event'ler
| Event | Tetiklenme Zamanı |
|-------|-------------------|
| `memory.created` | Yeni hafıza eklendi |
| `memory.updated` | Hafıza güncellendi |
| `memory.deleted` | Hafıza silindi |
| `memory.conflict` | Çelişki tespit edildi (Spec 03) |
| `compaction.completed` | Compaction tamamlandı (Spec 02) |

## Akış
```
add_memory("JWT yerine OAuth2'ye geçiyoruz")
    ↓
Hafıza kaydedilir
    ↓
Event oluşur: memory.created
    ↓
Kayıtlı webhook'lara POST:
  → Slack: "#architecture kanalına mesaj"
  → Diğer agent: "context güncelle"
    ↓
Webhook response loglanır
```

## Teknik Detay

### Webhook Config (Environment Variables)
```bash
# Virgülle ayrılmış webhook URL'leri
WEBHOOK_URLS="https://hooks.slack.com/services/xxx,https://my-agent.com/webhook"

# Hangi event'ler tetiklensin
WEBHOOK_EVENTS="memory.created,memory.updated,memory.conflict"

# Webhook secret (HMAC imzalama için)
WEBHOOK_SECRET="whsec_xxx"
```

### Webhook Dispatcher (server/main.py)
```python
import hashlib
import hmac

WEBHOOK_URLS = os.environ.get("WEBHOOK_URLS", "").split(",")
WEBHOOK_EVENTS = os.environ.get("WEBHOOK_EVENTS", "memory.created,memory.updated").split(",")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _dispatch_webhook(event: str, payload: dict):
    """Webhook'lara async fire-and-forget bildirim gönder."""
    if event not in WEBHOOK_EVENTS:
        return
    if not any(url.strip() for url in WEBHOOK_URLS):
        return

    import threading

    def _send():
        import json as _json
        body = _json.dumps({
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        })

        headers = {"Content-Type": "application/json"}

        # HMAC signature
        if WEBHOOK_SECRET:
            sig = hmac.new(
                WEBHOOK_SECRET.encode(),
                body.encode(),
                hashlib.sha256,
            ).hexdigest()
            headers["X-Signature-256"] = f"sha256={sig}"

        for url in WEBHOOK_URLS:
            url = url.strip()
            if not url:
                continue
            try:
                import httpx
                with httpx.Client(timeout=10) as client:
                    resp = client.post(url, content=body, headers=headers)
                    logger.info("Webhook %s → %s: %d", event, url[:50], resp.status_code)
            except Exception as e:
                logger.warning("Webhook failed %s → %s: %s", event, url[:50], e)

    # Fire and forget — don't block the API response
    threading.Thread(target=_send, daemon=True).start()
```

### Entegrasyon Noktaları
```python
# add_memory endpoint'inde:
@app.post("/v1/memories/")
def add_memory(req: AddMemoryRequest, ...):
    # ... mevcut kod ...
    result = m.add(content, **kwargs)

    _dispatch_webhook("memory.created", {
        "user_id": req.user_id,
        "content": content[:500],
        "metadata": req.metadata,
        "domain": req.metadata.get("domain", ""),
        "type": req.metadata.get("type", ""),
    })

    # Conflict varsa ayrı event
    if conflicts:
        _dispatch_webhook("memory.conflict", {
            "new_content": content[:300],
            "conflicts": conflicts,
        })

    return result


# update_memory endpoint'inde:
@app.put("/v1/memories/{memory_id}/")
def update_memory(memory_id: str, ...):
    # ... mevcut kod ...
    _dispatch_webhook("memory.updated", {
        "memory_id": memory_id,
        "new_content": req.data[:500],
    })


# delete_memory endpoint'inde:
@app.delete("/v1/memories/{memory_id}/")
def delete_memory(memory_id: str, ...):
    # ... mevcut kod ...
    _dispatch_webhook("memory.deleted", {"memory_id": memory_id})
```

### Slack Formatı Örneği
Slack Incoming Webhook ile kullanıldığında payload adapter:
```python
def _format_for_slack(event: str, data: dict) -> dict:
    """Slack Block Kit formatına çevir."""
    emoji = {
        "memory.created": "🧠",
        "memory.updated": "✏️",
        "memory.deleted": "🗑️",
        "memory.conflict": "⚠️",
    }.get(event, "📌")

    domain = data.get("domain", "?")
    mtype = data.get("type", "?")
    content = data.get("content", data.get("new_content", "?"))

    return {
        "text": f"{emoji} *{event}* [{domain}/{mtype}]\n>{content[:300]}"
    }
```

## Maliyet Etkisi
| Kaynak | Birim Maliyet | Etki |
|--------|--------------|------|
| Slack Incoming Webhook | Ücretsiz | $0.00 |
| HTTP POST calls | Network only | $0.00 |
| **Toplam** | | **$0.00/ay** |

**Ekstra maliyet: Sıfır.** Slack webhooks ücretsiz, HTTP call'lar network cost only.

## Uygulama Adımları
1. `server/main.py` → `_dispatch_webhook()` fonksiyonu
2. `server/main.py` → Her CRUD endpoint'ine webhook call ekle
3. Environment variable'ları dokümante et
4. HMAC signature verification
5. Slack adapter (opsiyonel)
6. Test: mock webhook server ile

## Bağımlılıklar
- Yok (opsiyonel Slack workspace)

## Güvenlik
- HMAC-SHA256 imzalama ile webhook authenticity
- Content truncation (max 500 char) — hassas bilgi sızıntısını önle
- Fire-and-forget — webhook failure API response'u bloklamaz
