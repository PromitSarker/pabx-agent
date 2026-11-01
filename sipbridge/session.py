"""One phone call: Asterisk AudioSocket (8 kHz PCM) <-> Gemini Live (16 kHz in / 24 kHz out)."""
import asyncio
import audioop
import base64
import json
import math
import os
import struct
import time
from datetime import datetime

import websockets
from fastapi.concurrency import run_in_threadpool

from agent.config import GEMINI_API_KEY, GEMINI_LIVE_MODEL
from agent.live_tools import LIVE_TOOL_DECLARATIONS, LIVE_TOOLS_MAP
from agent.nodes import _build_system_prompt
from api.admin_store import admin_store
from api.schemas import ConversationMessage
from api.store import conversation_store

# AudioSocket frame types
KIND_HANGUP, KIND_UUID, KIND_DTMF, KIND_AUDIO, KIND_ERROR = 0x00, 0x01, 0x03, 0x10, 0xFF
FRAME_BYTES = 320  # 20 ms of 8 kHz 16-bit mono

PHONE_TOOLS = {"escalate", "search_knowledge_base", "save_collected_information", "end_call"}

MODEL = GEMINI_LIVE_MODEL if GEMINI_LIVE_MODEL.startswith("models/") else f"models/{GEMINI_LIVE_MODEL}"
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta."
    f"GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"
)

LANGUAGES = {
    "Bengali": {"code": "bn-BD", "voice": "Leda"},
    "English": {"code": "en-US", "voice": "Aoede"},
    "Spanish": {"code": "es-ES", "voice": "Aoede"},
    "Portuguese": {"code": "pt-PT", "voice": "Aoede"},
}

OUTBOUND_PERSONA = """

--- OUTBOUND PHONE CALL PERSONA ---

You are the virtual voice assistant of Babuland, an indoor playground chain for children in Bangladesh.
YOU placed this phone call; the person did not call you. They have just picked up.

PURPOSE OF THIS CALL: {purpose}
{callee_line}

HOW TO RUN THE CALL:
- Speak {language} only, unless the person speaks another language first.
- Open with a short greeting, say you are calling from Babuland (as a virtual assistant), state the purpose in ONE sentence, and ask if it is a good time to talk.
- If it is a bad time, they are not interested, it is a wrong number, or they ask not to be called again: apologise briefly and call the `end_call` tool right away. Do not try to persuade them.
- Never claim to be a human. If asked, say you are Babuland's virtual assistant.
- Prices, timings and locations must come from the knowledge base (use `search_knowledge_base`); state the exact figure once and mention prices are plus VAT. Never guess.
- If they want a human or have a complaint, use the `escalate` tool and give them Babuland's number: +880 9666 723423.
- If they share details (name, preferred branch, date, number of children), store them with `save_collected_information`.
- This is a phone call: 2-3 short spoken sentences per turn, no markdown, no lists, no symbols read aloud, never repeat yourself.
- When the conversation is finished, say a polite goodbye and then call `end_call`.
"""


async def read_frame(reader):
    hdr = await reader.readexactly(3)
    (length,) = struct.unpack(">H", hdr[1:3])
    payload = await reader.readexactly(length) if length else b""
    return hdr[0], payload


def make_frame(kind: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + struct.pack(">H", len(payload)) + payload


# ---- hold tone: played to the callee between pickup and the agent's first words ----
HOLD_TONE_ENABLED = os.getenv("SIP_HOLD_TONE", "1") != "0"
HOLD_TONE_MAX_SECONDS = 8.0


def _build_hold_loop() -> bytes:
    """Soft two-note chime (C5 then E5) followed by a pause; 1.6 s, 8 kHz 16-bit mono, quiet."""
    rate, amp = 8000, 4500
    out = bytearray()
    for freq, dur in ((523.25, 0.30), (659.25, 0.30)):
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / (rate * 0.02), (n - i) / (rate * 0.12))  # 20 ms attack, 120 ms decay
            out += struct.pack("<h", int(amp * env * math.sin(2 * math.pi * freq * i / rate)))
    out += b"\x00\x00" * int(rate * 1.0)
    return bytes(out)


HOLD_LOOP = _build_hold_loop()


