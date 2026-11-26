"""Billing / usage API for the admin dashboard. Everything here requires the admin token (see api/auth.py)."""
import csv
import io
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent.db import get_connection
from agent.usage import compute_cost, invalidate_rates_cache
from api.admin_store import admin_store
from api.auth import require_admin

router = APIRouter(prefix="/admin/billing", tags=["billing"], dependencies=[Depends(require_admin)])

TOKEN_COLS = ["input_text", "input_audio", "input_image", "input_cached", "output_text", "output_audio"]


def _q(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]


def _fx() -> float:
    try:
        return float(admin_store.get_setting("usd_to_bdt", "123.0"))
    except ValueError:
        return 123.0


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _since(days: int) -> datetime:
    return datetime.utcnow() - timedelta(days=days)


def _billable_seconds(talk: Any, cdr: Any) -> float:
    """Telecom's record (Asterisk CDR billsec) wins; our own pickup-to-hangup measure is the fallback."""
    return float(cdr) if cdr is not None else _f(talk)


@router.get("/summary")
def summary(days: int = Query(30, ge=1, le=3650), include_test: bool = False) -> Dict[str, Any]:
    fx, since, t = _fx(), _since(days), 1 if include_test else 0
    usage = _q(
        "SELECT channel, purpose, model, COUNT(*) events, SUM(input_text) input_text, SUM(input_audio) input_audio, "
        "SUM(input_image) input_image, SUM(input_cached) input_cached, SUM(output_text) output_text, SUM(output_audio) output_audio, "
        "SUM(cost_usd) cost_usd, SUM(cost_usd IS NULL) unpriced, SUM(estimated) estimated "
        "FROM usage_events WHERE created_at >= %s AND (is_test = 0 OR %s = 1) GROUP BY channel, purpose, model "
        "ORDER BY cost_usd DESC", (since, t))
    calls = _q(
        "SELECT status, COUNT(*) n, SUM(talk_seconds) talk, SUM(COALESCE(cdr_billsec, talk_seconds)) billable, "
        "SUM(CEIL(COALESCE(cdr_billsec, talk_seconds) / 60)) billable_minutes_rounded_up, SUM(cdr_billsec IS NULL AND source = 'live') no_cdr, "
        "SUM(cdr_billsec IS NOT NULL AND ABS(cdr_billsec - talk_seconds) > 3) mismatched "
        "FROM calls WHERE requested_at >= %s AND (is_test = 0 OR %s = 1) GROUP BY status", (since, t))
    by_status = {r["status"]: int(r["n"]) for r in calls}
    fin = next((r for r in calls if r["status"] == "finished"), None)
    billable = _f(fin["billable"]) if fin else 0.0
    cost = sum(_f(r["cost_usd"]) for r in usage)
    phone_cost = sum(_f(r["cost_usd"]) for r in usage if r["channel"] == "phone")
    tokens = {c: int(sum(_f(r[c]) for r in usage)) for c in TOKEN_COLS}
    unpriced = int(sum(_f(r["unpriced"]) for r in usage))
    alerts = []
    if unpriced:
        alerts.append(f"{unpriced} usage event(s) have no price (model missing from the price table), so the cost total is too low")
    if by_status.get("lost"):
        alerts.append(f"{by_status['lost']} call(s) were open when the bridge restarted; their minutes may be incomplete")
    if fin and int(_f(fin["mismatched"])):
        alerts.append(f"{int(_f(fin['mismatched']))} call(s): our seconds differ from Asterisk's billsec by more than 3 s")
    if fin and int(_f(fin["no_cdr"])):
        alerts.append(f"{int(_f(fin['no_cdr']))} finished call(s) have no Asterisk CDR record yet (own seconds used)")
    return {
        "days": days, "include_test": include_test, "usd_to_bdt": fx,
        "calls": {"total": sum(by_status.values()), "by_status": by_status,
                  "talk_seconds": round(_f(fin["talk"]), 1) if fin else 0.0,
                  "billable_seconds": round(billable, 1),
                  "billable_minutes": round(billable / 60, 2),
                  "billable_minutes_rounded_up": int(_f(fin["billable_minutes_rounded_up"])) if fin else 0},
        "cost": {"gemini_usd": round(cost, 6), "gemini_bdt": round(cost * fx, 2),
                 "phone_usd": round(phone_cost, 6), "phone_bdt": round(phone_cost * fx, 2),
                 "per_call_minute_usd": round(phone_cost / (billable / 60), 6) if billable else None,
                 "per_call_minute_bdt": round(phone_cost / (billable / 60) * fx, 3) if billable else None,
                 "unpriced_events": unpriced},
        "tokens": tokens,
        "breakdown": [{**{k: (int(_f(v)) if k in TOKEN_COLS or k in ("events", "unpriced", "estimated") else v) for k, v in r.items()},
                       "cost_usd": round(_f(r["cost_usd"]), 6), "cost_bdt": round(_f(r["cost_usd"]) * fx, 3)} for r in usage],
        "alerts": alerts,
    }


