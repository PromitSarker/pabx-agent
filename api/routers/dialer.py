"""Admin: place several outbound calls from one form.

Each number goes to the SIP bridge's token-protected POST /call (the same call a single dial uses). The bridge runs at most
SIP_MAX_CONCURRENT calls at once and answers 429 when full, so extra numbers wait in line here and start as soon as a line frees up.
A batch lives in memory (it is only a work list): every call is also a normal row in the `calls` table, so Billing -> Calls still has it
after an API restart. Everything here requires the admin token.
"""
import asyncio
import os
import re
import time
import uuid
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from agent.db import get_connection
from api.auth import require_admin

router = APIRouter(prefix="/admin/dial", tags=["dialer"], dependencies=[Depends(require_admin)])

BRIDGE_URL = os.getenv("SIP_BRIDGE_URL", "http://sip-bridge:8093").rstrip("/")
MAX_NUMBERS = 50                                    # per batch
START_GAP_S = float(os.getenv("DIAL_START_GAP_S", "1.5"))   # pause between two call starts (do not burst the SIP trunk)
SLOT_POLL_S = float(os.getenv("DIAL_SLOT_POLL_S", "3"))     # how often a waiting number retries while every line is busy
QUEUE_TIMEOUT_S = float(os.getenv("DIAL_QUEUE_TIMEOUT_S", str(30 * 60)))
KEEP_BATCHES_S = 24 * 3600

BATCHES: Dict[str, Dict[str, Any]] = {}
_TERMINAL_CALL = ("finished", "failed", "lost")     # calls.status values that mean the call is over


def clean_number(raw: str) -> str:
    """Same rule as the bridge (sipbridge/app.py normalize_number), returns 0XXXXXXXXXX. The bridge stays the final judge."""
    d = re.sub(r"[\s\-().]", "", raw or "")
    if not re.fullmatch(r"\+?\d{7,15}", d):
        raise ValueError("use digits only, like 01797046851")
    d = d.lstrip("+")
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("880"):
        national = d[3:]
    elif d.startswith("0"):
        national = d[1:]
    elif re.fullmatch(r"1[3-9]\d{8}", d):
        national = d
    else:
        raise ValueError("only Bangladeshi numbers are allowed (01XXXXXXXXX)")
    if not re.fullmatch(r"[1-9]\d{7,10}", national):
        raise ValueError("not a valid Bangladeshi number")
    return "0" + national


class DialNumber(BaseModel):
    number: str = Field(..., max_length=40)
    name: str = Field("", max_length=60)


class DialRequest(BaseModel):
    numbers: List[DialNumber] = Field(..., min_length=1, max_length=MAX_NUMBERS)
    purpose: str = Field("", max_length=500)       # empty = the agent's default purpose


def _prune() -> None:
    cutoff = time.time() - KEEP_BATCHES_S
    for bid in [k for k, b in BATCHES.items() if b["created"] < cutoff]:
        BATCHES.pop(bid, None)


async def _run_batch(bid: str) -> None:
    """Starts the batch's calls one by one, waiting whenever the bridge says every line is busy."""
    b = BATCHES[bid]
    token = os.getenv("SIP_API_TOKEN", "")
    deadline = time.monotonic() + QUEUE_TIMEOUT_S
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            for it in b["items"]:
                while True:
                    if b["cancelled"]:
                        it["state"] = "cancelled"
                        break
                    if time.monotonic() > deadline:
                        it["state"], it["error"] = "failed", "waited too long for a free line"
                        break
                    try:
                        r = await client.post(f"{BRIDGE_URL}/call", headers={"X-API-Key": token},
                                              json={"number": it["number"], "name": it["name"], "purpose": b["purpose"]})
                    except Exception as e:                                  # bridge down / unreachable
                        it["state"], it["error"] = "failed", f"call service unreachable ({type(e).__name__})"
                        break
                    if r.status_code == 202:
                        it["call_id"], it["state"] = r.json().get("call_id"), "dialing"
                        await asyncio.sleep(START_GAP_S)
                        break
                    if r.status_code == 429:                                # every line busy: wait for a call to end
                        it["state"] = "waiting"
                        await asyncio.sleep(SLOT_POLL_S)
                        continue
                    detail = ""
                    try:
                        detail = str(r.json().get("detail") or "")
                    except Exception:
                        pass
                    it["state"], it["error"] = "failed", detail or f"call service answered {r.status_code}"
                    break
    except Exception as e:                                                  # never leave items hanging as "queued"
        for it in b["items"]:
            if it["state"] in ("queued", "waiting"):
                it["state"], it["error"] = "failed", f"dialer error: {type(e).__name__}"
    finally:
        b["dispatch_done"] = True


