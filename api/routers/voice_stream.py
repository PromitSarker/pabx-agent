import json
import time
import base64
import asyncio
import os
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
import websockets
from websockets.exceptions import ConnectionClosed
from datetime import datetime

from agent.config import GEMINI_API_KEY, GEMINI_LIVE_MODEL
from agent.nodes import _build_system_prompt
from agent.live_tools import LIVE_TOOL_DECLARATIONS, LIVE_TOOLS_MAP
from api.store import conversation_store
from agent.usage import record_usage, set_usage_context, tokens_from_live
from api.admin_store import admin_store
from api.schemas import ConversationMessage

router = APIRouter(prefix="/voice", tags=["voice_stream"])

VOICE_PERSONA_PROMPT = """

--- VOICE CALL PERSONA ---

You are the voice assistant of upay for the MTB Islamic DPS savings scheme. You are on a live voice call with a customer who contacted you.

PERSONALITY & APPROACH:
- Warm, polite, patient and clear. Simple words, no banking jargon. Speak fluent {language}.
- Sound natural, like a helpful person, but never claim to be human. Do not say your name; you are a virtual assistant.
- State exact figures from the knowledge base in one clear sentence. Never guess. The knowledge base has no profit rate: never quote or estimate profit or maturity amounts.

VOICE CALL RULES:
- ALWAYS speak in {language}. Never switch language unless the caller speaks another one first.
- STRICT RULE: When greeting, you MUST start exactly with "{greeting}" (Do not change this greeting).
- Phone-style answers: 2-4 short spoken sentences per turn. No markdown, lists or symbols read aloud. Never repeat a fact or ask the same question twice.
- Use `search_knowledge_base` for every factual question. If the check is quick you do not need to announce it; if you do ask the caller to wait, vary the phrase.
- If you cannot answer from the knowledge base: say you do not have that information right now, that you are noting down their question, and that a customer service person will contact them soon; then call `note_customer_query`. Never give a phone number.
- If the user asks you to write something down or send details as text, use the `write_to_chat` tool and say you are writing it in the chat.
- When the caller is finished, say a polite goodbye and then call `end_call`.
- IMPORTANT: Even if the user types a message in the chat during the call, still respond verbally.
- If the user asks you to read aloud what was written in the text chat, look at the RECENT TEXT CHAT HISTORY provided below and read it aloud.
"""

# Ensure model format
if not GEMINI_LIVE_MODEL.startswith("models/"):
    formatted_model = f"models/{GEMINI_LIVE_MODEL}"
else:
    formatted_model = GEMINI_LIVE_MODEL

GEMINI_WS_URL = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"

