# upay Outbound Call API (SIP bridge, port 8093)

Base URL: `http://103.228.38.69:8093`

This service lets another system ask the upay AI agent (MTB Islamic DPS) to place a **phone call**. It is separate from the
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
2. Asterisk dials `PJSIP/<number>@adn` with caller ID "upay". Nobody answering/rejecting → status `failed`.
3. When the callee answers, Asterisk opens an AudioSocket connection to the bridge (port 8092, internal only)
   and sends the call UUID. The bridge looks up the call and connects the audio to Gemini Live.
3a. **Two-phase call (cost saver).** On pickup the bridge plays a *pre-recorded* greeting (`data/prompts/*.pcm`, made once with
   `scripts/generate_canned_audio.py`) and only opens the paid Gemini Live session once the person actually speaks (about 160 ms of
   speech; the last 0.5 s of audio is kept so no first words are lost). If nobody speaks: two recorded "are you there?" nudges, a
   recorded goodbye, hang-up (outcome `no_response`, hard cap 60 s) and **no Gemini cost at all**. Set `SIP_CANNED_AUDIO=0` to go back
   to the model speaking the greeting. Tuning: `SIP_LAZY_MIN_RMS`, `SIP_LAZY_SPEECH_FRAMES`, `SIP_PREROLL_FRAMES`, `SIP_CATCHUP_SPEED`,
   `SIP_NO_SPEECH_CAP_S`. A pre-roll of 1.5 s made Gemini cut its own first answer short in 3 of 4 test runs; 0.5 s did not.
4. The agent runs a fixed script: it greets, says it is upay's virtual assistant calling about the MTB Islamic DPS,
   and asks whether the person has time. Only on a "yes" does it explain what the scheme offers and why it is useful,
   then answers questions from `knowledge/upay_dps.md` (inlined in its prompt, no search step). Anything it cannot
   answer is noted with `note_customer_query` (saved in the lead data as "Unanswered Query #n" + "Follow-up Needed")
   and the person is told customer service will contact them; no number is given. Other tools: `save_collected_information`, `end_call`. Transcripts are saved as a conversation named `call-<first 8 chars of call_id>`.
5. Calls end on hang-up, `end_call`, or after `SIP_MAX_CALL_SECONDS` (default 300).
6. Line handling: if nobody speaks for ~7 s the agent checks "are you there?" (max 2 times), then says goodbye and hangs up
   (outcome `no_response`, covers voicemail/dead air). Once the agent has said goodbye it hangs up after ~3 s. About 45 s before
   `SIP_MAX_CALL_SECONDS` the agent is told to wrap up and say goodbye; Asterisk's hard cut-off stays as the backstop.
7. The result of each call is saved in the lead data: `Call Outcome` (`interested`, `not_interested`, `call_later`, `do_not_call`,
   `wrong_number`, `answered_questions`, `no_response`, or `ended_without_outcome` if the agent hung up without choosing) and
   `Call Duration (s)`. Questions the agent could not answer are `Unanswered Query #n` plus `Follow-up Needed`.
8. The bridge log shows `latency: X.XXs` per turn (callee last audible speech -> first agent audio), a `latency summary` line
   per call, and `call ending: <reason>`.
   Tuning env vars (all optional): `SIP_START_SENS`, `SIP_END_SENS`, `SIP_PREFIX_MS`, `SIP_SILENCE_MS`, `SIP_SILENCE_NUDGE_S`,
   `SIP_MAX_NUDGES`, `SIP_WRAPUP_BEFORE_S`, `SIP_CLOSING_HANGUP_S`, `SIP_VOICE_RMS`.

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
| `purpose`  | string | no       | Why we are calling, max 500 chars. Defaults to the MTB Islamic DPS introduction (`SIP_DEFAULT_PURPOSE`). |
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
