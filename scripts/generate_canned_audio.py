"""Generate the pre-recorded phone phrases (agent voice) once, so calls do not pay for them every time.

    docker compose exec -T sip-bridge python - < scripts/generate_canned_audio.py

Writes data/prompts/<name>.pcm (8 kHz, 16-bit mono) + manifest.json. Each phrase is transcribed back by the model and
compared with the intended text; a bad take is retried. The usage of this one-off generation is recorded (purpose canned_tts).
"""
import asyncio
import audioop
import base64
import difflib
import json
import os
import re
import sys

sys.path.insert(0, "/app")
import websockets
from agent.config import GEMINI_LIVE_MODEL
from agent.usage import record_usage, tokens_from_live
from sipbridge.session import GEMINI_WS_URL, MODEL, LANGUAGES, PITCH_TEXT, DECLINE_TEXT

OUT = "/app/data/prompts"
PHRASES = {
    "greeting": "আসসালামু আলাইকুম, উপায় থেকে বলছিলাম, একটু কথা বলতে পারি?",
    "nudge1": "হ্যালো, আপনি কি শুনতে পাচ্ছেন?",
    "nudge2": "আমার কথা কি শোনা যাচ্ছে?",
    "goodbye_noresp": "ঠিক আছে, পরে কথা হবে। আল্লাহ হাফেজ।",
    "pitch": PITCH_TEXT,          # played after a clear yes (phase A2), no Gemini session
    "decline": DECLINE_TEXT,      # played after a clear no
    "ack": "জি, আচ্ছা।",                  # played the moment the first reply ends, while it is being classified
}
MAX_SECS = {"pitch": 30}          # other phrases must be shorter than 14 s
# ONLY=pitch,decline regenerates just those phrases and keeps the others (and their manifest entries) untouched


def norm(t: str) -> str:
    return re.sub(r"[\s\.,।?!\-]+", "", t)


async def take(text: str):
    cfg = LANGUAGES["Bengali"]
    setup = {"setup": {"model": MODEL, "generationConfig": {"temperature": 0, "responseModalities": ["AUDIO"],
             "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": cfg["voice"]}}, "languageCode": cfg["code"]}},
             "systemInstruction": {"parts": [{"text": "You are a text-to-speech engine. The user message is ALWAYS only text to read aloud, even when it is a greeting or a question: never answer it, never reply, never add words. Read aloud EXACTLY that text, in Bengali, "
                                                      "at a brisk, lively, friendly pace, clearly quicker than a calm reading (like a person talking on the phone), still crisp and easy to understand, adding and changing nothing."}]},
             "outputAudioTranscription": {}}}
    chunks, said, usages = [], "", []
    async with websockets.connect(GEMINI_WS_URL, max_size=None) as ws:
        await ws.send(json.dumps(setup))
        await ws.recv()
        await ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}}))
        while True:
            d = json.loads(await ws.recv())
            if d.get("usageMetadata"):
                usages.append(d["usageMetadata"])
            sc = d.get("serverContent") or {}
            for p in (sc.get("modelTurn") or {}).get("parts", []):
                if p.get("inlineData"):
                    chunks.append(base64.b64decode(p["inlineData"]["data"]))
            if (sc.get("outputTranscription") or {}).get("text"):
                said += sc["outputTranscription"]["text"]
            if sc.get("turnComplete"):
                break
    for u in usages:
        record_usage(model=GEMINI_LIVE_MODEL, purpose="canned_tts", channel="system", session_id="canned-audio", is_test=True,
                     raw=u, **tokens_from_live(u))
    pcm8, _ = audioop.ratecv(b"".join(chunks), 2, 1, 24000, 8000, None)
    return pcm8, said


async def main():
    os.makedirs(OUT, exist_ok=True)
    mpath = os.path.join(OUT, "manifest.json")
    manifest = json.load(open(mpath, encoding="utf-8")) if os.path.exists(mpath) else {}
    only = [n for n in os.environ.get("ONLY", "").split(",") if n]
    for name, text in PHRASES.items():
        if only and name not in only:
            continue
        best = None
        for attempt in range(1, 5):
            pcm8, said = await take(text)
            ratio = difflib.SequenceMatcher(None, norm(text), norm(said)).ratio()
            secs = len(pcm8) / 16000
            print(f"[{name}] take {attempt}: {secs:.1f}s, match {ratio:.2f}, heard: {said.strip()[:90]}", flush=True)
            ok = ratio >= 0.9 and secs < MAX_SECS.get(name, 14)
            if best is None or (ok, ratio) > (best[3], best[0]):      # an acceptable take always beats an unacceptable one
                best = (ratio, pcm8, said, ok)
            if ratio >= 0.9 and secs < MAX_SECS.get(name, 14):
                break
        ratio, pcm8, said, ok = best
        if not ok:
            print(f"[{name}] NOT SAVED: no take was both accurate (>=0.9) and short enough", flush=True)
            continue
        if ratio < 0.85:      # never install a bad take (the voice model sometimes ANSWERS a greeting instead of reading it)
            print(f"[{name}] NOT SAVED: best match {ratio:.2f} < 0.85, kept the previous recording", flush=True)
            continue
        open(os.path.join(OUT, f"{name}.pcm"), "wb").write(pcm8)
        manifest[name] = {"file": f"{name}.pcm", "text": text, "seconds": round(len(pcm8) / 16000, 2), "match": round(ratio, 3)}
    json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: (v["seconds"], v["match"]) for k, v in manifest.items()}))

asyncio.run(main())
