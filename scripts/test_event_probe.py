"""Fake-phone test with a SCRIPTED callee (speech made by Gemini TTS). No real call is placed.

    docker compose exec -T -e SCENARIO=A sip-bridge python - < scripts/test_scripted_call.py
Scenarios: D = silent line | A = yes, profit rate, then goodbye | B = busy, do not call again | C = long chat (use SIP_MAX_CALL_SECONDS=50 to test the wrap-up)
"""
import asyncio, audioop, base64, json, logging, os, struct, sys, time, uuid
sys.path.insert(0, "/app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
import websockets

import json as _json, time as _t, sipbridge.session as _ss
_T0 = _t.time()
class _J:
    def __getattr__(self, n): return getattr(_json, n)
    def dumps(self, o, *a, **k):
        if isinstance(o, dict) and "clientContent" in o: print(f"{_t.time()-_T0:6.2f}s >> clientContent", flush=True)
        return _json.dumps(o, *a, **k)
    def loads(self, raw, *a, **k):
        d = _json.loads(raw, *a, **k)
        if isinstance(d, dict):
            sc = d.get("serverContent") or {}
            tag = []
            if sc.get("inputTranscription"): tag.append("USER:" + sc["inputTranscription"].get("text", ""))
            if sc.get("outputTranscription"): tag.append("AGENT:" + sc["outputTranscription"].get("text", ""))
            if any("inlineData" in p for p in (sc.get("modelTurn") or {}).get("parts", [])): tag.append("audio")
            for k2 in ("interrupted", "turnComplete", "generationComplete"):
                if sc.get(k2): tag.append(k2)
            if d.get("toolCall"): tag.append("toolCall")
            if d.get("usageMetadata"): tag.append("usage")
            if tag: print(f"{_t.time()-_T0:6.2f}s << " + " | ".join(tag), flush=True)
        return d
_ss.json = _J()
from sipbridge.app import CallInfo
from sipbridge.session import CallSession, FRAME_BYTES, KIND_AUDIO, GEMINI_WS_URL, MODEL
from agent.db import get_connection
from api import call_store

SCENARIOS = {
    "A": ["হ্যাঁ, বলুন।", "মুনাফা কত শতাংশ?", "ঠিক আছে, ধন্যবাদ, রাখছি।"],
    "B": ["আমি এখন ব্যস্ত, আর ফোন দেবেন না।"],
    "D": [],
    "C": ["হ্যাঁ, বলুন।", "কত মাসের জন্য করা যায়?", "কিস্তি কীভাবে কাটবে?", "মাঝপথে টাকা তুললে কী হবে?", "নমিনি কি বদলানো যায়?", "ট্যাক্স কত কাটে?"],
}


async def tts(text):
    """Return 8 kHz PCM of `text` spoken by a different voice than the agent's."""
    setup = {"setup": {"model": MODEL, "generationConfig": {"temperature": 0, "responseModalities": ["AUDIO"],
             "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Charon"}}, "languageCode": "bn-BD"}},
             "systemInstruction": {"parts": [{"text": "You are a text-to-speech engine. Read aloud EXACTLY the text the user sends, nothing else."}]}}}
    chunks, usages = [], []
    async with websockets.connect(GEMINI_WS_URL, max_size=None) as ws:
        await ws.send(json.dumps(setup)); await ws.recv()
        await ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}}))
        while True:
            d = json.loads(await ws.recv())
            if d.get("usageMetadata"): usages.append(d["usageMetadata"])
            sc = d.get("serverContent") or {}
            for p in (sc.get("modelTurn") or {}).get("parts", []):
                if p.get("inlineData"): chunks.append(base64.b64decode(p["inlineData"]["data"]))
            if sc.get("turnComplete"): break
    from agent.usage import record_usage, tokens_from_live
    from agent.config import GEMINI_LIVE_MODEL
    for u in usages:
        record_usage(model=GEMINI_LIVE_MODEL, purpose="test_tts", channel="system", session_id="test-tts", is_test=True, raw=u, **tokens_from_live(u))
    pcm8, _ = audioop.ratecv(b"".join(chunks), 2, 1, 24000, 8000, None)
    return [pcm8[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\x00") for i in range(0, len(pcm8), FRAME_BYTES)]


class ScriptedReader:
    """Plays the next scripted sentence ~1.2 s after the agent stops talking; silence otherwise."""
    def __init__(self, clips):
        self.clips, self.cur, self.pos, self._hdr, self.session = clips, [], 0, True, None
        self.armed_at = None

    async def readexactly(self, n):
        if self._hdr:
            self._hdr = False
            return bytes([KIND_AUDIO]) + struct.pack(">H", FRAME_BYTES)
        self._hdr = True
        await asyncio.sleep(0.02)
        if self.cur:
            f = self.cur[self.pos]; self.pos += 1
            if self.pos >= len(self.cur): self.cur, self.pos = [], 0
            return f
        s = self.session
        if s and self.clips and (s.first_audio.is_set() or getattr(s, "greeting_played", False)) and not s.turn_open and not s.out_buf:
            if self.armed_at is None: self.armed_at = time.time()
            if time.time() - self.armed_at > 1.2:
                self.cur, self.armed_at = self.clips.pop(0), None
        else:
            self.armed_at = None
        return b"\x00" * FRAME_BYTES


class FakeWriter:
    def __init__(self): self.audio_bytes = 0
    def write(self, data):
        if data and data[0] == KIND_AUDIO: self.audio_bytes += len(data) - 3
    async def drain(self): pass
    def close(self): pass


async def main():
    scen = os.getenv("SCENARIO", "A")
    print("generating callee speech...", flush=True)
    clips = [await tts(t) for t in SCENARIOS[scen]]
    cid = f"test-{scen}-" + uuid.uuid4().hex[:6]
    info = CallInfo(call_id=str(uuid.uuid4()), conversation_id=cid, dialed="01797046851", name="",
                    purpose="Introduce the MTB Islamic DPS on the upay app and answer questions.", language="Bengali")
    call_store.create_call(info.call_id, cid, "01797046851", "")   # same rows a real call creates (no real call is placed)
    call_store.mark_answered(info.call_id)
    rd, w = ScriptedReader(clips), FakeWriter()
    sess = CallSession(info, rd, w, logging.getLogger("fakecall")); rd.session = sess
    t0 = time.time()
    await asyncio.wait_for(sess.run(), 200)
    print(f"\n== scenario {scen}: call ended after {time.time()-t0:.0f}s, agent audio {w.audio_bytes/16000:.1f}s, unplayed callee clips: {len(rd.clips)}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT `key`, `value` FROM collected_data WHERE session_id=%s", (cid,))
            print("== collected_data:"); [print("  ", r["key"], "=", r["value"]) for r in cur.fetchall()]
            cur.execute("SELECT status, talk_seconds, end_reason, outcome, is_test FROM calls WHERE call_id=%s", (info.call_id,))
            print("== calls row:", cur.fetchall())
            cur.execute("SELECT purpose, input_text it, input_audio ia, output_text ot, output_audio oa, cost_usd, is_test FROM usage_events WHERE call_id=%s ORDER BY id", (info.call_id,))
            ev = cur.fetchall()
            print(f"== usage events: {len(ev)} rows, total cost ${sum(float(e['cost_usd'] or 0) for e in ev):.5f}")
            for e in ev: print("   ", dict(e))
            cur.execute("SELECT role, content FROM conversations WHERE conversation_id=%s ORDER BY id", (cid,))
            print("== transcript:"); [print(f"   {r['role']:9}", r["content"][:170]) for r in cur.fetchall()]
            if os.getenv("KEEP") == "1":
                print("== KEEP=1: rows left in place (flagged test)")
                conn.commit()
                return
            for t, col in (("collected_data", "session_id"), ("conversations", "conversation_id"), ("token_usage", "session_id"), ("session_summaries", "session_id"), ("usage_events", "session_id")):
                cur.execute(f"DELETE FROM {t} WHERE {col}=%s", (cid,))
            cur.execute("DELETE FROM calls WHERE call_id=%s", (info.call_id,))
        conn.commit()

asyncio.run(main())
