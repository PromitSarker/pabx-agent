"""Outbound AI phone calls: HTTP control API + AudioSocket server for Asterisk.

POST /call {"number": "01XXXXXXXXX", "name": "...", "purpose": "...", "language": "Bengali"}
  -> writes an Asterisk call file; when the callee answers, Asterisk connects to the AudioSocket
     server below and the call is bridged to Gemini Live (see session.py).
"""
import asyncio
import hmac
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from fastapi.concurrency import run_in_threadpool
from api import call_store
from api.admin_store import admin_store
from agent.usage import replay_spool
from .session import CallSession, KIND_UUID, read_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sipbridge")

SIP_USER = os.getenv("SIP_USER", "")
API_TOKEN = os.getenv("SIP_API_TOKEN", "")
DIAL_FORMAT = os.getenv("SIP_DIAL_FORMAT", "local")  # local=01XXXXXXXXX  intl=8801XXXXXXXXX  e164=+8801XXXXXXXXX
CALLER_ID = os.getenv("SIP_CALLER_ID", SIP_USER)
MAX_CONCURRENT = int(os.getenv("SIP_MAX_CONCURRENT", "2"))
RING_SECONDS = int(os.getenv("SIP_RING_SECONDS", "45"))
SPOOL = "/var/spool/asterisk"
DEFAULT_PURPOSE = os.getenv("SIP_DEFAULT_PURPOSE", (
    "Introduce the MTB Islamic DPS (Shariah-compliant monthly savings scheme from Mutual Trust Bank, opened "
    "in the upay app), explain briefly what it offers and why it can be useful, and answer their questions."))


@dataclass
class CallInfo:
    call_id: str
    conversation_id: str
    dialed: str
    name: str
    purpose: str
    language: str
    created: float = field(default_factory=time.time)
    status: str = "queued"          # queued -> answered -> finished | failed
    detail: str = ""


CALLS: Dict[str, CallInfo] = {}


def normalize_number(raw: str) -> str:
    """Accepts Bangladeshi numbers in any common notation; returns the string to dial."""
    d = re.sub(r"[\s\-().]", "", raw)
    if not re.fullmatch(r"\+?\d{7,15}", d):
        raise ValueError("number must contain only digits (optionally starting with +)")
    d = d.lstrip("+")
    if d.startswith("00"):
        d = d[2:]
    intl_ok = os.getenv("SIP_ALLOW_INTERNATIONAL") == "1"
    if d.startswith("880"):
        national = d[3:]
    elif d.startswith("0"):
        national = d[1:]
    elif re.fullmatch(r"1[3-9]\d{8}", d):      # bare Bangladeshi mobile, e.g. 1711234567
        national = d
    elif intl_ok:
        return d
    else:
        raise ValueError("only Bangladeshi numbers are allowed (01XXXXXXXXX or +8801XXXXXXXXX); set SIP_ALLOW_INTERNATIONAL=1 to lift this")
    if not re.fullmatch(r"[1-9]\d{7,10}", national):
        raise ValueError("not a valid Bangladeshi number")
    return {"local": "0" + national, "intl": "880" + national, "e164": "+880" + national}[DIAL_FORMAT]


def active_calls() -> int:
    now = time.time()
    return sum(1 for c in CALLS.values() if c.status in ("queued", "answered") and now - c.created < 3600)


def write_call_file(info: CallInfo):
    body = "\n".join([
        f"Channel: PJSIP/{info.dialed}@adn",
        f'CallerID: "upay" <{CALLER_ID}>',
        f"WaitTime: {RING_SECONDS}",
        "MaxRetries: 0",
        "Context: ai-bridge",
        "Extension: s",
        "Priority: 1",
        "Archive: yes",
        f"Setvar: CALL_UUID={info.call_id}",
        "",
    ])
    tmp = f"{SPOOL}/tmp/{info.call_id}.call"
    with open(tmp, "w") as f:
        f.write(body)
    try:  # Asterisk runs as a different user and must own the file to touch its mtime
        os.chown(tmp, int(os.getenv("SIP_ASTERISK_UID", "0")), int(os.getenv("SIP_ASTERISK_GID", "0")))
    except OSError as e:
        log.warning("chown of call file failed: %s", e)
    os.rename(tmp, f"{SPOOL}/outgoing/{info.call_id}.call")  # atomic: Asterisk never sees a half-written file


