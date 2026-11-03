"""
WebSocket routes for real-time bidirectional voice conversation.
Handles microphone audio streaming, Sarvam STT, LLM context, Sarvam TTS, and barge-in / interruption.
"""
import asyncio
import base64
import json
import logging
import time
from typing import Optional, Tuple
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from backend.app.core.config import settings
from backend.app.integrations.sarvam.stt import stt_client
from backend.app.integrations.sarvam.tts import tts_client
from backend.app.integrations.llm.client import llm_client
from backend.app.orchestrator.conversation_manager import conversation_manager
from backend.app.services.language_detector import language_detector
from backend.app.services.conversation_context import context_engine
from backend.app.services.number_formatter import format_text_for_speech

logger = logging.getLogger("citylinc.ws")

router = APIRouter(tags=["WebSocket"])

def validate_audio_payload(audio_bytes: bytes, declared_format: str, duration_ms: Optional[int] = None) -> Tuple[bool, str, str]:
    """
    Validates audio container structure, payload bounds, and duration limits (Phase 4.4 Scope D).
    Returns (is_valid, detected_mime, failure_reason).
    """
    if not audio_bytes or len(audio_bytes) < 200:
        return False, "unknown", f"payload_too_small_{len(audio_bytes) if audio_bytes else 0}_bytes"

    if len(audio_bytes) > 5 * 1024 * 1024:
        return False, "unknown", f"payload_exceeds_max_size_{len(audio_bytes)}_bytes"

    # Inspect magic bytes for container verification
    detected_mime = None
    if len(audio_bytes) >= 4:
        if audio_bytes[:4] == b"\x1a\x45\xdf\xa3":
            detected_mime = "audio/webm"
        elif audio_bytes[:4] == b"RIFF" and len(audio_bytes) >= 12 and audio_bytes[8:12] == b"WAVE":
            detected_mime = "audio/wav"
        elif audio_bytes[:4] == b"OggS":
            detected_mime = "audio/ogg"
        elif audio_bytes[:3] == b"ID3" or (len(audio_bytes) >= 2 and audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
            detected_mime = "audio/mpeg"
        elif len(audio_bytes) >= 8 and audio_bytes[4:8] == b"ftyp":
            detected_mime = "audio/mp4"

    clean_decl = (declared_format or "").split(";")[0].strip().lower()
    allowed_mimes = {"audio/webm", "audio/wav", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/aac", "audio/x-wav"}

    effective_mime = detected_mime or (clean_decl if clean_decl in allowed_mimes else None)
    if not effective_mime:
        return False, "unknown", f"unsupported_or_corrupt_container_{audio_bytes[:4].hex()}"

    # Duration limits if provided by client
    if duration_ms is not None:
        try:
            d_ms = int(duration_ms)
            if d_ms < 180 and len(audio_bytes) < 1500:
                return False, effective_mime, f"duration_too_short_{d_ms}ms"
            if d_ms > 25000:
                return False, effective_mime, f"duration_too_long_{d_ms}ms"
        except (ValueError, TypeError):
            pass

    return True, effective_mime, "valid"

@router.websocket("/ws/conversation")
async def websocket_conversation_endpoint(websocket: WebSocket):
    """
    Real-time bidirectional WebSocket voice endpoint.
    Communicates with browser frontend using JSON events.
    Enforces daily call limits and 180s duration if caller phone is identified.
    """
    await websocket.accept()
    session = conversation_manager.get_or_create_session()
    logger.info(f"WebSocket client connected. Session: {session.session_id}")

    # Check caller phone if provided for call limit and duration restrictions
    caller_phone = websocket.query_params.get("caller_id") or websocket.query_params.get("phone") or websocket.headers.get("x-caller-id")
    effective_phone = caller_phone or f"+9199990{abs(hash(session.session_id)) % 100000:05d}"
    call_registered = False
    call_start_time = time.time()
    warning_sent = False
    is_ws_closed = False

    async def safe_send_json(payload: dict) -> bool:
        nonlocal is_ws_closed
        if is_ws_closed:
            return False
        try:
            if hasattr(websocket, "client_state") and websocket.client_state != WebSocketState.CONNECTED:
                is_ws_closed = True
                return False
        except Exception:
            pass
        try:
            await websocket.send_json(payload)
            return True
        except Exception as exc:
            is_ws_closed = True
            logger.debug(f"[WebSocket] safe_send_json aborted / closed: {exc}")
            return False

    async def safe_close(code: int = 1000, reason: str = ""):
        nonlocal is_ws_closed
        if is_ws_closed:
            return
        is_ws_closed = True
        session.is_interrupted = True
        try:
            if hasattr(websocket, "client_state") and websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=code, reason=reason)
            elif not hasattr(websocket, "client_state"):
                await websocket.close(code=code, reason=reason)
        except Exception as ce:
            logger.debug(f"[WebSocket] safe_close error (already closed): {ce}")

    async def _send_graceful_closing_warning(call_id: str):
        nonlocal warning_sent
        if warning_sent or session.is_interrupted or is_ws_closed:
            return
        warning_sent = True
        curr_lang = session.current_language
        closing_msgs = {
            "mr-IN": "आपल्या कॉलची ३ मिनिटांची वेळ मर्यादा लवकरच संपत आहे. सिटीलिंकचा वापर केल्याबद्दल धन्यवाद, आपला दिवस सुखकर जावो!",
            "hi-IN": "आपके कॉल की 3 मिनट की समय सीमा जल्द ही समाप्त हो रही है। सिटीलिंक का उपयोग करने के लिए धन्यवाद, आपका दिन शुभ हो!",
            "en-IN": "Your 3-minute call limit is concluding shortly. Thank you for calling CityLinc, have a wonderful day!"
        }
        closing_text = closing_msgs.get(curr_lang, closing_msgs["mr-IN"])
        logger.info(f"[GracefulClosing] Sending 20s pre-timeout notification for session {call_id} in {curr_lang}")
        try:
            sent = await safe_send_json({
                "type": "call_warning",
                "reason": "duration_approaching_limit",
                "lead_seconds": 20,
                "text": closing_text,
                "language": curr_lang
            })
            if not sent or is_ws_closed:
                return
            tts_res = await tts_client.synthesize(text=closing_text, language_code=curr_lang)
            audio_b64 = tts_res.get("audio_base64")
            if audio_b64 and not is_ws_closed:
                await safe_send_json({"type": "status", "status": "speaking"})
                await safe_send_json({
                    "type": "audio",
                    "audio": audio_b64,
                    "format": tts_res.get("format", "audio/mpeg"),
                    "language": curr_lang,
                    "is_closing": True
                })
        except Exception as ce:
            logger.debug(f"[GracefulClosing] Error dispatching warning to websocket {call_id}: {ce}")

    async def _enforce_ws_timeout(call_id: str):
        nonlocal is_ws_closed
        try:
            logger.info(f"Call duration limit ({settings.MAX_CALL_DURATION_SECONDS}s) reached for WebSocket session {call_id}")
            term_msgs = {
                "mr-IN": "कॉलची कमाल वेळ मर्यादा (३ मिनिटे) संपली आहे.",
                "hi-IN": "कॉल की अधिकतम समय सीमा (3 मिनट) समाप्त हो गई है।",
                "en-IN": "Call maximum duration limit (3 minutes) reached."
            }
            term_msg = term_msgs.get(session.current_language, term_msgs["mr-IN"])
            await safe_send_json({
                "type": "call_terminated",
                "reason": "max_duration_exceeded",
                "message": term_msg
            })
            await safe_close(code=4008, reason="Max call duration exceeded")
        except Exception as e:
            logger.debug(f"Error closing timed-out websocket {call_id}: {e}")

    from backend.app.services.call_restriction_service import call_restriction_service, DAILY_LIMIT_EXCEEDED_MESSAGE_MR

    # Daily call limit validation if explicit phone provided
    if caller_phone:
        check = call_restriction_service.check_daily_call_limit(caller_phone)
        if not check.allowed:
            logger.warning(f"WebSocket caller {caller_phone} exceeded daily limit ({check.connected_count}/{settings.MAX_CONNECTED_CALLS_PER_DAY})")
            await safe_send_json({
                "type": "call_rejected",
                "reason": "daily_limit_exceeded",
                "message": DAILY_LIMIT_EXCEEDED_MESSAGE_MR,
                "limit": settings.MAX_CONNECTED_CALLS_PER_DAY,
                "current_count": check.connected_count
            })
            await safe_close(code=4003, reason="Daily call limit exceeded")
            return

    reg = await call_restriction_service.register_connected_call(
        provider_call_id=session.session_id,
        caller_phone=effective_phone,
        on_timeout_callback=_enforce_ws_timeout,
        on_warning_callback=_send_graceful_closing_warning,
        warning_lead_seconds=20
    )
    if not reg.allowed:
        logger.warning(f"WebSocket caller {effective_phone} atomic register rejected ({reg.connected_count}/{settings.MAX_CONNECTED_CALLS_PER_DAY})")
        await safe_send_json({
            "type": "call_rejected",
            "reason": "daily_limit_exceeded",
            "message": DAILY_LIMIT_EXCEEDED_MESSAGE_MR,
            "limit": settings.MAX_CONNECTED_CALLS_PER_DAY,
            "current_count": reg.connected_count
        })
        await safe_close(code=4003, reason="Daily call limit exceeded")
        return

    call_registered = True

    # Send initial welcome and status
    await safe_send_json({
        "type": "ready",
        "session_id": session.session_id,
        "language": session.current_language,
        "has_sarvam_key": settings.has_sarvam_key,
        "message": "Connected to CityLinc Multilingual Voice Agent."
    })

    try:
        while True:
            if is_ws_closed:
                break
            raw_data = await websocket.receive_text()
            if is_ws_closed:
                break
            try:
                event = json.loads(raw_data)
            except json.JSONDecodeError:
                await safe_send_json({"type": "error", "message": "Invalid JSON payload"})
                continue

            event_type = event.get("type", "")

            # 1. Ping / Keep-alive
            if event_type == "ping":
                await safe_send_json({"type": "pong"})
                continue

            # 2. Interruption / Barge-in
            if event_type in ("interrupt", "user_speaking"):
                target_turn = event.get("turn_id")
                turn_id = session.interrupt(turn_id=target_turn)
                logger.info(f"Received user interruption for turn {target_turn}. Session advanced to: {turn_id}")
                await safe_send_json({
                    "type": "interrupted",
                    "turn_id": turn_id,
                    "status": "listening"
                })
                continue

            # 3. Reset conversation context
            if event_type == "reset":
                session.reset()
                context_engine.clear_context(session.session_id)
                await safe_send_json({
                    "type": "status",
                    "status": "idle",
                    "message": "Conversation context reset."
                })
                continue

            # 3B. Audio Playback Completion Confirmation from Client
            if event_type in ("audio_playback_completed", "playback_completed"):
                turn_id = event.get("turn_id")
                logger.info(f"[AudioPlayback] Client confirmed audio playback completed for turn {turn_id}")
                continue

            # 4. Incoming Voice Audio payload (Phase 4.4 Stage 2)
            if event_type == "audio":
                if is_ws_closed:
                    continue
                b64_audio = event.get("data", "")
                raw_format = event.get("format", "audio/webm")
                audio_format = raw_format.split(";")[0].strip().lower() if raw_format else "audio/webm"
                req_lang = event.get("language", "auto")
                duration_ms = event.get("duration_ms")

                if not b64_audio or len(b64_audio.strip()) < 16:
                    logger.warning("[Audio Ingest] Discarding empty or missing audio payload.")
                    await safe_send_json({"type": "status", "status": "listening"})
                    continue

                try:
                    audio_bytes = base64.b64decode(b64_audio)
                except Exception as e:
                    logger.error(f"[Audio Ingest] Failed to decode base64 audio: {e}")
                    await safe_send_json({"type": "error", "message": "Corrupted audio data"})
                    await safe_send_json({"type": "status", "status": "listening"})
                    continue

                # Scope D: Backend STT Payload Validation
                is_valid, detected_mime, val_reason = validate_audio_payload(audio_bytes, audio_format, duration_ms)
                if not is_valid:
                    logger.warning(
                        f"[Audio Ingest Rejected] Reason: {val_reason} | Size: {len(audio_bytes)} bytes | "
                        f"Declared: {audio_format} | Duration: {duration_ms}ms"
                    )
                    if "unsupported_or_corrupt" in val_reason:
                        await safe_send_json({"type": "error", "message": "Unsupported or corrupted audio format."})
                    await safe_send_json({"type": "status", "status": "listening"})
                    continue

                logger.info(
                    f"[Audio Ingest Valid] Size: {len(audio_bytes)} bytes | Format: {detected_mime} (declared: {raw_format}) | "
                    f"Duration: {duration_ms}ms | Client Lang: {req_lang}"
                )

                # Atomically start a new user turn (Phase 4.8 P0 Stale Response Prevention)
                current_turn = session.start_user_turn()

                # Notify client: Transcribing speech
                await safe_send_json({
                    "type": "status",
                    "status": "thinking",
                    "stage": "stt",
                    "turn_id": current_turn,
                    "bytes_received": len(audio_bytes)
                })

                # Step 1: Sarvam STT Transcription with Latency Tracking (Scope E)
                if session.has_explicit_language_preference:
                    stt_req_lang = session.current_language
                else:
                    stt_req_lang = req_lang if req_lang in ("mr-IN", "hi-IN", "en-IN") else "unknown"
                stt_start_time = time.perf_counter()
                try:
                    stt_result = await stt_client.transcribe(
                        audio_bytes=audio_bytes,
                        language_code=stt_req_lang,
                        mime_type=detected_mime
                    )
                except Exception as e:
                    stt_latency_ms = (time.perf_counter() - stt_start_time) * 1000
                    logger.error(f"[STT Error] Latency: {stt_latency_ms:.1f}ms | Error: {e}")
                    service_err = {
                        "mr-IN": "सेवा तात्पुरती अनुपलब्ध आहे. कृपया काही सेकंदांनंतर पुन्हा प्रयत्न करा.",
                        "hi-IN": "सेवा अस्थायी रूप से अनुपलब्ध है। कृपया थोड़ी देर बाद पुनः प्रयास करें।",
                        "en-IN": "Service temporarily unavailable. Please try again shortly."
                    }.get(session.current_language, "सेवा तात्पुरती अनुपलब्ध आहे. कृपया काही सेकंदांनंतर पुन्हा प्रयत्न करा.")
                    await safe_send_json({"type": "info", "message": service_err})
                    await safe_send_json({"type": "status", "status": "listening"})
                    continue

                stt_latency_ms = (time.perf_counter() - stt_start_time) * 1000
                transcript = stt_result.get("transcript", "").strip()
                sarvam_acoustic_lang = stt_result.get("language_code", "unknown")
                stt_error = stt_result.get("error")

                # Verify turn validity before processing (reject stale turns overtaken during async STT or closed ws)
                if is_ws_closed or not session.is_turn_valid(current_turn):
                    logger.info(f"Turn {current_turn} superseded or interrupted during STT (ws_closed={is_ws_closed}). Discarding result.")
                    continue

                # Scope C: Empty STT Transcript & Error Handling
                if not transcript or stt_error:
                    curr_lang = session.current_language

                    # 1. Cloud API or timeout errors
                    if stt_error in ("timeout", "api_error"):
                        logger.warning(f"[STT Failure] Error: {stt_error} | Latency: {stt_latency_ms:.1f}ms")
                        api_err_msg = {
                            "mr-IN": "सेवा तात्पुरती अनुपलब्ध आहे. कृपया काही सेकंदांनंतर पुन्हा प्रयत्न करा.",
                            "hi-IN": "सेवा अस्थायी रूप से अनुपलब्ध है। कृपया थोड़ी देर बाद पुनः प्रयास करें।",
                            "en-IN": "Service temporarily unavailable. Please try again shortly."
                        }.get(curr_lang, "सेवा तात्पुरती अनुपलब्ध आहे. कृपया काही सेकंदांनंतर पुन्हा प्रयत्न करा.")
                        await safe_send_json({"type": "info", "message": api_err_msg})
                        await safe_send_json({"type": "status", "status": "listening"})
                        continue

                    # 2. Ambient noise burst returning empty transcript -> Silent discard!
                    approx_dur = duration_ms if duration_ms is not None else 0
                    if (approx_dur > 0 and approx_dur < 500) or len(audio_bytes) < 1200:
                        logger.info(
                            f"[STT Empty Burst Discarded] Latency: {stt_latency_ms:.1f}ms | "
                            f"Duration: {approx_dur}ms | Bytes: {len(audio_bytes)} | Silent reset."
                        )
                        await safe_send_json({"type": "status", "status": "listening"})
                        continue

                    # 3. User spoke genuine duration but speech was unclear -> Controlled polite retry (max 2)
                    if session.consecutive_empty_count < 2:
                        session.consecutive_empty_count += 1
                        user_msg = (
                            stt_result.get(f"user_message_{curr_lang[:2]}") or
                            stt_result.get("user_message_mr") or
                            {
                                "mr-IN": "तुमचा आवाज स्पष्ट ऐकू आला नाही. कृपया पुन्हा बोला.",
                                "hi-IN": "आपकी आवाज़ साफ़ सुनाई नहीं दी। कृपया दोबारा बोलें।",
                                "en-IN": "I couldn't hear you clearly. Please try again."
                            }.get(curr_lang, "तुमचा आवाज स्पष्ट ऐकू आला नाही. कृपया पुन्हा बोला.")
                        )
                        logger.info(
                            f"[STT Empty User Speech] Prompting user to repeat (attempt {session.consecutive_empty_count}/2) | "
                            f"Latency: {stt_latency_ms:.1f}ms | Duration: {approx_dur}ms"
                        )
                        await safe_send_json({"type": "info", "message": user_msg})
                    else:
                        logger.info(
                            f"[STT Empty Suppressed] Exceeded consecutive empty prompt threshold ({session.consecutive_empty_count}). "
                            f"Silent reset to avoid spam."
                        )

                    await safe_send_json({"type": "status", "status": "listening"})
                    continue

                # Valid transcript received: reset consecutive empty counter
                session.consecutive_empty_count = 0
                transcript_len = len(transcript)
                word_count = len(transcript.split())
                logger.info(
                    f"[STT Transcribed] Length: {transcript_len} chars, Words: {word_count} | Latency: {stt_latency_ms:.1f}ms | "
                    f"Acoustic Lang: {sarvam_acoustic_lang} | Bytes: {len(audio_bytes)}"
                )

                # Multi-layered language detection combining acoustic STT + lexical analysis + context
                eff_pref = session.current_language if session.has_explicit_language_preference else (req_lang if req_lang in ("mr-IN", "hi-IN", "en-IN") else None)
                det_result = language_detector.detect(
                    text=transcript,
                    stt_language=sarvam_acoustic_lang,
                    context_language=session.current_language,
                    user_preference=eff_pref
                )
                
                is_explicit_cmd = bool(det_result.reason and "Explicit user command to switch" in det_result.reason)
                if session.has_explicit_language_preference and not is_explicit_cmd:
                    # Explicit session preference is locked; do not allow acoustic/lexical noise to override
                    detected_lang = session.current_language
                    is_lang_switched = False
                else:
                    detected_lang = det_result.language
                    is_lang_switched = (detected_lang != session.current_language)
                    if is_lang_switched or is_explicit_cmd:
                        logger.info(f"Language switched: {session.current_language} -> {detected_lang} (explicit={is_explicit_cmd})")
                        session.set_language(detected_lang, explicit=is_explicit_cmd)

                # Send User transcript to UI
                await safe_send_json({
                    "type": "transcript",
                    "role": "user",
                    "text": transcript,
                    "turn_id": current_turn,
                    "language": detected_lang,
                    "language_switched": is_lang_switched,
                    "previous_language": session.current_language
                })

                # Register message in session
                session.add_user_message(transcript, language=detected_lang, turn_id=current_turn)

                # Check if user interrupted while we were processing or ws closed
                if is_ws_closed or not session.is_turn_valid(current_turn):
                    logger.info(f"Turn {current_turn} canceled due to interruption or ws closure.")
                    continue

                # Step 2: Deterministic Transit Engine & Context Resolution
                if det_result.confidence < 0.4 and len(transcript) < 3:
                    clarification = {
                        "mr-IN": "मला स्पष्ट समजले नाही. कृपया बस क्रमांक, वेळ किंवा पासबद्दल पुन्हा सांगा.",
                        "hi-IN": "मुझे स्पष्ट समझ नहीं आया। कृपया बस नंबर, समय या पास के बारे में फिर से बताएं।",
                        "en-IN": "I could not hear clearly. Could you please repeat your query about routes, timings, or passes?"
                    }.get(detected_lang, "मला स्पष्ट समजले नाही. कृपया पुन्हा सांगा.")
                    reply = clarification
                    speech_text = clarification
                else:
                    if is_ws_closed:
                        continue
                    await safe_send_json({"type": "status", "status": "thinking", "stage": "context", "turn_id": current_turn})
                    ctx_turn = context_engine.process_turn(session.session_id, transcript, language=detected_lang, turn_id=current_turn)
                    if ctx_turn.get("new_language"):
                        detected_lang = ctx_turn["new_language"]
                        session.set_language(detected_lang, explicit=True)

                    response_lang = ctx_turn.get("language") or detected_lang

                    if ctx_turn["handled_deterministically"]:
                        reply = ctx_turn["response_text"]
                        speech_text = ctx_turn.get("speech_text") or format_text_for_speech(reply, lang=response_lang)
                        if ctx_turn.get("map_action"):
                            await safe_send_json({
                                "type": "map_action",
                                **ctx_turn["map_action"]
                            })
                    else:
                        if is_ws_closed:
                            continue
                        await safe_send_json({"type": "status", "status": "thinking", "stage": "llm", "turn_id": current_turn})
                        try:
                            reply = await llm_client.generate_response(session.history, language_code=response_lang)
                            speech_text = format_text_for_speech(reply, lang=response_lang)
                        except Exception as e:
                            logger.error(f"LLM generation error: {e}")
                            reply = "I apologize, I encountered an issue retrieving the information. Please ask again."
                            speech_text = reply

                # Check interruption after context/LLM or ws closed
                if is_ws_closed or not session.is_turn_valid(current_turn):
                    logger.info(f"Turn {current_turn} interrupted after response generation or ws closed.")
                    continue

                # Check if approaching call duration limit (>= 155s of 180s)
                elapsed_call_time = time.time() - call_start_time
                if elapsed_call_time >= 155 and not warning_sent:
                    warning_sent = True
                    closing_suffixes = {
                        "mr-IN": " (आपल्या कॉलची ३ मिनिटांची वेळ मर्यादा संपत आहे. सिटीलिंकचा वापर केल्याबद्दल धन्यवाद!)",
                        "hi-IN": " (आपके कॉल की 3 मिनट की समय सीमा समाप्त हो रही है। सिटीलिंक का उपयोग करने के लिए धन्यवाद!)",
                        "en-IN": " (Your 3-minute call limit is concluding. Thank you for calling CityLinc!)"
                    }
                    closing_suffix = closing_suffixes.get(response_lang, closing_suffixes["mr-IN"])
                    reply += closing_suffix
                    speech_text = format_text_for_speech(reply, lang=response_lang)

                # Send Assistant transcript to UI
                await safe_send_json({
                    "type": "transcript",
                    "role": "assistant",
                    "text": reply,
                    "turn_id": current_turn,
                    "language": response_lang
                })

                # Step 3: Sarvam TTS Speech Synthesis
                if is_ws_closed:
                    continue
                await safe_send_json({"type": "status", "status": "thinking", "stage": "tts", "turn_id": current_turn})
                try:
                    if is_ws_closed or not session.is_turn_valid(current_turn):
                        logger.info(f"Turn {current_turn} canceled before TTS (ws_closed={is_ws_closed}).")
                        continue

                    tts_result = await tts_client.synthesize(
                        text=speech_text,
                        language_code=response_lang
                    )
                    audio_output = tts_result.get("audio_base64", "")
                    out_format = tts_result.get("format", "audio/mpeg")
                    if out_format == "audio/mp3":
                        out_format = "audio/mpeg"

                    # Check interruption before sending audio
                    if is_ws_closed or not session.is_turn_valid(current_turn):
                        logger.info(f"Turn {current_turn} canceled before sending audio (ws_closed={is_ws_closed}).")
                        continue

                    session.add_assistant_message(reply, current_turn)

                    # Send generated speech audio to client
                    await safe_send_json({"type": "status", "status": "speaking", "turn_id": current_turn})
                    await safe_send_json({
                        "type": "audio",
                        "audio": audio_output,
                        "format": out_format,
                        "turn_id": current_turn,
                        "language": detected_lang
                    })

                except Exception as e:
                    logger.error(f"TTS synthesis error: {e}")
                    await safe_send_json({"type": "error", "message": f"TTS synthesis error: {str(e)}"})
                    await safe_send_json({"type": "status", "status": "idle"})

            # 5. Direct Text Input
            elif event_type == "text":
                text_input = event.get("text", "").strip()
                req_lang = event.get("language", "auto")

                if not text_input:
                    continue

                # Multi-layered language detection for typed text (handles Roman Marathi, Hinglish, Devanagari, English)
                det_result = language_detector.detect(
                    text=text_input,
                    context_language=session.current_language,
                    user_preference=req_lang if req_lang in ("mr-IN", "hi-IN", "en-IN") else None
                )
                detected_lang = det_result.language

                is_lang_switched = (detected_lang != session.current_language)
                if is_lang_switched:
                    logger.info(f"Text language switched: {session.current_language} -> {detected_lang}")

                current_turn = session.start_user_turn()
                if is_lang_switched:
                    session.set_language(detected_lang)

                session.add_user_message(text_input, language=detected_lang, turn_id=current_turn)
                await safe_send_json({
                    "type": "transcript",
                    "role": "user",
                    "text": text_input,
                    "turn_id": current_turn,
                    "language": detected_lang,
                    "language_switched": is_lang_switched,
                    "previous_language": session.current_language
                })

                if is_ws_closed or not session.is_turn_valid(current_turn):
                    continue

                await safe_send_json({"type": "status", "status": "thinking", "turn_id": current_turn})
                ctx_turn = context_engine.process_turn(session.session_id, text_input, language=detected_lang, turn_id=current_turn)
                if ctx_turn.get("new_language"):
                    detected_lang = ctx_turn["new_language"]
                    session.set_language(detected_lang)

                if ctx_turn["handled_deterministically"]:
                    reply = ctx_turn["response_text"]
                    speech_text = ctx_turn.get("speech_text") or format_text_for_speech(reply, lang=detected_lang)
                    if ctx_turn.get("map_action"):
                        await safe_send_json({
                            "type": "map_action",
                            **ctx_turn["map_action"]
                        })
                else:
                    if is_ws_closed:
                        continue
                    reply = await llm_client.generate_response(session.history, language_code=detected_lang)
                    speech_text = format_text_for_speech(reply, lang=detected_lang)

                if is_ws_closed or not session.is_turn_valid(current_turn):
                    continue

                # Check if approaching call duration limit (>= 155s of 180s)
                elapsed_call_time = time.time() - call_start_time
                if elapsed_call_time >= 155 and not warning_sent:
                    warning_sent = True
                    closing_suffixes = {
                        "mr-IN": " (आपल्या कॉलची ३ मिनिटांची वेळ मर्यादा संपत आहे. सिटीलिंकचा वापर केल्याबद्दल धन्यवाद!)",
                        "hi-IN": " (आपके कॉल की 3 मिनट की समय सीमा समाप्त हो रही है। सिटीलिंक का उपयोग करने के लिए धन्यवाद!)",
                        "en-IN": " (Your 3-minute call limit is concluding. Thank you for calling CityLinc!)"
                    }
                    closing_suffix = closing_suffixes.get(detected_lang, closing_suffixes["mr-IN"])
                    reply += closing_suffix
                    speech_text = format_text_for_speech(reply, lang=detected_lang)

                await safe_send_json({
                    "type": "transcript",
                    "role": "assistant",
                    "text": reply,
                    "turn_id": current_turn,
                    "language": detected_lang
                })

                if is_ws_closed or not session.is_turn_valid(current_turn):
                    continue

                tts_result = await tts_client.synthesize(text=speech_text, language_code=detected_lang)
                if is_ws_closed or not session.is_turn_valid(current_turn):
                    continue

                session.add_assistant_message(reply, current_turn)

                tts_format = tts_result.get("format", "audio/mpeg")
                if tts_format == "audio/mp3":
                    tts_format = "audio/mpeg"

                await safe_send_json({"type": "status", "status": "speaking", "turn_id": current_turn})
                await safe_send_json({
                    "type": "audio",
                    "audio": tts_result.get("audio_base64", ""),
                    "format": tts_format,
                    "turn_id": current_turn,
                    "language": detected_lang
                })

    except WebSocketDisconnect:
        is_ws_closed = True
        logger.info(f"WebSocket client disconnected: {session.session_id}")
    except Exception as e:
        is_ws_closed = True
        logger.error(f"WebSocket exception: {e}", exc_info=True)
    finally:
        is_ws_closed = True
        try:
            from backend.app.services.call_restriction_service import call_restriction_service
            call_restriction_service.cancel_call_timeout(session.session_id)
        except Exception:
            pass

        if call_registered:
            try:
                from backend.app.services.call_restriction_service import call_restriction_service
                call_restriction_service.register_call_disconnect(
                    provider_call_id=session.session_id,
                    termination_reason="client_disconnect"
                )
            except Exception as e:
                logger.error(f"Error registering disconnect for session {session.session_id}: {e}")

        conversation_manager.remove_session(session.session_id)
        context_engine.clear_context(session.session_id)

