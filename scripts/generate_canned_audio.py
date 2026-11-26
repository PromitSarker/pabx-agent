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
from sipbridge.session import GEMINI_WS_URL, MODEL, LANGUAGES

OUT = "/app/data/prompts"
PHRASES = {
    "greeting": "আসসালামু আলাইকুম, আমি উপায়-এর ভার্চুয়াল অ্যাসিস্ট্যান্ট। উপায় অ্যাপের এমটিবি ইসলামিক ডিপিএস নিয়ে কি এখন একটু কথা বলতে পারি?",
    "nudge1": "হ্যালো, আপনি কি শুনতে পাচ্ছেন?",
    "nudge2": "আমার কথা কি শোনা যাচ্ছে?",
    "goodbye_noresp": "ঠিক আছে, পরে কথা হবে। আল্লাহ হাফেজ।",
}


def norm(t: str) -> str:
    return re.sub(r"[\s\.,।?!\-]+", "", t)


async def take(text: str):
    cfg = LANGUAGES["Bengali"]
    setup = {"setup": {"model": MODEL, "generationConfig": {"temperature": 0, "responseModalities": ["AUDIO"],
             "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": cfg["voice"]}}, "languageCode": cfg["code"]}},
             "systemInstruction": {"parts": [{"text": "You are a text-to-speech engine. Read aloud EXACTLY the text the user sends, in Bengali, "
                                                      "at a calm natural pace, adding and changing nothing."}]},
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
    manifest = {}
    for name, text in PHRASES.items():
        best = None
        for attempt in range(1, 5):
            pcm8, said = await take(text)
            ratio = difflib.SequenceMatcher(None, norm(text), norm(said)).ratio()
            secs = len(pcm8) / 16000
            print(f"[{name}] take {attempt}: {secs:.1f}s, match {ratio:.2f}, heard: {said.strip()[:90]}", flush=True)
            if best is None or ratio > best[0]:
                best = (ratio, pcm8, said)
            if ratio >= 0.9 and secs < 14:
                break
        ratio, pcm8, said = best
        open(os.path.join(OUT, f"{name}.pcm"), "wb").write(pcm8)
        manifest[name] = {"file": f"{name}.pcm", "text": text, "seconds": round(len(pcm8) / 16000, 2), "match": round(ratio, 3)}
    json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: (v["seconds"], v["match"]) for k, v in manifest.items()}))

asyncio.run(main())
