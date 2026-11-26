"""Fake-phone test with a SCRIPTED callee (speech made by Gemini TTS). No real call is placed.

    docker compose exec -T -e SCENARIO=A sip-bridge python - < scripts/test_scripted_call.py
Scenarios: D = silent line | A = yes, profit rate, then goodbye | B = busy, do not call again | C = long chat (use SIP_MAX_CALL_SECONDS=50 to test the wrap-up)
"""
import asyncio, audioop, base64, json, logging, os, struct, sys, time, uuid
sys.path.insert(0, "/app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
import websockets

import json as _json, sipbridge.session as _ss
class _J:
    def __getattr__(self, n): return getattr(_json, n)
    def loads(self, raw, *a, **k):
        d = _json.loads(raw, *a, **k)
        if isinstance(d, dict) and d.get("usageMetadata"):
            print("USAGE", _json.dumps(d["usageMetadata"], ensure_ascii=False), flush=True)
        if isinstance(d, dict) and (d.get("serverContent") or {}).get("turnComplete"):
            print("---- turnComplete", flush=True)
        if isinstance(d, dict) and d.get("toolCall"):
            print("---- toolCall", [f["name"] for f in d["toolCall"].get("functionCalls", [])], flush=True)
        if isinstance(d, dict) and (d.get("serverContent") or {}).get("generationComplete"):
            print("---- generationComplete", flush=True)
        if isinstance(d, dict) and (d.get("serverContent") or {}).get("interrupted"):
            print("---- interrupted", flush=True)
        return d
_ss.json = _J()
from sipbridge.app import CallInfo
from sipbridge.session import CallSession, FRAME_BYTES, KIND_AUDIO, GEMINI_WS_URL, MODEL
from agent.db import get_connection

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
    chunks = []
    async with websockets.connect(GEMINI_WS_URL, max_size=None) as ws:
        await ws.send(json.dumps(setup)); await ws.recv()
        await ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}}))
        while True:
            d = json.loads(await ws.recv())
            sc = d.get("serverContent") or {}
            for p in (sc.get("modelTurn") or {}).get("parts", []):
                if p.get("inlineData"): chunks.append(base64.b64decode(p["inlineData"]["data"]))
            if sc.get("turnComplete"): break
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
        if s and self.clips and s.first_audio.is_set() and not s.turn_open and not s.out_buf:
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
    info = CallInfo(call_id=str(uuid.uuid4()), conversation_id=cid, dialed="TEST-NO-REAL-CALL", name="",
                    purpose="Introduce the MTB Islamic DPS on the upay app and answer questions.", language="Bengali")
    rd, w = ScriptedReader(clips), FakeWriter()
    sess = CallSession(info, rd, w, logging.getLogger("fakecall")); rd.session = sess
    t0 = time.time()
    await asyncio.wait_for(sess.run(), 200)
    print(f"\n== scenario {scen}: call ended after {time.time()-t0:.0f}s, agent audio {w.audio_bytes/16000:.1f}s, unplayed callee clips: {len(rd.clips)}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT `key`, `value` FROM collected_data WHERE session_id=%s", (cid,))
            print("== collected_data:"); [print("  ", r["key"], "=", r["value"]) for r in cur.fetchall()]
            cur.execute("SELECT role, content FROM conversations WHERE conversation_id=%s ORDER BY id", (cid,))
            print("== transcript:"); [print(f"   {r['role']:9}", r["content"][:170]) for r in cur.fetchall()]
            for t, col in (("collected_data", "session_id"), ("conversations", "conversation_id"), ("token_usage", "session_id"), ("session_summaries", "session_id")):
                cur.execute(f"DELETE FROM {t} WHERE {col}=%s", (cid,))
        conn.commit()

asyncio.run(main())