@router.websocket("/ws/{conversation_id}")
async def voice_websocket_endpoint(websocket: WebSocket, conversation_id: str):
    await websocket.accept()
    start_time = time.time()

    # Dictionary to capture max tokens from the background proxy task
    session_tokens = {"input": 0, "output": 0}

    if not GEMINI_API_KEY:
        await websocket.send_json({"error": "GEMINI_API_KEY is not configured"})
        await websocket.close()
        return

    # Fetch context summary to inject into system instruction
    session_summary = await run_in_threadpool(conversation_store.get_session_summary, conversation_id)
    
    current_language = admin_store.get_setting("agent_language", "Bengali")
    
    language_configs = {
        "Bengali": {
            "code": "bn-BD",
            "greeting": "উপায়-এ আপনাকে স্বাগতম। এমটিবি ইসলামিক ডিপিএস নিয়ে আমি আপনাকে কীভাবে সাহায্য করতে পারি?",
            "voice": "Leda" # Just use default or try to rely on what works
        },
        "English": {
            "code": "en-US",
            "greeting": "Hello! Welcome to upay. How can I help you with MTB Islamic DPS today?",
            "voice": "Aoede"
        },
        "Spanish": {
            "code": "es-ES",
            "greeting": "¡Hola! Bienvenido a upay. ¿Cómo puedo ayudarle con MTB Islamic DPS hoy?",
            "voice": "Aoede"
        },
        "Portuguese": {
            "code": "pt-PT",
            "greeting": "Olá! Bem-vindo à upay. Como posso ajudá-lo com o MTB Islamic DPS hoje?",
            "voice": "Aoede"
        }
    }
    
    config = language_configs.get(current_language, language_configs["Bengali"])
    
    system_prompt = _build_system_prompt(session_summary, current_language, voice=True)
    
    # Fetch recent raw messages for exact context if the user asks to read them
    recent_messages_full = await run_in_threadpool(conversation_store.get_messages, conversation_id)
    recent_messages = recent_messages_full[-15:]
    chat_history_str = "\n".join([f"{msg.role}: {msg.content}" for msg in recent_messages])
    
    full_prompt = system_prompt + "\n" + VOICE_PERSONA_PROMPT.format(language=current_language, greeting=config["greeting"])
    if chat_history_str:
        full_prompt += f"\n\n--- RECENT TEXT CHAT HISTORY ---\n{chat_history_str}\n--------------------------------\n"

    try:
        async with websockets.connect(GEMINI_WS_URL) as gemini_ws:
            # 1. Send Setup Message
            setup_message = {
                "setup": {
                    "model": formatted_model,
                    "generationConfig": {
                        "temperature": 0.2,
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": config["voice"]
                                }
                            },
                            "languageCode": config["code"]
                        }
                    },
                    "systemInstruction": {
                        "parts": [{"text": full_prompt}]
                    },
                    "tools": [{"functionDeclarations": LIVE_TOOL_DECLARATIONS}]
                }
            }
            print(f"Sending setup with model: {formatted_model}")
            await gemini_ws.send(json.dumps(setup_message))

            # Receive the setup response
            setup_response = await gemini_ws.recv()
            print("Setup response:", setup_response)
            
            # No artificial delay. We rely entirely on Gemini's natural generation time to provide the ringing duration.
            
            # Trigger initial greeting
            initial_greeting_message = {
                "clientContent": {
                    "turns": [
                        {
                            "role": "user",
                            "parts": [{"text": "Hello! Please greet me to start the call."}]
                        }
                    ],
                    "turnComplete": True
                }
            }
            await gemini_ws.send(json.dumps(initial_greeting_message))

            # Start proxying
            client_to_gemini_task = asyncio.create_task(proxy_client_to_gemini(websocket, gemini_ws, conversation_id))
            gemini_to_client_task = asyncio.create_task(proxy_gemini_to_client(websocket, gemini_ws, conversation_id, session_tokens))

            done, pending = await asyncio.wait(
                [client_to_gemini_task, gemini_to_client_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    except WebSocketDisconnect:
        print(f"Client disconnected from WS for conversation {conversation_id}")
    except Exception as e:
        print(f"Error in voice websocket: {str(e)}")
    finally:
        try:
            await websocket.close()
        except:
            pass


async def proxy_client_to_gemini(client_ws: WebSocket, gemini_ws, conversation_id: str):
    """Reads audio and text chunks from the browser and sends to Gemini."""
    chunk_count = 0
    try:
        while True:
            # Expecting base64 string or JSON
            message = await client_ws.receive_text()
            try:
                data = json.loads(message)
                if "realtimeInput" in data:
                    # Forward structured realtime input
                    await gemini_ws.send(message)
                    chunk_count += 1
                elif "audioB64" in data:
                    # Construct realtimeInput if client just sends raw b64
                    payload = {
                        "realtimeInput": {
                            "audio": {
                                "data": data["audioB64"],
                                "mimeType": "audio/pcm;rate=16000"
                            }
                        }
                    }
                    await gemini_ws.send(json.dumps(payload))
                    chunk_count += 1
                elif "text" in data:
                    # Forward chat text as clientContent
                    text_payload = {
                        "clientContent": {
                            "turns": [
                                {
                                    "role": "user",
                                    "parts": [{"text": data["text"]}]
                                }
                            ],
                            "turnComplete": True
                        }
                    }
                    await gemini_ws.send(json.dumps(text_payload))
                    
                    # Also save to conversation store
                    try:
                        db_msg = ConversationMessage(role="user", content=data["text"])
                        await run_in_threadpool(conversation_store.append, conversation_id, db_msg)
                    except Exception as db_err:
                        print(f"Warning: Failed to save text message to DB: {db_err}")

            except json.JSONDecodeError:
                # If they just sent bare text, they might have sent base64 directly
                payload = {
                    "realtimeInput": {
                        "audio": {
                            "data": message,
                            "mimeType": "audio/pcm;rate=16000"
                        }
                    }
                }
                await gemini_ws.send(json.dumps(payload))
                chunk_count += 1

            if chunk_count > 0 and chunk_count % 50 == 1:
                print(f"Audio chunks sent to Gemini: {chunk_count}")

    except Exception as e:
        print(f"client_to_gemini exception (after {chunk_count} chunks):", str(e))


async def proxy_gemini_to_client(client_ws: WebSocket, gemini_ws, conversation_id: str, session_tokens: dict):
    """Reads from Gemini and routes audio/text to client, and handles tool calls."""
    response_count = 0
    set_usage_context(conversation_id, "web_voice")  # tool calls (query rewrite, embeddings) attribute usage to this call
    is_initial_greeting = True
    initial_greeting_buffer = []
    
    try:
        while True:
            should_end_call = False
            response_str = await gemini_ws.recv()
            response_count += 1
            data = json.loads(response_str)
            
            # Attempt to extract usageMetadata if provided by Gemini WS API
            usage = None
            if "usageMetadata" in data:
                usage = data["usageMetadata"]
            elif "serverContent" in data and "modelTurn" in data["serverContent"] and "usageMetadata" in data["serverContent"]["modelTurn"]:
                usage = data["serverContent"]["modelTurn"]["usageMetadata"]
                
            if usage:
                # one usage message arrives per generated turn: each is a billed event (text/audio split kept)
                await run_in_threadpool(record_usage, model=GEMINI_LIVE_MODEL, purpose="live_turn", channel="web_voice",
                                        session_id=conversation_id, raw=usage, **tokens_from_live(usage))

            if "serverContent" in data:
                server_content = data["serverContent"]
                
                # Signal interruption
                if server_content.get("interrupted"):
                    await client_ws.send_json({"interrupted": True})
                    
                # If the turn completes, add the current turn's output tokens to the session total and reset
                if server_content.get("turnComplete"):
                    if is_initial_greeting:
                        # Concatenate all audio bytes into one giant chunk to prevent browser GC stutter
                        combined_audio = b""
                        text_msgs = []
                        
                        for buffered_msg in initial_greeting_buffer:
                            if "audioB64" in buffered_msg:
                                combined_audio += base64.b64decode(buffered_msg["audioB64"])
                            if "text" in buffered_msg:
                                text_msgs.append(buffered_msg)
                        
                        # Flush all text messages first
                        for msg in text_msgs:
                            await client_ws.send_json(msg)
                            
                        # Send one massive audio chunk
                        if combined_audio:
                            await client_ws.send_json({"audioB64": base64.b64encode(combined_audio).decode("utf-8")})
                        
                        await client_ws.send_json({"turnComplete": True})
                        is_initial_greeting = False
                        initial_greeting_buffer.clear()
                    else:
                        await client_ws.send_json({"turnComplete": True})
                        

                if "modelTurn" in server_content:
                    parts = server_content["modelTurn"].get("parts", [])
                    for part in parts:
                        # Forward audio back to frontend
                        if "inlineData" in part:
                            # e.g., audio/pcm
                            msg = {"audioB64": part["inlineData"]["data"]}
                            if is_initial_greeting:
                                initial_greeting_buffer.append(msg)
                            else:
                                await client_ws.send_json(msg)
                        
                        # Forward text and log to DB if present
                        if "text" in part:
                            text_content = part["text"]
                            msg = {"text": text_content}
                            
                            # Optionally append to conversation store so it appears in text UI later
                            try:
                                db_msg = ConversationMessage(role="assistant", content=text_content)
                                await run_in_threadpool(conversation_store.append, conversation_id, db_msg)
                            except Exception as db_err:
                                print(f"Warning: Failed to save transcript to DB: {db_err}")
                            
                            if is_initial_greeting:
                                initial_greeting_buffer.append(msg)
                            else:
                                await client_ws.send_json(msg)

            elif "toolCall" in data:
                function_calls = data["toolCall"]["functionCalls"]
                responses = []

                for fc in function_calls:
                    f_name = fc["name"]
                    f_id = fc["id"]
                    f_args = fc.get("args", {})
                    
                    if f_name in ["save_collected_information", "send_verification_email", "write_to_chat"]:
                        f_args["session_id"] = conversation_id
                    
                    print(f"Executing tool {f_name} with {f_args}")
                    
                    result = ""
                    if f_name == "write_to_chat":
                        # Send this message immediately to the frontend
                        try:
                            message_text = f_args.get("message", "")
                            await client_ws.send_json({"chat_message": message_text})
                            
                            # Handle DB storage independently so failures don't cause the LLM to apologize 
                            # after the user has already seen the message.
                            try:
                                msg = ConversationMessage(role="assistant", content=message_text)
                                await run_in_threadpool(conversation_store.append, conversation_id, msg)
                            except Exception as db_err:
                                print(f"Warning: Failed to save chat message to DB: {db_err}")
                                
                            result = "Message successfully written to chat."
                        except Exception as e:
                            result = f"Error writing to chat: {str(e)}"
                    elif f_name == "end_call":
                        try:
                            await client_ws.send_json({"end_call": True})
                            result = "Call ended successfully. No further responses are needed."
                            should_end_call = True
                        except Exception as e:
                            result = f"Error ending call: {str(e)}"
                    elif f_name in LIVE_TOOLS_MAP:
                        try:
                            result = LIVE_TOOLS_MAP[f_name](f_args)
                        except Exception as e:
                            result = f"Error: {str(e)}"
                    else:
                        result = "Unknown tool."
                    
                    responses.append({
                        "id": f_id,
                        "name": f_name,
                        "response": {"result": str(result)}
                    })

                # Send tool response back to Gemini
                tool_resp = {
                    "toolResponse": {
                        "functionResponses": responses
                    }
                }
                await gemini_ws.send(json.dumps(tool_resp))
                
                if should_end_call:
                    print("Agent requested to end the call. Breaking server loop.")
                    break

    except Exception as e:
        print("gemini_to_client exception:", str(e))
    finally:
        pass
