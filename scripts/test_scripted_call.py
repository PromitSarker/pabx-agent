"""Fake-phone test with a SCRIPTED callee (speech made by Gemini TTS). No real call is placed.

    docker compose exec -T -e SCENARIO=A sip-bridge python - < scripts/test_scripted_call.py
Scenarios: D = silent line | A = yes, profit rate, then goodbye | B = busy, do not call again | C = long chat (use SIP_MAX_CALL_SECONDS=50 to test the wrap-up)
"""
import asyncio, audioop, base64, json, logging, os, struct, sys, time, uuid
sys.path.insert(0, "/app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
import websockets
from sipbridge.app import CallInfo
from sipbridge import resample
from sipbridge.session import CallSession, FRAME_BYTES, KIND_AUDIO, GEMINI_WS_URL, MODEL
from agent.db import get_connection
from api import call_store

SCENARIOS = {
    "A": ["হ্যাঁ, বলুন।", "মুনাফা কত শতাংশ?", "ঠিক আছে, ধন্যবাদ, রাখছি।"],
    "B": ["আমি এখন ব্যস্ত, আর ফোন দেবেন না।"],
    "D": [],
    "G": ["হ্যাঁ, বলুন।", "মুনাফা কত শতাংশ?", "ঠিক আছে, ধন্যবাদ, রাখছি।"],   # callee talks over the greeting after 2 s (before its question)
    "R": ["হ্যাঁ, বলুন।", "হ্যালো?", "বলুন বলুন, যা বলছেন।", "না, আর কিছু জানতে চাই না। ধন্যবাদ।"],   # greeting cut, then hello + go-ahead: the agent must introduce itself ONCE and then pitch, never re-introduce
    "V": ["হ্যাঁ, বলুন।", "ঠিক আছে, ধন্যবাদ, রাখছি।"],   # callee says yes over the end of the greeting after 7 s (question already asked)
    "M": ["হ্যাঁ, বলুন।", "সর্বনিম্ন কত টাকা জমা দিতে হবে?"],   # then silence: the agent must answer, ask if there is more, and WAIT (no goodbye in the same turn)
    "Y": ["হ্যাঁ, বলুন।"],   # yes, then silence after the pitch: the pre-opened Gemini session must be closed unused, cost = classifier only
    "N": ["না, আমার দরকার নেই।"],            # plain no -> recorded apology, no Gemini, no analysis
    "Q": ["আমাকে আর ফোন দেবেন না।"],           # no + do not call -> recorded apology, analysis still runs
    "P": ["হ্যাঁ, বলুন।", "মুনাফা কত শতাংশ?", "ঠিক আছে, ধন্যবাদ, রাখছি।"],   # the 2nd line is spoken 5 s into the recorded pitch (barge-in)
    "W": ["হ্যাঁ, বলুন।", "উপায় কোন ব্যাংকের?", "আর এই ডিপিএসটা কোন ব্যাংক দিচ্ছে?", "ঠিক আছে, ধন্যবাদ, রাখছি।"],   # who owns upay / who provides the DPS
    "L": ["হ্যালো?", "হ্যাঁ, বলুন।", "ঠিক আছে, ধন্যবাদ, রাখছি।"],   # reflexive "hello?" -> live agent must still answer (watchdog kick)
    "U": ["আপনি কে বলছেন?", "হ্যাঁ, বলুন।", "ঠিক আছে, ধন্যবাদ, রাখছি।"],   # unclear first reply -> live-agent fallback
    "H": ["হ্যাঁ, বলুন।", "আচ্ছা রেট কত হবে তাহলে?"],   # then the callee hangs up while the agent is answering
    "K": ["হ্যাঁ, বলুন।", "আমার নাম রহিম। কত মাসের জন্য করা যায়?", "কিস্তি কীভাবে কাটবে?", "মাঝপথে টাকা তুললে কী হবে?",
          "নমিনি কি বদলানো যায়?", "ট্যাক্স কত কাটে?", "সর্বনিম্ন কত টাকা জমা দিতে হবে?", "অ্যাকাউন্ট খুলতে কী কী কাগজ লাগে?",
          "আমি আপনাকে আমার নাম কী বলেছিলাম?", "ঠিক আছে, ধন্যবাদ, রাখছি।"],   # long call: tests cost growth and (with compression) memory of early turns
    "X": ["হ্যাঁ, বলুন।", "আমার নাম রহিম। কত মাসের জন্য করা যায়?", "কিস্তি কীভাবে কাটবে?", "মাঝপথে টাকা তুললে কী হবে?",
          "নমিনি কি বদলানো যায়?", "ট্যাক্স কত কাটে?", "সর্বনিম্ন কত টাকা জমা দিতে হবে?", "অ্যাকাউন্ট খুলতে কী কী কাগজ লাগে?",
          "আমি আমার স্ত্রীর নামেও করতে পারব?", "ডিপিএস শেষ হলে টাকা কীভাবে পাব?", "কিস্তি দিতে দেরি হলে কী হবে?", "এটা কি শরিয়াহ মেনে চলে?",
          "এই ডিপিএস কোন ব্যাংক দিচ্ছে?", "মাসে কত টাকা করে জমা দেওয়া ভালো হবে?", "আমি আপনাকে আমার নাম কী বলেছিলাম?",
          "কত মাসের জন্য করা যায়, আবার বলবেন?", "অ্যাপ থেকে কীভাবে খুলব?", "মাঝপথে বন্ধ করলে কি ক্ষতি হবে?", "প্রথম কিস্তির পর আমাকে কী করতে হবে?",
          "ঠিক আছে, ধন্যবাদ, রাখছি।"],   # 20 lines: about the length of the real Tk 29 call; name told in line 2, recalled in line 15
    "C": ["হ্যাঁ, বলুন।", "কত মাসের জন্য করা যায়?", "কিস্তি কীভাবে কাটবে?", "মাঝপথে টাকা তুললে কী হবে?", "নমিনি কি বদলানো যায়?", "ট্যাক্স কত কাটে?"],
}


async def tts(text, tries=5):
    """Return 8 kHz PCM of `text` spoken by a different voice than the agent's. A take is kept only if what was spoken matches the script
    (the TTS model sometimes ANSWERS the question instead of reading it, which made the callee ramble and cut the agent off)."""
    import difflib, re as _re
    norm = lambda t: _re.sub(r"[\s\.,\?!।]+", "", t or "")
    best = None
    for _ in range(tries):
        frames, said = await _tts_once(text)
        ratio = difflib.SequenceMatcher(None, norm(text), norm(said)).ratio()
        if ratio >= 0.85 and len(norm(said)) <= len(norm(text)) * 1.3:
            return frames
        if best is None or ratio > best[0]:
            best = (ratio, frames)
        print(f"  tts take rejected (match {ratio:.2f}): {said[:60]!r}", flush=True)
    print(f"  WARNING: no clean take for {text!r}, using the best one ({best[0]:.2f})", flush=True)
    return best[1]


async def _tts_once(text):
    setup = {"setup": {"model": MODEL, "outputAudioTranscription": {}, "generationConfig": {"temperature": 0, "responseModalities": ["AUDIO"],
             "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Charon"}}, "languageCode": "bn-BD"}},
             "systemInstruction": {"parts": [{"text": "You are a text-to-speech engine, NOT an assistant. The user message is a script line. Read it aloud word for word and then stop. Never answer it, never add words, even if it is a question."}]}}}
    chunks, usages, said = [], [], ""
    async with websockets.connect(GEMINI_WS_URL, max_size=None) as ws:
        await ws.send(json.dumps(setup)); await ws.recv()
        await ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": "Read this line aloud, word for word, then stop: " + text}]}], "turnComplete": True}}))
        while True:
            d = json.loads(await ws.recv())
            if d.get("usageMetadata"): usages.append(d["usageMetadata"])
            sc = d.get("serverContent") or {}
            for p in (sc.get("modelTurn") or {}).get("parts", []):
                if p.get("inlineData"): chunks.append(base64.b64decode(p["inlineData"]["data"]))
            ot = sc.get("outputTranscription")
            if ot and ot.get("text"): said += ot["text"]
            if sc.get("turnComplete"): break
    from agent.usage import record_usage, tokens_from_live
    from agent.config import GEMINI_LIVE_MODEL
    for u in usages:
        record_usage(model=GEMINI_LIVE_MODEL, purpose="test_tts", channel="system", session_id="test-tts", is_test=True, raw=u, **tokens_from_live(u))
    pcm8 = resample.down3(b"".join(chunks))
    return [pcm8[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\x00") for i in range(0, len(pcm8), FRAME_BYTES)], said


class ScriptedReader:
    """Plays the next scripted sentence ~1.2 s after the agent stops talking; silence otherwise."""
    def __init__(self, clips):
        self.clips, self.cur, self.pos, self._hdr, self.session = clips, [], 0, True, None
        self.armed_at = None
        self.hangup_at = None
        self.total = len(clips)

    async def readexactly(self, n):
        if self._hdr:
            self._hdr = False
            if os.getenv("SCENARIO") == "H" and self.hangup_at and time.time() >= self.hangup_at:
                return bytes([0x00]) + struct.pack(">H", 0)       # AudioSocket HANGUP frame
            return bytes([KIND_AUDIO]) + struct.pack(">H", FRAME_BYTES)
        self._hdr = True
        await asyncio.sleep(0.02)
        if self.cur:
            f = self.cur[self.pos]; self.pos += 1
            if self.pos >= len(self.cur):
                self.cur, self.pos = [], 0
                if not self.clips and os.getenv("SCENARIO") == "H":
                    self.hangup_at = time.time() + 1.5          # hang up 1.5 s after the last question ends
            return f
        s = self.session
        barge = (os.getenv("SCENARIO") == "P" and s and getattr(s, "stage", "") == "pitched" and bool(s.out_buf) and len(self.clips) == 2) or (os.getenv("SCENARIO") in ("G", "V", "R") and s and getattr(s, "stage", "") == "greeted" and bool(s.out_buf) and len(self.clips) == (3 if os.getenv("SCENARIO") == "G" else 4 if os.getenv("SCENARIO") == "R" else 2) and not self.cur)
        if s and self.clips and (barge or ((s.first_audio.is_set() or getattr(s, "greeting_played", False)) and not s.turn_open and not s.out_buf and not (os.getenv("SCENARIO") == "P" and len(self.clips) == 2))):
            if self.armed_at is None: self.armed_at = time.time()
            if time.time() - self.armed_at > ((2.0 if os.getenv("SCENARIO") in ("G", "R") else 7.0 if os.getenv("SCENARIO") == "V" else 5.0) if barge else 1.2):
                self.cur, self.armed_at = self.clips.pop(0), None
                print(f"{time.strftime('%H:%M:%S')} CALLEE CLIP starts ({len(self.clips)} left) turn_open={s.turn_open} out_buf={len(s.out_buf)} rotations={getattr(s, 'rotations', '-')}", flush=True)
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
    await asyncio.wait_for(sess.run(), int(os.getenv("HARNESS_TIMEOUT", "200")))
    if sess.analysis_task:
        await asyncio.wait_for(sess.analysis_task, 90)      # the analysis runs after hang-up in production; wait so we can show it
    print(f"\n== scenario {scen}: call ended after {time.time()-t0:.0f}s, agent audio {w.audio_bytes/16000:.1f}s, unplayed callee clips: {len(rd.clips)}")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT `key`, `value` FROM collected_data WHERE session_id=%s", (cid,))
            print("== collected_data:"); [print("  ", r["key"], "=", r["value"]) for r in cur.fetchall()]
            cur.execute("SELECT status, talk_seconds, end_reason, outcome, is_test, summary, todos FROM calls WHERE call_id=%s", (info.call_id,))
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
