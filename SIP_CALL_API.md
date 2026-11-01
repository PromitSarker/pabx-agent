# Babuland Outbound Call API (SIP bridge, port 8093)

Base URL: `http://103.228.38.69:8093`

This service lets another system ask the Babuland AI agent to place a **phone call**. It is separate from the
chat API (`api.md`, port 8002).

> `GET /` returns 404 by design – there is no landing page. Use the endpoints below, or open
> `http://103.228.38.69:8093/docs` (Swagger UI) / `/openapi.json`.

## How it works

```
 your system ──POST /call──▶ sip-bridge (:8093, FastAPI)
                                │ writes an Asterisk "call file" into the shared sip_spool volume
                                ▼
                            Asterisk ──SIP/PJSIP trunk "adn"──▶ ADN Telecom softswitch ──▶ callee's phone
                                │ (callee answers)
                                ▼
                    AudioSocket (TCP :8092, 8 kHz PCM)
                                │
                                ▼
                    sip-bridge CallSession ⇄ Gemini Live (speech-to-speech, 16 kHz in / 24 kHz out)
```

1. `POST /call` validates the API key and number, then writes `<call_id>.call` into Asterisk's outgoing spool.
2. Asterisk dials `PJSIP/<number>@adn` with caller ID "Babuland". Nobody answering/rejecting → status `failed`.
3. When the callee answers, Asterisk opens an AudioSocket connection to the bridge (port 8092, internal only)
   and sends the call UUID. The bridge looks up the call and connects the audio to Gemini Live.
4. The agent speaks with the Babuland system prompt plus an outbound persona (greets, states the purpose,
   answers from the knowledge base, can `search_knowledge_base`, `save_collected_information`, `escalate`
   and `end_call`). Transcripts are saved as a conversation named `call-<first 8 chars of call_id>`.
5. Calls end on hang-up, `end_call`, or after `SIP_MAX_CALL_SECONDS` (default 300).

## Authentication

`POST /call` and `GET /call/{call_id}` need the header:

```
X-API-Key: <SIP_API_TOKEN>
```

The token lives in the server's `.env` (`SIP_API_TOKEN`). `/health` is open. If the token is not configured on
the server the API answers `503`. The port is publicly reachable over plain HTTP, so treat the key as a secret
and consider putting HTTPS in front of it.

## Endpoints

### `POST /call` – start an outbound call

Request body (JSON):

| field      | type   | required | notes |
|------------|--------|----------|-------|
| `number`   | string | yes      | Bangladeshi number: `01XXXXXXXXX`, `8801XXXXXXXXX`, `+8801XXXXXXXXX`, `1XXXXXXXXX`. Spaces, dashes, brackets are ignored. Other countries are rejected unless the server sets `SIP_ALLOW_INTERNATIONAL=1`. |
| `name`     | string | no       | Callee's name, max 60 chars. Lets the agent address them personally. |
| `purpose`  | string | no       | Why we are calling, max 500 chars. Defaults to a generic Babuland follow-up (`SIP_DEFAULT_PURPOSE`). |
| `language` | string | no       | `Bengali`, `English`, `Spanish`, `Portuguese`. Defaults to the admin-panel "agent language" setting (Bengali). |

Example:

```bash
curl -X POST http://103.228.38.69:8093/call \
  -H "X-API-Key: $SIP_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"number":"01711234567","name":"Rahim","purpose":"Invite him to the weekend birthday-party offer","language":"Bengali"}'
```

Response `202 Accepted`:

```json
{
  "call_id": "0c6e1a52-....-....-....-............",
  "conversation_id": "call-0c6e1a52",
  "dialed": "01711234567",
  "status": "queued"
}
```

`dialed` is the number in the format the trunk expects (`SIP_DIAL_FORMAT`: `local` = `01…`, `intl` = `8801…`,
`e164` = `+8801…`).

Errors:

| code | meaning |
|------|---------|
| 401  | missing / wrong `X-API-Key` |
| 422  | invalid number (non-digits, not Bangladeshi, wrong length) or body failed validation |
| 429  | already `SIP_MAX_CONCURRENT` (default 2) calls queued/answered |
| 500  | the call file could not be written |
| 503  | `SIP_API_TOKEN` not configured on the server |

### `GET /call/{call_id}` – call status

Header `X-API-Key` required. Returns the stored call record:

```json
{
  "call_id": "...", "conversation_id": "call-0c6e1a52", "dialed": "01711234567",
  "name": "Rahim", "purpose": "...", "language": "Bengali",
  "created": 1793000000.0, "status": "answered", "detail": ""
}
```

`status` lifecycle: `queued` → `answered` → `finished`, or `queued` → `failed` (`detail` explains, e.g.
`not answered / rejected (Failed)`). `404` for an unknown id.
Call records are kept **in memory only** – they are lost when the sip-bridge container restarts.
The conversation transcript, however, is stored under `conversation_id` in the normal database.

### `GET /health` – liveness

No auth. Returns `{"ok": true, "active_calls": 0}`.

### `GET /docs`, `GET /openapi.json`

Auto-generated FastAPI documentation / OpenAPI schema.

## Related ports on the same server

| port | service | purpose |
|------|---------|---------|
| 8002 | `api` | Chat/web API (see `api.md`) |
| 3004 / 3006 | frontend / admin-frontend | Web UIs |
| 8093 | `sip-bridge` | **This document** – outbound call control API |
| 8092 | `sip-bridge` | AudioSocket for Asterisk (bound to 127.0.0.1 only) |
| 5062/udp, 10000-10400/udp | `asterisk` | SIP signalling and RTP media to the ADN trunk |

## Configuration (server `.env`)

`SIP_USER`, `SIP_PASSWORD`, `SIP_HOST`, `SIP_PORT`, `SIP_LOCAL_PORT`, `SIP_RTP_START/END`,
`SIP_MAX_CALL_SECONDS`, `SIP_API_TOKEN`, `SIP_DIAL_FORMAT`, `SIP_CALLER_ID`, `SIP_MAX_CONCURRENT`,
`SIP_RING_SECONDS` (default 45), `SIP_DEFAULT_PURPOSE`, `SIP_ALLOW_INTERNATIONAL`.

## Source

- `sipbridge/app.py` – HTTP API, number normalisation, call-file creation, AudioSocket accept loop
- `sipbridge/session.py` – one call: audio transcoding and Gemini Live session, phone tools
- `sip/` – Asterisk image and PJSIP/dialplan config