@router.get("/daily")
def daily(days: int = Query(30, ge=1, le=365), include_test: bool = False) -> List[Dict[str, Any]]:
    fx, since, t = _fx(), _since(days), 1 if include_test else 0
    cost = {str(r["d"]): r for r in _q(
        "SELECT DATE(created_at) d, SUM(cost_usd) cost FROM usage_events WHERE created_at >= %s AND (is_test = 0 OR %s = 1) "
        "GROUP BY DATE(created_at)", (since, t))}
    calls = {str(r["d"]): r for r in _q(
        "SELECT DATE(requested_at) d, COUNT(*) n, SUM(status='finished') finished, SUM(COALESCE(cdr_billsec, talk_seconds)) secs "
        "FROM calls WHERE requested_at >= %s AND (is_test = 0 OR %s = 1) GROUP BY DATE(requested_at)", (since, t))}
    out = []
    for i in range(days - 1, -1, -1):
        d = str((datetime.utcnow() - timedelta(days=i)).date())
        c, k = cost.get(d), calls.get(d)
        out.append({"date": d, "calls": int(_f(k["n"])) if k else 0, "finished": int(_f(k["finished"])) if k else 0,
                    "minutes": round(_f(k["secs"]) / 60, 2) if k else 0.0,
                    "cost_usd": round(_f(c["cost"]), 6) if c else 0.0, "cost_bdt": round(_f(c["cost"]) * fx, 2) if c else 0.0})
    return out


_CALLS_SQL = (
    "SELECT c.call_id, c.conversation_id, c.phone_number, c.contact_name, c.client_id, c.status, c.requested_at, c.answered_at, "
    "c.ended_at, c.talk_seconds, c.cdr_billsec, c.cdr_duration, c.cdr_disposition, c.end_reason, c.outcome, c.fail_detail, c.is_test, "
    "c.source, c.summary, c.todos, "
    "COALESCE(u.cost, 0) cost_usd, COALESCE(u.unpriced, 0) unpriced, COALESCE(u.events, 0) events, "
    "COALESCE(u.tin, 0) tokens_in, COALESCE(u.tout, 0) tokens_out "
    "FROM calls c LEFT JOIN (SELECT call_id, SUM(cost_usd) cost, SUM(cost_usd IS NULL) unpriced, COUNT(*) events, "
    "SUM(input_text + input_audio + input_image + input_cached) tin, SUM(output_text + output_audio) tout "
    "FROM usage_events WHERE call_id IS NOT NULL GROUP BY call_id) u ON u.call_id = c.call_id "
    "WHERE c.requested_at >= %s AND (c.is_test = 0 OR %s = 1) ")


