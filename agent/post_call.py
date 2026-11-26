"""After a call ends: one cheap text-model call turns the transcript into a summary, outcome, to-dos and the questions that
need a customer-service answer. Saved with the caller's number (calls table + the lead data the admin panel already shows).
Replaces the live-session tools for notes/saves: a tool call re-bills the whole Live context (about 2x for that turn)."""
import json
import traceback
from typing import List, Optional

from pydantic import BaseModel

from agent.config import GEMINI_API_KEY, GEMINI_MODEL
from agent.db import get_connection
from agent.usage import record_usage, tokens_from_langchain, is_test_session

OUTCOMES = ("interested", "not_interested", "call_later", "do_not_call", "wrong_number", "answered_questions", "no_response")


class Todo(BaseModel):
    action: str                  # what customer service should do, one short line in English
    priority: str = "normal"     # high | normal | low
    due: Optional[str] = None    # e.g. "today 17:00" if the caller named a time


class CallAnalysis(BaseModel):
    summary_bn: str              # 2-3 short Bengali sentences
    summary_en: str              # the same in English
    outcome: str                 # one of OUTCOMES
    interest: str                # high | medium | low | none
    callee_name: Optional[str] = None
    callback_time: Optional[str] = None
    questions_asked: List[str] = []
    unanswered_questions: List[str] = []   # things the agent could not answer (profit rate, maturity amount, other banks, own account...)
    todos: List[Todo] = []
    transcript_quality: str = "good"       # good | noisy | unclear


SYSTEM = """You analyse a FINISHED outbound phone call made by upay's AI voice assistant, which offers the MTB Islamic DPS savings scheme.
You receive the transcript. Lines starting "agent:" are the assistant's own words (reliable). Lines starting "caller:" come from automatic
speech recognition: they can be wrong, garbled, or labelled in the wrong language (Bengali speech is often written as Hindi). Use the
agent's replies to understand what the caller really asked.

Return the JSON fields. Rules:
- summary_bn / summary_en: 2-3 short sentences: who was reached, what they wanted, how it ended. Never invent facts that are not in the transcript.
- outcome: one of interested, not_interested, call_later, do_not_call, wrong_number, answered_questions, no_response.
  interested = wants to open it or asked how; answered_questions = we talked but no clear decision.
- unanswered_questions: every question the agent could not answer or said it had noted (profit rate or percentage, maturity amount, other banks or
  products, the caller's own account). Write each as a short English question. Empty list if none.
- todos: concrete actions for the customer-service team, one short line each in English, e.g. "Call back at 17:00 today",
  "Tell caller the expected profit rate for 500 Taka x 24 months", "Add number to the do-not-call list" (priority high). Empty list if nothing is needed.
- callee_name / callback_time only if the caller actually said them.
- transcript_quality: good, noisy (many garbled lines) or unclear (mostly unusable)."""


def _transcript(conversation_id: str) -> str:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role, content FROM conversations WHERE conversation_id = %s ORDER BY id", (conversation_id,))
            rows = cur.fetchall()
    return "\n".join(f"{'agent' if r['role'] == 'assistant' else 'caller'}: {r['content'].strip()}" for r in rows if (r["content"] or "").strip())


def analyze_call(call_id: str, conversation_id: str, force: bool = False) -> Optional[dict]:
    """Run the analysis once per call (force=True re-runs). Never raises; returns the saved analysis or None."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT analysis_json, outcome, phone_number, is_test FROM calls WHERE call_id = %s", (call_id,))
                row = cur.fetchone()
        if not row:
            return None
        if row["analysis_json"] and not force:
            return json.loads(row["analysis_json"])
        text = _transcript(conversation_id)
        if "caller:" not in text:      # nobody spoke: nothing to analyse, nothing to pay
            return None

        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(model=GEMINI_MODEL, api_key=GEMINI_API_KEY, temperature=0, thinking_budget=0, max_retries=2, timeout=60.0)
        result = llm.with_structured_output(CallAnalysis, include_raw=True).invoke(f"{SYSTEM}\n\nTRANSCRIPT:\n{text}")
        raw, parsed = result.get("raw"), result.get("parsed")
        if raw is not None:
            record_usage(model=(getattr(raw, "response_metadata", None) or {}).get("model_name") or GEMINI_MODEL, purpose="post_call",
                         channel="phone", session_id=conversation_id, call_id=call_id, is_test=bool(row["is_test"]) or is_test_session(conversation_id),
                         **tokens_from_langchain(getattr(raw, "usage_metadata", None)))
        if parsed is None:
            raise ValueError(f"model returned no parsable analysis: {result.get('parsing_error')}")
        a = parsed.model_dump()
        if a["outcome"] not in OUTCOMES:
            a["outcome"] = "answered_questions"
        _save(call_id, conversation_id, a, row["outcome"])
        return a
    except Exception as e:
        print(f"[post_call] analysis failed for {call_id}: {e}\n{traceback.format_exc()[-600:]}")
        return None


def _save(call_id: str, conversation_id: str, a: dict, live_outcome: Optional[str]) -> None:
    summary = f"{a['summary_bn']}\n{a['summary_en']}".strip()
    todos = [t for t in a["todos"] if (t.get("action") or "").strip()]
    # the live agent's own end_call outcome wins; the fallback labels are replaced by the analysis
    outcome = live_outcome if live_outcome in OUTCOMES and live_outcome != "answered_questions" else a["outcome"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE calls SET summary=%s, todos=%s, analysis_json=%s, outcome=%s, "
                        "contact_name=COALESCE(contact_name, %s) WHERE call_id=%s",
                        (summary, json.dumps(todos, ensure_ascii=False), json.dumps(a, ensure_ascii=False), outcome,
                         a.get("callee_name"), call_id))

            def put(key: str, value: str):
                cur.execute("UPDATE collected_data SET `value`=%s, created_at=CURRENT_TIMESTAMP(6) WHERE session_id=%s AND `key`=%s",
                            (value[:1000], conversation_id, key))
                if cur.rowcount == 0:
                    cur.execute("INSERT INTO collected_data (session_id, `key`, `value`) VALUES (%s, %s, %s)",
                                (conversation_id, key, value[:1000]))

            put("Call Outcome", outcome)
            put("Summary", summary)
            put("Interest Level", a["interest"])
            if a.get("callee_name"):
                put("Name", a["callee_name"])
            if a.get("callback_time"):
                put("Callback Time", a["callback_time"])
            for i, q in enumerate(a["unanswered_questions"], 1):
                put(f"Unanswered Query #{i}", q)
            for i, t in enumerate(todos, 1):
                put(f"To-do #{i}", f"[{t.get('priority', 'normal')}] {t['action']}" + (f" (by {t['due']})" if t.get("due") else ""))
            if a["unanswered_questions"] or todos:
                put("Follow-up Needed", "Yes - customer service to contact")
        conn.commit()