async def handle_audiosocket(reader, writer):
    try:
        kind, payload = await asyncio.wait_for(read_frame(reader), 5)
        if kind != KIND_UUID or len(payload) != 16:
            raise ValueError("first AudioSocket frame was not a UUID")
        call_id = str(uuid.UUID(bytes=payload))
        info = CALLS.get(call_id)
        if not info:
            raise ValueError(f"unknown call {call_id}")
    except Exception as e:
        log.warning("rejecting audiosocket connection: %s", e)
        writer.close()
        return
    info.status = "answered"
    try:
        await run_in_threadpool(call_store.mark_answered, call_id)
    except Exception as e:
        log.warning("call row not marked answered: %s", e)
    clog = logging.getLogger(f"call.{call_id[:8]}")
    clog.info("answered by %s -> bridging to Gemini", info.dialed)
    await CallSession(info, reader, writer, clog).run()
    info.status = "finished"


async def reap_archive():
    """Turns Asterisk's archived call files into a status ('Failed'/'Expired' = nobody answered)."""
    done_dir = f"{SPOOL}/outgoing_done"
    while True:
        await asyncio.sleep(3)
        try:
            for fn in os.listdir(done_dir):
                cid = fn[:-5] if fn.endswith(".call") else None
                info = CALLS.get(cid or "")
                if not info:
                    continue
                with open(f"{done_dir}/{fn}") as f:
                    m = re.search(r"^Status:\s*(\w+)", f.read(), re.M)
                result = m.group(1) if m else "Unknown"
                if info.status == "queued":
                    info.status, info.detail = "failed", f"not answered / rejected ({result})"
                    try:
                        await run_in_threadpool(call_store.mark_failed, cid, info.detail)
                    except Exception as e:
                        log.warning("call row not marked failed: %s", e)
                os.remove(f"{done_dir}/{fn}")
        except Exception as e:
            log.debug("reaper: %s", e)


async def cdr_sync_loop():
    """Every minute, copy Asterisk's own call durations (CDR) onto the calls table for reconciliation."""
    while True:
        try:
            n = await run_in_threadpool(call_store.sync_cdr)
            if n:
                log.info("cdr sync: updated %d call(s)", n)
        except Exception as e:
            log.warning("cdr sync failed: %s", e)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        lost = await run_in_threadpool(call_store.sweep_orphans)
        if lost:
            log.warning("marked %d call(s) as lost (bridge restarted while they were open)", lost)
        replayed = await run_in_threadpool(replay_spool)
        if replayed:
            log.info("replayed %d spooled usage event(s)", replayed)
    except Exception as e:
        log.warning("startup sweep failed: %s", e)
    server = await asyncio.start_server(handle_audiosocket, "0.0.0.0", 8092)
    reaper = asyncio.create_task(reap_archive())
    cdr_task = asyncio.create_task(cdr_sync_loop())
    log.info("AudioSocket listening on :8092; control API on :8093; dial format=%s", DIAL_FORMAT)
    yield
    cdr_task.cancel()
    reaper.cancel()
    server.close()


app = FastAPI(title="upay SIP bridge", lifespan=lifespan)


class CallRequest(BaseModel):
    number: str
    name: str = Field("", max_length=60)
    purpose: str = Field("", max_length=500)
    language: Optional[str] = None


def check_token(x_api_key: str):
    if not API_TOKEN:
        raise HTTPException(503, "SIP_API_TOKEN is not configured; outbound calling is disabled")
    if not hmac.compare_digest(x_api_key or "", API_TOKEN):
        raise HTTPException(401, "bad API key")


@app.post("/call", status_code=202)
async def start_call(req: CallRequest, x_api_key: str = Header("")):
    check_token(x_api_key)
    try:
        dialed = normalize_number(req.number)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if active_calls() >= MAX_CONCURRENT:
        raise HTTPException(429, f"max {MAX_CONCURRENT} concurrent calls")
    call_id = str(uuid.uuid4())
    info = CallInfo(
        call_id=call_id, conversation_id=f"call-{call_id[:8]}", dialed=dialed,
        name=req.name.strip(), purpose=req.purpose.strip() or DEFAULT_PURPOSE,
        language=req.language or admin_store.get_setting("agent_language", "Bengali"),
    )
    CALLS[call_id] = info
    try:
        await run_in_threadpool(call_store.create_call, call_id, info.conversation_id, dialed, info.name)
    except Exception as e:
        log.error("could not persist call row: %s", e)
    try:
        write_call_file(info)
    except Exception as e:
        info.status, info.detail = "failed", str(e)
        await run_in_threadpool(call_store.mark_failed, call_id, f"could not queue call: {e}")
        raise HTTPException(500, f"could not queue call: {e}")
    log.info("queued call %s to %s", call_id, dialed)
    return {"call_id": call_id, "conversation_id": info.conversation_id, "dialed": dialed, "status": info.status}


@app.get("/call/{call_id}")
async def call_status(call_id: str, x_api_key: str = Header("")):
    check_token(x_api_key)
    info = CALLS.get(call_id)
    if not info:
        raise HTTPException(404, "unknown call")
    return info.__dict__


@app.get("/health")
async def health():
    return {"ok": True, "active_calls": active_calls()}
