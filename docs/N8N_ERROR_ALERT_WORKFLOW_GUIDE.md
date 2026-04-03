# n8n Error Alert Workflow Guide

This workflow consumes backend alert webhooks from:

- `/error-alert/dead-letter`
- `/error-alert/runtime-error`

It is intentionally a single linear path (Webhook -> Normalize -> Dedup -> Build Telegram -> Telegram). There is no severity-based IF routing.

## Payload Contract (Backend -> n8n)

Expected fields:

- `event_type`, `service`, `environment`, `severity`
- `error_id`, `request_id`, `timestamp`
- `path`, `method`, `status_code`, `user_id?`
- `message_safe`, `message_internal` (already redacted by backend), `latency_ms?`
- `context` (object)

## Webhook Node

- Method: `POST`
- Path: `error-alert/:kind`
- Respond: immediately (`200`, body `{"ok":true}`)
- Optional shared-secret gate: check `X-Error-Alert-Secret` before downstream nodes.

## Webhook Node JSON

```json
{
  "id": "error-alert-webhook",
  "name": "Error Alert Webhook",
  "type": "n8n-nodes-base.webhook",
  "typeVersion": 2,
  "position": [300, 280],
  "parameters": {
    "httpMethod": "POST",
    "path": "error-alert/:kind",
    "responseMode": "onReceived",
    "responseCode": 200,
    "responseData": "={\"ok\":true}"
  }
}
```

## Code Node JSON: Normalize Alert

```json
{
  "id": "normalize-alert-code",
  "name": "Normalize Alert",
  "type": "n8n-nodes-base.code",
  "typeVersion": 2,
  "position": [560, 280],
  "parameters": {
    "mode": "runOnceForAllItems",
    "language": "javaScript",
    "jsCode": "const out = [];\n\nfor (const item of items) {\n  const body = item.json || {};\n  const params = body.params && typeof body.params === 'object' ? body.params : {};\n  const kindRaw = String(params.kind || body.event_type || '').trim().toLowerCase();\n\n  const normalizedKind =\n    kindRaw.includes('dead') ? 'dead-letter' :\n    kindRaw.includes('runtime') ? 'runtime-error' :\n    'runtime-error';\n\n  const severityRaw = String(body.severity || '').trim().toLowerCase();\n  const severity = ['critical', 'high', 'medium', 'low'].includes(severityRaw)\n    ? severityRaw\n    : 'high';\n\n  const statusCode = Number(body.status_code || 0) || 0;\n  const service = String(body.service || body.context?.service_component || 'unknown').trim() || 'unknown';\n  const env = String(body.environment || 'unknown').trim() || 'unknown';\n  const method = String(body.method || 'PROCESS').trim().toUpperCase() || 'PROCESS';\n  const path = String(body.path || '/').trim() || '/';\n  const eventType = String(body.event_type || normalizedKind.replace('-', '_')).trim() || 'runtime_error';\n\n  const errorId = String(body.error_id || '').trim() || `missing-${Date.now()}`;\n  const requestId = String(body.request_id || '').trim();\n  const safe = String(body.message_safe || '').trim();\n  const internal = String(body.message_internal || '').trim();\n\n  const rawLatency = body.latency_ms;\n  const latencyMs = Number.isFinite(Number(rawLatency)) ? Math.max(0, Number(rawLatency)) : null;\n\n  const context = body.context && typeof body.context === 'object' ? body.context : {};\n  const exceptionType = String(context.exception_type || '').trim();\n  const script = String(context.script || '').trim();\n  const phase = String(context.phase || '').trim();\n  const provider = String(context.provider || '').trim();\n  const providerEventType = String(context.provider_event_type || '').trim();\n\n  const fingerprintParts = [\n    normalizedKind,\n    service,\n    env,\n    path,\n    method,\n    String(statusCode),\n    exceptionType,\n    script,\n    phase,\n    provider,\n    providerEventType\n  ];\n\n  const fingerprint = fingerprintParts.join('|').slice(0, 500);\n\n  out.push({\n    json: {\n      normalizedKind,\n      eventType,\n      severity,\n      service,\n      env,\n      statusCode,\n      method,\n      path,\n      errorId,\n      requestId,\n      safe,\n      internal,\n      latencyMs,\n      context,\n      fingerprint,\n      timestamp: body.timestamp || new Date().toISOString(),\n      userId: body.user_id || null\n    }\n  });\n}\n\nreturn out;"
  }
}
```

## Code Node JSON: Dedup 90s

```json
{
  "id": "dedup-90s-code",
  "name": "Dedup 90s",
  "type": "n8n-nodes-base.code",
  "typeVersion": 2,
  "position": [820, 280],
  "parameters": {
    "mode": "runOnceForAllItems",
    "language": "javaScript",
    "jsCode": "const WINDOW_MS = 90 * 1000;\nconst now = Date.now();\n\nconst store = this.getWorkflowStaticData('global');\nif (!store.alertDedup || typeof store.alertDedup !== 'object') {\n  store.alertDedup = {};\n}\n\nfor (const [key, ts] of Object.entries(store.alertDedup)) {\n  if (typeof ts !== 'number' || now - ts > WINDOW_MS) {\n    delete store.alertDedup[key];\n  }\n}\n\nconst passed = [];\n\nfor (const item of items) {\n  const j = item.json || {};\n  const key = `${j.normalizedKind}|${j.service}|${j.env}|${j.fingerprint}`;\n\n  if (store.alertDedup[key] && now - store.alertDedup[key] <= WINDOW_MS) {\n    continue;\n  }\n\n  store.alertDedup[key] = now;\n  passed.push(item);\n}\n\nreturn passed;"
  }
}
```

