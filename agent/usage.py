"""Usage + cost recording for every Gemini call (text, Live voice, embeddings).

Every model call ends in record_usage(): one row in `usage_events` with the raw token counts split by modality and a
cost_usd snapshot priced from the effective-dated `price_rates` table. An unknown model/modality is stored with
cost_usd = NULL (shown as "unpriced" on the dashboard) - it is never silently priced with a default.
If the database write fails the event is appended to data/usage_failed.jsonl and replayed on the next start.
"""
import contextvars
import json
import os
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from agent.db import get_connection

SPOOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "usage_failed.jsonl")

# (session_id, channel, call_id) for code that has no session handy (e.g. the query-rewrite step inside a tool)
_ctx: contextvars.ContextVar = contextvars.ContextVar("usage_ctx", default=(None, "system", None))


def set_usage_context(session_id: Optional[str], channel: str, call_id: Optional[str] = None) -> None:
    _ctx.set((session_id, channel, call_id))


def get_usage_context() -> Tuple[Optional[str], str, Optional[str]]:
    return _ctx.get()


# ---------------- prices ----------------
_rates_cache: Dict[str, Any] = {"at": 0.0, "rows": []}
_rates_lock = threading.Lock()


def _load_rates():
    with _rates_lock:
        if time.time() - _rates_cache["at"] < 60 and _rates_cache["rows"]:
            return _rates_cache["rows"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT model, modality, direction, usd_per_million, valid_from, valid_to FROM price_rates")
                _rates_cache["rows"] = list(cur.fetchall())
                _rates_cache["at"] = time.time()
        return _rates_cache["rows"]


def invalidate_rates_cache() -> None:
    _rates_cache["at"] = 0.0


def rate_for(model: str, modality: str, direction: str, on: Optional[date] = None) -> Optional[float]:
    """USD per 1M tokens in force on date `on` (latest valid_from wins), or None if no row applies."""
    on = on or date.today()
    best = None
    for r in _load_rates():
        if r["model"] != model or r["modality"] != modality or r["direction"] != direction:
            continue
        if r["valid_from"] > on or (r["valid_to"] is not None and r["valid_to"] < on):
            continue
        if best is None or r["valid_from"] > best["valid_from"]:
            best = r
    return float(best["usd_per_million"]) if best else None


_COMPONENTS = (
    ("input_text", "text", "in"),
    ("input_audio", "audio", "in"),
    ("input_image", "image", "in"),
    ("input_cached", "cached", "in"),
    ("output_text", "text", "out"),
    ("output_audio", "audio", "out"),
)


def compute_cost(model: str, tokens: Dict[str, int], on: Optional[date] = None) -> Tuple[Optional[float], Optional[str]]:
    """(cost_usd, note). cost is None when any non-zero component has no price."""
    total, missing = 0.0, []
    for col, modality, direction in _COMPONENTS:
        n = int(tokens.get(col) or 0)
        if not n:
            continue
        r = rate_for(model, modality, direction, on)
        if r is None:
            missing.append(f"{modality} {direction}")
            continue
        total += n / 1_000_000 * r
    if missing:
        return None, f"no price for {model}: {', '.join(missing)}"
    return round(total, 8), None


# ---------------- test-traffic detection ----------------
def _setting(key: str, default: str = "") -> str:
    from api.admin_store import admin_store
    return admin_store.get_setting(key, default)


def is_test_session(session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    prefixes = [p.strip() for p in _setting("test_session_prefixes", "test-,upaytest").split(",") if p.strip()]
    return any(session_id.startswith(p) for p in prefixes)


def is_test_number(number: Optional[str]) -> bool:
    if not number:
        return False
    digits = "".join(c for c in number if c.isdigit())[-10:]
    nums = ["".join(c for c in n if c.isdigit())[-10:] for n in _setting("test_numbers", "").split(",") if n.strip()]
    return bool(digits) and digits in nums


# ---------------- recording ----------------
def _insert(row: Dict[str, Any]) -> None:
    cols = ["session_id", "call_id", "channel", "purpose", "model", "input_text", "input_audio", "input_image",
            "input_cached", "output_text", "output_audio", "estimated", "cost_usd", "price_note", "is_test", "source", "raw_usage"]
    sql = f"INSERT INTO usage_events ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(row.get(c) for c in cols))
        conn.commit()


def record_usage(*, model: str, purpose: str, channel: Optional[str] = None, session_id: Optional[str] = None,
                 call_id: Optional[str] = None, raw: Optional[dict] = None, estimated: bool = False,
                 is_test: Optional[bool] = None, source: str = "live", **tokens: int) -> None:
    """Never raises. tokens: input_text, input_audio, input_image, input_cached, output_text, output_audio."""
    try:
        c_session, c_channel, c_call = get_usage_context()
        session_id = session_id or c_session
        channel = channel or c_channel
        call_id = call_id or c_call
        tok = {k: int(tokens.get(k) or 0) for k, _, _ in _COMPONENTS}
        if not any(tok.values()):
            return
        model_key = (model or "unknown").replace("models/", "")
        cost, note = compute_cost(model_key, tok)
        row = {
            "session_id": session_id, "call_id": call_id, "channel": channel, "purpose": purpose, "model": model_key,
            **tok, "estimated": 1 if estimated else 0, "cost_usd": cost, "price_note": note,
            "is_test": 1 if (is_test if is_test is not None else is_test_session(session_id)) else 0,
            "source": source, "raw_usage": json.dumps(raw, ensure_ascii=False)[:4000] if raw else None,
        }
        try:
            _insert(row)
        except Exception as e:  # DB down: keep the event on disk so no usage is lost
            print(f"[usage] DB write failed ({e}); spooling to {SPOOL_PATH}")
            os.makedirs(os.path.dirname(SPOOL_PATH), exist_ok=True)
            with open(SPOOL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({**row, "spooled_at": datetime.utcnow().isoformat()}, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"[usage] could not record usage: {e}")


def replay_spool() -> int:
    """Re-insert events that could not be written earlier. Returns how many were replayed."""
    if not os.path.exists(SPOOL_PATH):
        return 0
    done, keep = 0, []
    with open(SPOOL_PATH, encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    for line in lines:
        try:
            row = json.loads(line)
            row.pop("spooled_at", None)
            _insert(row)
            done += 1
        except Exception:
            keep.append(line)
    with open(SPOOL_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + ("\n" if keep else ""))
    return done


# ---------------- adapters ----------------
def tokens_from_live(usage: dict) -> Dict[str, int]:
    """Gemini Live usageMetadata -> token columns (text/audio/image split for input and output)."""
    out = {k: 0 for k, _, _ in _COMPONENTS}
    prompt_total = int(usage.get("promptTokenCount") or 0)
    seen = 0
    for d in usage.get("promptTokensDetails") or []:
        n, mod = int(d.get("tokenCount") or 0), str(d.get("modality") or "").upper()
        seen += n
        if mod == "AUDIO":
            out["input_audio"] += n
        elif mod in ("IMAGE", "VIDEO", "DOCUMENT"):
            out["input_image"] += n
        else:
            out["input_text"] += n
    out["input_text"] += max(0, prompt_total - seen)          # anything the details did not account for is priced as text
    cached = int(usage.get("cachedContentTokenCount") or 0)
    if cached:
        shift = min(cached, out["input_text"])
        out["input_text"] -= shift
        out["input_cached"] += shift
    resp_total = int(usage.get("responseTokenCount") or usage.get("candidatesTokenCount") or 0)
    seen = 0
    for d in usage.get("responseTokensDetails") or []:
        n, mod = int(d.get("tokenCount") or 0), str(d.get("modality") or "").upper()
        seen += n
        out["output_audio" if mod == "AUDIO" else "output_text"] += n
    out["output_text"] += max(0, resp_total - seen)            # e.g. a tool-call turn has no details: it is text
    out["output_text"] += int(usage.get("thoughtsTokenCount") or 0)   # thinking is billed as output
    return out


def tokens_from_langchain(um: Any) -> Dict[str, int]:
    """langchain usage_metadata -> token columns. output_tokens already includes reasoning tokens."""
    if not um:
        return {}
    get = (lambda k: um.get(k)) if isinstance(um, dict) else (lambda k: getattr(um, k, None))
    inp, outp = int(get("input_tokens") or 0), int(get("output_tokens") or 0)
    det = get("input_token_details") or {}
    cached = int((det.get("cache_read") if isinstance(det, dict) else getattr(det, "cache_read", 0)) or 0)
    return {"input_text": max(0, inp - cached), "input_cached": cached, "output_text": outp}


def estimate_tokens(text: str) -> int:
    """Rough token estimate for calls that report no usage (embeddings). Bengali needs ~1 token per 2-3 characters."""
    return max(1, int(len(text or "") / 2.5))