def _shape_call(r: Dict[str, Any], fx: float) -> Dict[str, Any]:
    secs = _billable_seconds(r["talk_seconds"], r["cdr_billsec"]) if r["status"] == "finished" else 0.0
    cost = _f(r["cost_usd"])
    return {**r, "talk_seconds": _f(r["talk_seconds"]), "billable_seconds": secs,
            "billable_minutes_rounded_up": int(-(-secs // 60)), "cost_usd": round(cost, 6), "cost_bdt": round(cost * fx, 3),
            "cost_per_minute_bdt": round(cost / (secs / 60) * fx, 3) if secs else None, "summary": r.get("summary"), "todos": r.get("todos")}


@router.get("/calls")
def list_calls(days: int = Query(30, ge=1, le=3650), include_test: bool = False,
               limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> Dict[str, Any]:
    fx = _fx()
    rows = _q(_CALLS_SQL + "ORDER BY c.requested_at DESC LIMIT %s OFFSET %s", (_since(days), 1 if include_test else 0, limit, offset))
    return {"usd_to_bdt": fx, "calls": [_shape_call(r, fx) for r in rows]}


@router.get("/calls.csv")
def calls_csv(days: int = Query(30, ge=1, le=3650), include_test: bool = False):
    fx = _fx()
    rows = [_shape_call(r, fx) for r in _q(_CALLS_SQL + "ORDER BY c.requested_at DESC", (_since(days), 1 if include_test else 0))]
    cols = ["requested_at", "phone_number", "status", "outcome", "talk_seconds", "cdr_billsec", "billable_seconds",
            "billable_minutes_rounded_up", "tokens_in", "tokens_out", "cost_usd", "cost_bdt", "cost_per_minute_bdt",
            "end_reason", "is_test", "call_id"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) for c in cols])
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="calls-{date.today()}.csv"'})


@router.get("/calls/{call_id}")
def call_detail(call_id: str) -> Dict[str, Any]:
    fx = _fx()
    rows = _q(_CALLS_SQL.replace("WHERE c.requested_at >= %s AND (c.is_test = 0 OR %s = 1) ", "WHERE c.call_id = %s AND %s IN (0, 1) "),
              (call_id, 1))
    if not rows:
        raise HTTPException(404, "unknown call")
    events = _q("SELECT id, created_at, purpose, model, input_text, input_audio, input_image, input_cached, output_text, output_audio, "
                "cost_usd, price_note, estimated FROM usage_events WHERE call_id = %s ORDER BY id", (call_id,))
    return {"usd_to_bdt": fx, "call": _shape_call(rows[0], fx),
            "events": [{**e, "cost_usd": None if e["cost_usd"] is None else _f(e["cost_usd"])} for e in events]}


@router.post("/calls/{call_id}/analyze")
def analyze(call_id: str) -> Dict[str, Any]:
    """(Re)run the post-call analysis (summary, to-dos, unanswered questions) for one finished call."""
    from agent.post_call import analyze_call
    rows = _q("SELECT conversation_id FROM calls WHERE call_id = %s", (call_id,))
    if not rows:
        raise HTTPException(404, "unknown call")
    a = analyze_call(call_id, rows[0]["conversation_id"], force=True)
    if a is None:
        raise HTTPException(422, "nothing to analyse (no caller speech) or the analysis failed; see the api log")
    return a


@router.get("/prices")
def prices() -> List[Dict[str, Any]]:
    return _q("SELECT id, model, modality, direction, usd_per_million, valid_from, valid_to, source FROM price_rates "
              "ORDER BY model, modality, direction, valid_from")


class PriceIn(BaseModel):
    model: str
    modality: str   # text | audio | image | cached
    direction: str  # in | out
    usd_per_million: float
    valid_from: date
    valid_to: Optional[date] = None
    source: Optional[str] = None


@router.post("/prices")
def add_price(p: PriceIn) -> Dict[str, Any]:
    if p.modality not in ("text", "audio", "image", "cached") or p.direction not in ("in", "out") or p.usd_per_million < 0:
        raise HTTPException(422, "modality must be text|audio|image|cached, direction in|out, price >= 0")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO price_rates (model, modality, direction, usd_per_million, valid_from, valid_to, source) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE usd_per_million = VALUES(usd_per_million), "
                        "valid_to = VALUES(valid_to), source = VALUES(source)",
                        (p.model.replace("models/", ""), p.modality, p.direction, p.usd_per_million, p.valid_from, p.valid_to, p.source))
        conn.commit()
    invalidate_rates_cache()
    return {"status": "saved", "note": "Past usage keeps its old cost until you run Recompute."}


@router.post("/recompute")
def recompute() -> Dict[str, Any]:
    """Re-price every stored usage event from its raw token counts using the price table as it is now (dated by the event)."""
    invalidate_rates_cache()
    rows = _q("SELECT id, model, created_at, " + ", ".join(TOKEN_COLS) + " FROM usage_events")
    updated = unpriced = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cost, note = compute_cost(r["model"], {c: r[c] for c in TOKEN_COLS}, r["created_at"].date())
                cur.execute("UPDATE usage_events SET cost_usd = %s, price_note = %s WHERE id = %s", (cost, note, r["id"]))
                updated += 1
                unpriced += cost is None
        conn.commit()
    return {"events": updated, "unpriced": unpriced}
