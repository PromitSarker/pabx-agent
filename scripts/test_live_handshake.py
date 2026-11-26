import asyncio, json, sys
sys.path.insert(0, "/app")
import websockets
from sipbridge.session import GEMINI_WS_URL, MODEL

async def try_cfg(name, rtc):
    setup = {"setup": {"model": MODEL, "generationConfig": {"responseModalities": ["AUDIO"]}, "systemInstruction": {"parts": [{"text": "Say hello."}]}}}
    if rtc is not None:
        setup["setup"]["realtimeInputConfig"] = rtc
    try:
        async with websockets.connect(GEMINI_WS_URL, max_size=None) as ws:
            await ws.send(json.dumps(setup))
            r = await asyncio.wait_for(ws.recv(), 15)
            print(f"{name}: OK -> {str(r)[:80]}")
    except Exception as e:
        print(f"{name}: REJECTED -> {str(e)[:300]}")

async def main():
    await try_cfg("baseline", None)
    await try_cfg("full", {"automaticActivityDetection": {"disabled": False, "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
        "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH", "prefixPaddingMs": 150, "silenceDurationMs": 600}})
    await try_cfg("full+interrupts", {"automaticActivityDetection": {"startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
        "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH", "prefixPaddingMs": 150, "silenceDurationMs": 600}, "activityHandling": "START_OF_ACTIVITY_INTERRUPTS"})
asyncio.run(main())