class CallSession:
    def __init__(self, info, reader, writer, log):
        self.info = info
        self.reader = reader
        self.writer = writer
        self.log = log
        self.cid = info.conversation_id
        self.out_buf = bytearray()
        self.stop = asyncio.Event()
        self.turn_done = asyncio.Event()
        self.hangup_requested = False
        self.user_text = ""
        self.agent_text = ""
        self.tokens = {"input": 0, "output": 0}
        self._turn_out = 0
        self._down_state = None
        self.first_audio = asyncio.Event()  # set when Gemini's first speech chunk arrives
        self.tone_done = asyncio.Event()    # player() waits for this so tone and speech never overlap
        if not HOLD_TONE_ENABLED:
            self.tone_done.set()

    # ---------- hold tone ----------
    async def hold_tone(self):
        """Loop a quiet chime into the call until the agent is ready to speak."""
        if self.tone_done.is_set():
            return
        pos, next_t, started = 0, time.monotonic(), time.monotonic()
        try:
            while not self.first_audio.is_set():
                if time.monotonic() - started > HOLD_TONE_MAX_SECONDS:
                    self.log.error("no audio from Gemini after %.0fs; ending call", HOLD_TONE_MAX_SECONDS)
                    self.stop.set()
                    return
                frame = HOLD_LOOP[pos:pos + FRAME_BYTES]
                pos = (pos + FRAME_BYTES) % len(HOLD_LOOP)
                if len(frame) < FRAME_BYTES:
                    frame += b"\x00" * (FRAME_BYTES - len(frame))
                self.writer.write(make_frame(KIND_AUDIO, frame))
                await self.writer.drain()
                next_t += 0.02
                await asyncio.sleep(max(0.0, next_t - time.monotonic()))
            # speech is ready: let any chime that is mid-note ring out for at most one frame, then hand over
            frame = HOLD_LOOP[pos:pos + FRAME_BYTES]
            if len(frame) == FRAME_BYTES:
                faded = audioop.mul(frame, 2, 0.4)
                self.writer.write(make_frame(KIND_AUDIO, faded))
                await self.writer.drain()
        finally:
            self.tone_done.set()

    # ---------- setup ----------
    def _build_setup(self):
        lang = self.info.language
        cfg = LANGUAGES.get(lang, LANGUAGES["Bengali"])
        summary = conversation_store.get_session_summary(self.cid)
        callee = f"The person's name is {self.info.name}." if self.info.name else "You do not know the person's name; do not invent one."
        prompt = _build_system_prompt(summary, lang) + OUTBOUND_PERSONA.format(
            purpose=self.info.purpose, callee_line=callee, language=lang
        )
        return {
            "setup": {
                "model": MODEL,
                "generationConfig": {
                    "temperature": 0.2,
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": cfg["voice"]}},
                        "languageCode": cfg["code"],
                    },
                },
                "systemInstruction": {"parts": [{"text": prompt}]},
                "tools": [{"functionDeclarations": [d for d in LIVE_TOOL_DECLARATIONS if d["name"] in PHONE_TOOLS]}],
                "inputAudioTranscription": {},
                "outputAudioTranscription": {},
            }
        }

    # ---------- main ----------
    async def run(self):
        started = time.time()
        tone_task = asyncio.create_task(self.hold_tone())
        try:
            await run_in_threadpool(
                LIVE_TOOLS_MAP["save_collected_information"],
                {"data": {"phone_number": self.info.dialed, "call_direction": "outbound"}, "session_id": self.cid},
            )
            setup = await run_in_threadpool(self._build_setup)
            async with websockets.connect(GEMINI_WS_URL, max_size=None) as gws:
                await gws.send(json.dumps(setup))
                resp = await gws.recv()
                self.log.info("gemini setup ok: %s", str(resp)[:120])
                await gws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{
                    "text": "The person has just answered the phone. Start the call now."}]}], "turnComplete": True}}))
                tasks = [asyncio.create_task(c) for c in (
                    self.phone_to_gemini(gws), self.gemini_to_phone(gws), self.player(), self.stop.wait())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    if t.exception() and not isinstance(t.exception(), (asyncio.IncompleteReadError, websockets.ConnectionClosed)):
                        self.log.error("task failed: %r", t.exception())
        except Exception:
            self.log.exception("call session error")
        finally:
            tone_task.cancel()
            await self._flush_transcript()
            dur = time.time() - started
            if self.tokens["input"] or self.tokens["output"]:
                try:
                    await run_in_threadpool(conversation_store.record_token_usage, self.cid,
                                            self.tokens["input"], self.tokens["output"], GEMINI_LIVE_MODEL, dur)
                except Exception as e:
                    self.log.warning("token usage not recorded: %s", e)
            try:
                self.writer.write(make_frame(KIND_HANGUP))
                await self.writer.drain()
            except Exception:
                pass
            self.writer.close()
            self.log.info("call finished after %.1fs", dur)

    # ---------- phone -> gemini ----------
    async def phone_to_gemini(self, gws):
        state = None
        while True:
            kind, payload = await read_frame(self.reader)  # IncompleteReadError == callee hung up
            if kind == KIND_AUDIO:
                pcm16, state = audioop.ratecv(payload, 2, 1, 8000, 16000, state)
                await gws.send(json.dumps({"realtimeInput": {"audio": {
                    "data": base64.b64encode(pcm16).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
            elif kind == KIND_HANGUP:
                self.log.info("callee hung up")
                return
            elif kind == KIND_ERROR:
                self.log.warning("audiosocket error frame: %r", payload)

    # ---------- gemini -> phone ----------
    async def gemini_to_phone(self, gws):
        async for raw in gws:
            data = json.loads(raw)
            usage = data.get("usageMetadata")
            if usage:
                self.tokens["input"] = max(self.tokens["input"], usage.get("promptTokenCount", 0))
                out = usage.get("candidatesTokenCount") or usage.get("responseTokenCount", 0)
                if not out and usage.get("totalTokenCount") and usage.get("promptTokenCount"):
                    out = usage["totalTokenCount"] - usage["promptTokenCount"]
                self._turn_out = max(self._turn_out, out or 0)

            sc = data.get("serverContent")
            if sc:
                if sc.get("interrupted"):
                    self.out_buf.clear()
                    self._down_state = None
                it = sc.get("inputTranscription")
                if it and it.get("text"):
                    self.user_text += it["text"]
                ot = sc.get("outputTranscription")
                if ot and ot.get("text"):
                    self.agent_text += ot["text"]
                for part in (sc.get("modelTurn") or {}).get("parts", []):
                    inline = part.get("inlineData")
                    if inline:
                        self.first_audio.set()
                        pcm24 = base64.b64decode(inline["data"])
                        pcm8, self._down_state = audioop.ratecv(pcm24, 2, 1, 24000, 8000, self._down_state)
                        self.out_buf.extend(pcm8)
                if sc.get("turnComplete"):
                    self.tokens["output"] += self._turn_out
                    self._turn_out = 0
                    self.turn_done.set()
                    await self._flush_transcript()

            tc = data.get("toolCall")
            if tc:
                responses = []
                for fc in tc.get("functionCalls", []):
                    name, args = fc["name"], dict(fc.get("args") or {})
                    if name == "save_collected_information":
                        args["session_id"] = self.cid
                    self.log.info("tool %s %s", name, args)
                    if name == "end_call":
                        result = "Call will end after your goodbye is spoken."
                        self.hangup_requested = True
                        self.turn_done.clear()
                        asyncio.create_task(self._graceful_hangup())
                    elif name in PHONE_TOOLS and name in LIVE_TOOLS_MAP:
                        try:
                            result = await run_in_threadpool(LIVE_TOOLS_MAP[name], args)
                        except Exception as e:
                            result = f"Error: {e}"
                    else:
                        result = "Unknown tool."
                    responses.append({"id": fc["id"], "name": name, "response": {"result": str(result)}})
                await gws.send(json.dumps({"toolResponse": {"functionResponses": responses}}))

    # ---------- audio out to phone, paced at real time ----------
    async def player(self):
        await self.tone_done.wait()  # hold tone (if any) finishes before speech is written
        next_t = time.monotonic()
        while True:
            if not self.out_buf:
                next_t = time.monotonic()
                await asyncio.sleep(0.01)
                continue
            chunk = bytes(self.out_buf[:FRAME_BYTES])
            del self.out_buf[:FRAME_BYTES]
            if len(chunk) < FRAME_BYTES:
                chunk += b"\x00" * (FRAME_BYTES - len(chunk))
            self.writer.write(make_frame(KIND_AUDIO, chunk))
            await self.writer.drain()
            next_t += 0.02
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.2:
                next_t = time.monotonic()

    async def _graceful_hangup(self):
        try:
            await asyncio.wait_for(self.turn_done.wait(), 8)
        except asyncio.TimeoutError:
            pass
        deadline = time.monotonic() + 20
        while self.out_buf and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.6)  # let the last frames play out
        self.stop.set()

    async def _flush_transcript(self):
        for role, attr in (("user", "user_text"), ("assistant", "agent_text")):
            text = getattr(self, attr).strip()
            setattr(self, attr, "")
            if text:
                try:
                    await run_in_threadpool(conversation_store.append, self.cid,
                                            ConversationMessage(role=role, content=text, timestamp=datetime.now()))
                except Exception as e:
                    self.log.warning("transcript not saved: %s", e)