def _snapshot(b: Dict[str, Any]) -> Dict[str, Any]:
    """Batch + the live state of each call (from the calls table)."""
    ids = [it["call_id"] for it in b["items"] if it.get("call_id")]
    rows: Dict[str, Dict[str, Any]] = {}
    if ids:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT call_id, status, outcome, talk_seconds, end_reason, fail_detail FROM calls WHERE call_id IN (%s)"
                            % ",".join(["%s"] * len(ids)), tuple(ids))
                rows = {r["call_id"]: r for r in cur.fetchall()}
    items, over = [], 0
    for i, it in enumerate(b["items"]):
        row = rows.get(it.get("call_id") or "")
        call_status = row["status"] if row else None
        finished_call = call_status in _TERMINAL_CALL
        if it["state"] in ("failed", "cancelled") or finished_call:
            over += 1
        items.append({
            "index": i, "number": it["number"], "name": it["name"], "state": it["state"], "error": it.get("error"),
            "call_id": it.get("call_id"), "call_status": call_status,
            "outcome": row["outcome"] if row else None,
            "talk_seconds": float(row["talk_seconds"]) if row and row["talk_seconds"] is not None else None,
            "detail": (row["fail_detail"] or row["end_reason"]) if row else None,
        })
    return {"batch_id": b["id"], "created": b["created"], "cancelled": b["cancelled"], "items": items,
            "done": over == len(items) and b["dispatch_done"]}


@router.post("")
async def start_batch(req: DialRequest) -> Dict[str, Any]:
    if not os.getenv("SIP_API_TOKEN"):
        raise HTTPException(503, "SIP_API_TOKEN is not configured; outbound calling is disabled")
    items, seen, problems = [], {}, []
    for i, n in enumerate(req.numbers, 1):
        try:
            num = clean_number(n.number)
        except ValueError as e:
            problems.append(f"row {i} ({n.number.strip() or 'empty'}): {e}")
            continue
        if num in seen:
            problems.append(f"row {i} ({n.number.strip()}): same number as row {seen[num]}")
            continue
        seen[num] = i
        items.append({"number": num, "name": n.name.strip(), "state": "queued", "call_id": None, "error": None})
    if problems:
        raise HTTPException(422, "Fix these before calling: " + "; ".join(problems))
    _prune()
    bid = uuid.uuid4().hex[:10]
    b = {"id": bid, "created": time.time(), "purpose": req.purpose.strip(), "items": items, "cancelled": False, "dispatch_done": False}
    b["task"] = asyncio.create_task(_run_batch(bid))       # keep the reference so it is not garbage collected
    BATCHES[bid] = b
    return await run_in_threadpool(_snapshot, b)


@router.get("/{batch_id}")
async def batch_status(batch_id: str) -> Dict[str, Any]:
    b = BATCHES.get(batch_id)
    if not b:
        raise HTTPException(404, "unknown or expired batch (the call list is kept in memory); see Billing -> Calls for finished calls")
    return await run_in_threadpool(_snapshot, b)


@router.post("/{batch_id}/cancel")
async def cancel_batch(batch_id: str) -> Dict[str, Any]:
    """Stops numbers that have not started yet. Calls already ringing or talking are not hung up."""
    b = BATCHES.get(batch_id)
    if not b:
        raise HTTPException(404, "unknown or expired batch")
    b["cancelled"] = True
    for it in b["items"]:
        if it["state"] in ("queued", "waiting"):
            it["state"] = "cancelled"
    return await run_in_threadpool(_snapshot, b)