## Code Node JSON: Build Telegram Message

```json
{
  "id": "build-telegram-message-code",
  "name": "Build Telegram Message",
  "type": "n8n-nodes-base.code",
  "typeVersion": 2,
  "position": [1080, 280],
  "parameters": {
    "mode": "runOnceForAllItems",
    "language": "javaScript",
    "jsCode": "const severityIcon = {\n  critical: '🚨',\n  high: '⚠️',\n  medium: '🔶',\n  low: 'ℹ️'\n};\n\nfunction esc(v) {\n  return String(v ?? '')\n    .replace(/&/g, '&amp;')\n    .replace(/</g, '&lt;')\n    .replace(/>/g, '&gt;');\n}\n\nconst out = [];\n\nfor (const item of items) {\n  const j = item.json || {};\n  const icon = severityIcon[j.severity] || '⚠️';\n\n  const provider = j.context?.provider ? `\\nProvider: <b>${esc(j.context.provider)}</b>` : '';\n  const providerEvent = j.context?.provider_event_type ? `\\nProvider Event: <code>${esc(j.context.provider_event_type)}</code>` : '';\n  const eventId = j.context?.event_id ? `\\nEvent ID: <code>${esc(j.context.event_id)}</code>` : '';\n  const latency = Number.isFinite(Number(j.latencyMs)) ? `\\nLatency: <b>${esc(Number(j.latencyMs).toFixed(2))} ms</b>` : '';\n\n  const human =\n`${icon} <b>${esc((j.normalizedKind || '').toUpperCase())}</b>\nSeverity: <b>${esc((j.severity || '').toUpperCase())}</b>\nService: <b>${esc(j.service)}</b>\nEnv: <b>${esc(j.env)}</b>\nStatus: <b>${esc(j.statusCode)}</b>\nPath: <code>${esc(j.method)} ${esc(j.path)}</code>${latency}${provider}${providerEvent}${eventId}\nError ID: <code>${esc(j.errorId)}</code>\nRequest ID: <code>${esc(j.requestId || 'n/a')}</code>\nSafe Msg: ${esc(j.safe || 'n/a')}`;\n\n  const aiSummary = {\n    kind: j.normalizedKind,\n    severity: j.severity,\n    service: j.service,\n    environment: j.env,\n    status_code: j.statusCode,\n    path: j.path,\n    method: j.method,\n    error_id: j.errorId,\n    request_id: j.requestId || null,\n    user_id: j.userId || null,\n    message_safe: j.safe || null,\n    message_internal: j.internal || null,\n    latency_ms: Number.isFinite(Number(j.latencyMs)) ? Number(j.latencyMs) : null,\n    context: j.context || {},\n    timestamp: j.timestamp,\n    dedup_fingerprint: j.fingerprint\n  };\n\n  const aiBlock = `\\n\\n<b>AI_PAYLOAD</b>\\n<pre>${esc(JSON.stringify(aiSummary, null, 2))}</pre>`;\n\n  out.push({\n    json: {\n      ...j,\n      telegram_text: `${human}${aiBlock}`,\n      telegram_parse_mode: 'HTML'\n    }\n  });\n}\n\nreturn out;"
  }
}
```

## Telegram Node

- Operation: `Send Message`
- Chat ID: alert chat/group
- Text: `{{$json.telegram_text}}`
- Parse Mode: `HTML`
- Disable Web Page Preview: `true`

## Telegram Node JSON

```json
{
  "id": "telegram-send-alert",
  "name": "Telegram: Send Alert",
  "type": "n8n-nodes-base.telegram",
  "typeVersion": 1,
  "position": [1340, 280],
  "parameters": {
    "operation": "sendMessage",
    "chatId": "={{$env.TELEGRAM_ALERT_CHAT_ID}}",
    "text": "={{$json.telegram_text}}",
    "additionalFields": {
      "parse_mode": "HTML",
      "disable_web_page_preview": true
    }
  },
  "credentials": {
    "telegramApi": {
      "id": "replace-with-your-credential-id",
      "name": "Telegram Bot"
    }
  }
}
```

## Operational Notes

- Backend already applies retry/backoff, dedupe, rate limiting, circuit breaking, and redaction before hitting n8n.
- n8n dedup is still kept as a second guard rail (90-second in-memory window).
- Keep workflow active (not test-only webhook URL) for real alert traffic.

## Quick Test Payloads

Dead-letter test:

```json
{
  "event_type": "dead_letter",
  "service": "api-web",
  "environment": "local",
  "severity": "critical",
  "error_id": "dead-test-001",
  "request_id": "req-test-001",
  "timestamp": "2026-04-03T00:00:00Z",
  "path": "/api/webhooks/plisio",
  "method": "POST",
  "status_code": 500,
  "message_safe": "Webhook moved to dead-letter.",
  "message_internal": "Timeout while writing webhook_events row",
  "latency_ms": 12.4,
  "context": {
    "provider": "plisio",
    "provider_event_type": "completed",
    "event_id": "evt_abc"
  }
}
```

Runtime-error test:

```json
{
  "event_type": "runtime_error",
  "service": "api-web",
  "environment": "local",
  "severity": "critical",
  "error_id": "runtime-test-001",
  "request_id": "req-test-002",
  "timestamp": "2026-04-03T00:00:00Z",
  "path": "/api/payments/create-checkout",
  "method": "POST",
  "status_code": 500,
  "message_safe": "Internal server error",
  "message_internal": "KeyError: provider",
  "latency_ms": 48.9,
  "context": {
    "exception_type": "KeyError",
    "client_ip": "127.0.0.1"
  }
}
```
