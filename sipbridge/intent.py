"""Classify the callee's FIRST reply (to the recorded greeting) with one cheap text-model call, so the bridge can play the
recorded pitch (yes) or the recorded apology (no) without opening a paid Gemini Live session.

Only a clear Bengali yes/no short-circuits; anything else (call later, "who is this?", other language, noise, any error) returns
"unclear" and the caller falls back to the normal live-agent path, so a wrong guess can never be worse than today's behaviour.
"""
import audioop
import io
import json
import os
import wave
from sipbridge import resample

from agent.config import GEMINI_API_KEY
from agent.usage import record_usage

# measured 2026-11-26 on 9 spoken replies: gemini-3.1-flash-lite 9/9 sensible in 1.7-2.3 s; gemini-3.8-flash same accuracy but 3-6 s
INTENT_MODEL = os.getenv("SIP_INTENT_MODEL", "gemini-3.1-flash-lite")

PROMPT = """You listen to the FIRST reply of a person who just answered an outbound phone call. The call is from upay (a Bangladeshi mobile
financial service app). A recording said: "Assalamu alaikum, I was calling from upay, may I talk to you for a moment?" (it is about the MTB Islamic DPS
savings scheme in the upay app). The audio is 8 kHz phone quality and may be noisy.

Return compact JSON: {"intent": ..., "reason": ..., "lang": ...}
- intent:
  "yes"   = they CLEARLY agree to talk or invite you to go on ("হ্যাঁ", "জি বলুন", "বলুন", "ঠিক আছে বলেন", "yes", "okay go ahead").
  "no"    = they CLEARLY refuse or cannot talk and do not ask for a later call ("না", "দরকার নেই", "ফোন দেবেন না", "ভুল নম্বর", "wrong number", "not interested").
  "later" = they ask to be called later or say they are busy now ("পরে ফোন দিন", "এখন ব্যস্ত", "call me later").
  "unclear" = anything else: a bare greeting ("হ্যালো", "আসসালামু আলাইকুম"), "who is this?", a question, a mix of yes and conditions, silence/noise,
              speech you cannot understand. WHEN IN DOUBT answer "unclear".
- reason: only when intent is "no": "do_not_call" (asks not to be called again), "wrong_number", otherwise "not_interested". Else "".
- lang: "bn" if they spoke Bengali, otherwise "other".
Output nothing else (no transcript, no explanation): a short answer is faster."""


def _wav16k(pcm8: bytes) -> bytes:
    pcm16 = resample.up2(pcm8)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm16)
    return buf.getvalue()


_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=10000))
    return _client


def warm() -> None:
    """Imports the SDK and opens the HTTPS connection while the greeting plays, so the classification itself is not slowed by it. Free (no tokens)."""
    try:
        _get_client().models.get(model=INTENT_MODEL)
    except Exception as e:
        print(f"[intent] warm-up failed: {e!r}")


def classify_reply(pcm8: bytes, *, session_id: str, call_id: str, is_test: bool) -> dict:
    """Blocking (run it in a thread). Never raises. Returns {"intent", "lang", "text"}."""
    fallback = {"intent": "unclear", "reason": "not_interested", "lang": "other", "text": ""}
    try:
        from google.genai import types
        resp = _get_client().models.generate_content(
            model=INTENT_MODEL,
            contents=[types.Part.from_bytes(data=_wav16k(pcm8), mime_type="audio/wav"), PROMPT],
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                               thinking_config=types.ThinkingConfig(thinking_budget=0)),
        )
        um = resp.usage_metadata
        tok = {"input_text": 0, "input_audio": 0, "output_text": int(getattr(um, "candidates_token_count", 0) or 0)
               + int(getattr(um, "thoughts_token_count", 0) or 0)}
        for d in (getattr(um, "prompt_tokens_details", None) or []):
            key = "input_audio" if "AUDIO" in str(d.modality).upper() else "input_text"
            tok[key] += int(d.token_count or 0)
        if not (tok["input_text"] or tok["input_audio"]):
            tok["input_text"] = int(getattr(um, "prompt_token_count", 0) or 0)
        record_usage(model=INTENT_MODEL, purpose="intent", channel="phone", session_id=session_id, call_id=call_id,
                     is_test=is_test, **tok)
        data = json.loads(resp.text)
        intent = str(data.get("intent", "unclear")).lower()
        if intent not in ("yes", "no", "later", "unclear"):
            intent = "unclear"
        reason = str(data.get("reason", "")).lower()
        if reason not in ("do_not_call", "wrong_number"):
            reason = "not_interested"
        return {"intent": intent, "reason": reason, "lang": "bn" if data.get("lang") == "bn" else "other", "text": ""}
    except Exception as e:
        print(f"[intent] classification failed: {e!r}")
        return fallback
