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
    call_registered = False

    if caller_phone:
        from backend.app.services.call_restriction_service import call_restriction_service, DAILY_LIMIT_EXCEEDED_MESSAGE_MR
        check = call_restriction_service.check_daily_call_limit(caller_phone)
        if not check.allowed:
            logger.warning(f"WebSocket caller {caller_phone} exceeded daily limit ({check.connected_count}/{settings.MAX_CONNECTED_CALLS_PER_DAY})")
            await websocket.send_json({
                "type": "call_rejected",
                "reason": "daily_limit_exceeded",
                "message": DAILY_LIMIT_EXCEEDED_MESSAGE_MR,
                "limit": settings.MAX_CONNECTED_CALLS_PER_DAY,
                "current_count": check.connected_count
            })
            await websocket.close(code=4003, reason="Daily call limit exceeded")
            return

        async def _enforce_ws_timeout(call_id: str):
            try:
                logger.info(f"Call duration limit ({settings.MAX_CALL_DURATION_SECONDS}s) reached for WebSocket session {call_id}")
                await websocket.send_json({
                    "type": "call_terminated",
                    "reason": "max_duration_exceeded",
                    "message": "कॉलची कमाल वेळ मर्यादा (३ मिनिटे) संपली आहे."
                })
                await websocket.close(code=4008, reason="Max call duration exceeded")
            except Exception as e:
                logger.debug(f"Error closing timed-out websocket {call_id}: {e}")

        reg = await call_restriction_service.register_connected_call(
            provider_call_id=session.session_id,
            caller_phone=caller_phone,
            on_timeout_callback=_enforce_ws_timeout
        )
        if not reg.allowed:
            logger.warning(f"WebSocket caller {caller_phone} atomic register rejected ({reg.connected_count}/{settings.MAX_CONNECTED_CALLS_PER_DAY})")
            await websocket.send_json({
                "type": "call_rejected",
                "reason": "daily_limit_exceeded",
                "message": DAILY_LIMIT_EXCEEDED_MESSAGE_MR,
                "limit": settings.MAX_CONNECTED_CALLS_PER_DAY,
                "current_count": reg.connected_count
            })
            await websocket.close(code=4003, reason="Daily call limit exceeded")
            return

        call_registered = True

    # Send initial welcome and status
    await websocket.send_json({
        "type": "ready",
        "session_id": session.session_id,
        "language": session.current_language,
        "has_sarvam_key": settings.has_sarvam_key,
        "message": "Connected to CityLinc Multilingual Voice Agent."
    })

    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                event = json.loads(raw_data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON payload"})
                continue

            event_type = event.get("type", "")

            # 1. Ping / Keep-alive
            if event_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # 2. Interruption / Barge-in
            if event_type in ("interrupt", "user_speaking"):
                target_turn = event.get("turn_id")
                turn_id = session.interrupt(turn_id=target_turn)
                logger.info(f"Received user interruption for turn {target_turn}. Session advanced to: {turn_id}")
                await websocket.send_json({
                    "type": "interrupted",
                    "turn_id": turn_id,
                    "status": "listening"
                })
                continue

            # 3. Reset conversation context
            if event_type == "reset":
                session.reset()
                context_engine.clear_context(session.session_id)
                await websocket.send_json({
                    "type": "status",
                    "status": "idle",
                    "message": "Conversation context reset."
                })
                continue

            # 4. Incoming Voice Audio payload (Phase 4.4 Stage 2)
            if event_type == "audio":
                b64_audio = event.get("data", "")
                raw_format = event.get("format", "audio/webm")
                audio_format = raw_format.split(";")[0].strip().lower() if raw_format else "audio/webm"
                req_lang = event.get("language", "auto")
                duration_ms = event.get("duration_ms")

                if not b64_audio or len(b64_audio.strip()) < 16:
                    logger.warning("[Audio Ingest] Discarding empty or missing audio payload.")
                    await websocket.send_json({"type": "status", "status": "listening"})
                    continue

                try:
                    audio_bytes = base64.b64decode(b64_audio)
                except Exception as e:
                    logger.error(f"[Audio Ingest] Failed to decode base64 audio: {e}")
                    await websocket.send_json({"type": "error", "message": "Corrupted audio data"})
                    await websocket.send_json({"type": "status", "status": "listening"})
                    continue

                # Scope D: Backend STT Payload Validation
                is_valid, detected_mime, val_reason = validate_audio_payload(audio_bytes, audio_format, duration_ms)
                if not is_valid:
                    logger.warning(
                        f"[Audio Ingest Rejected] Reason: {val_reason} | Size: {len(audio_bytes)} bytes | "
                        f"Declared: {audio_format} | Duration: {duration_ms}ms"
                    )
                    if "unsupported_or_corrupt" in val_reason:
                        await websocket.send_json({"type": "error", "message": "Unsupported or corrupted audio format."})
                    await websocket.send_json({"type": "status", "status": "listening"})
                    continue

                logger.info(
                    f"[Audio Ingest Valid] Size: {len(audio_bytes)} bytes | Format: {detected_mime} (declared: {raw_format}) | "
                    f"Duration: {duration_ms}ms | Client Lang: {req_lang}"
                )

                # Notify client: Transcribing speech
                await websocket.send_json({
                    "type": "status",
                    "status": "thinking",
                    "stage": "stt",
                    "bytes_received": len(audio_bytes)
                })

                # Step 1: Sarvam STT Transcription with Latency Tracking (Scope E)
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
                    await websocket.send_json({"type": "info", "message": service_err})
                    await websocket.send_json({"type": "status", "status": "listening"})
                    continue

                stt_latency_ms = (time.perf_counter() - stt_start_time) * 1000
                transcript = stt_result.get("transcript", "").strip()
                sarvam_acoustic_lang = stt_result.get("language_code", "unknown")
                stt_error = stt_result.get("error")

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
                        await websocket.send_json({"type": "info", "message": api_err_msg})
                        await websocket.send_json({"type": "status", "status": "listening"})
                        continue

                    # 2. Ambient noise burst returning empty transcript -> Silent discard!
                    approx_dur = duration_ms if duration_ms is not None else 0
                    if (approx_dur > 0 and approx_dur < 500) or len(audio_bytes) < 1200:
                        logger.info(
                            f"[STT Empty Burst Discarded] Latency: {stt_latency_ms:.1f}ms | "
                            f"Duration: {approx_dur}ms | Bytes: {len(audio_bytes)} | Silent reset."
                        )
                        await websocket.send_json({"type": "status", "status": "listening"})
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
                        await websocket.send_json({"type": "info", "message": user_msg})
                    else:
                        logger.info(
                            f"[STT Empty Suppressed] Exceeded consecutive empty prompt threshold ({session.consecutive_empty_count}). "
                            f"Silent reset to avoid spam."
                        )

                    await websocket.send_json({"type": "status", "status": "listening"})
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
                det_result = language_detector.detect(
                    text=transcript,
                    stt_language=sarvam_acoustic_lang,
                    context_language=session.current_language,
                    user_preference=req_lang if req_lang in ("mr-IN", "hi-IN", "en-IN") else None
                )
                detected_lang = det_result.language

                logger.info(f"Speech transcribed: Length: {transcript_len} chars, Words: {word_count} -> Detected Lang: {detected_lang} (conf: {det_result.confidence:.2f}, reason: {det_result.reason})")

                is_lang_switched = (detected_lang != session.current_language)
                if is_lang_switched:
                    logger.info(f"Language switched dynamically: {session.current_language} -> {detected_lang}")

                # Send User transcript to UI
                await websocket.send_json({
                    "type": "transcript",
                    "role": "user",
                    "text": transcript,
                    "language": detected_lang,
                    "language_switched": is_lang_switched,
                    "previous_language": session.current_language
                })

                # Register message in session (updates session.current_language to detected_lang while preserving history)
                current_turn = session.add_user_message(transcript, language=detected_lang)

                # Check if user interrupted while we were processing
                if session.is_interrupted or current_turn != session.current_turn_id:
                    logger.info(f"Turn {current_turn} canceled due to interruption.")
                    continue

                # Step 2: Deterministic Transit Engine & Context Resolution
                if det_result.confidence < 0.4 and len(transcript) < 3:
                    clarification = {
                        "mr-IN": "मला स्पष्ट समजले नाही. कृपया बस क्रमांक, वेळ किंवा पासबद्दल पुन्हा सांगा.",
                        "hi-IN": "मुझे स्पष्ट समझ नहीं आया। कृपया बस नंबर, समय या पास के बारे में फिर से बताएं।",
                        "en-IN": "I could not hear clearly. Could you please repeat your query about routes, timings, or passes?"
                    }.get(detected_lang, "मला स्पष्ट समजले नाही. कृपया पुन्हा सांगा.")
                    reply = clarification
                else:
                    await websocket.send_json({"type": "status", "status": "thinking", "stage": "context"})
                    ctx_turn = context_engine.process_turn(session.session_id, transcript, language=detected_lang)
                    if ctx_turn["handled_deterministically"]:
                        reply = ctx_turn["response_text"]
                        speech_text = ctx_turn.get("speech_text") or format_text_for_speech(reply, lang=detected_lang)
                        if ctx_turn.get("map_action"):
                            await websocket.send_json({
                                "type": "map_action",
                                **ctx_turn["map_action"]
                            })
                    else:
                        await websocket.send_json({"type": "status", "status": "thinking", "stage": "llm"})
                        try:
                            reply = await llm_client.generate_response(session.history, language_code=detected_lang)
                            speech_text = format_text_for_speech(reply, lang=detected_lang)
                        except Exception as e:
                            logger.error(f"LLM generation error: {e}")
                            reply = "I apologize, I encountered an issue retrieving the information. Please ask again."
                            speech_text = reply

                # Check interruption after LLM
                if session.is_interrupted or current_turn != session.current_turn_id:
                    logger.info(f"Turn {current_turn} interrupted after LLM.")
                    continue

                # Send Assistant transcript to UI
                await websocket.send_json({
                    "type": "transcript",
                    "role": "assistant",
                    "text": reply,
                    "language": detected_lang
                })

                # Step 3: Sarvam TTS Speech Synthesis
                await websocket.send_json({"type": "status", "status": "thinking", "stage": "tts"})
                try:
                    tts_result = await tts_client.synthesize(
                        text=speech_text,
                        language_code=detected_lang
                    )
                    audio_output = tts_result.get("audio_base64", "")
                    out_format = tts_result.get("format", "audio/mpeg")
                    if out_format == "audio/mp3":
                        out_format = "audio/mpeg"

                    # Check interruption before sending audio
                    if session.is_interrupted or current_turn != session.current_turn_id:
                        logger.info(f"Turn {current_turn} canceled before sending audio.")
                        continue

                    session.add_assistant_message(reply, current_turn)

                    # Send generated speech audio to client
                    await websocket.send_json({"type": "status", "status": "speaking"})
                    await websocket.send_json({
                        "type": "audio",
                        "audio": audio_output,
                        "format": out_format,
                        "turn_id": current_turn,
                        "language": detected_lang
                    })

                except Exception as e:
                    logger.error(f"TTS synthesis error: {e}")
                    await websocket.send_json({"type": "error", "message": f"TTS synthesis error: {str(e)}"})
                    await websocket.send_json({"type": "status", "status": "idle"})

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

                current_turn = session.add_user_message(text_input, language=detected_lang)
                await websocket.send_json({
                    "type": "transcript",
                    "role": "user",
                    "text": text_input,
                    "language": detected_lang,
                    "language_switched": is_lang_switched,
                    "previous_language": session.current_language
                })

                await websocket.send_json({"type": "status", "status": "thinking"})
                ctx_turn = context_engine.process_turn(session.session_id, text_input, language=detected_lang)
                if ctx_turn["handled_deterministically"]:
                    reply = ctx_turn["response_text"]
                    speech_text = ctx_turn.get("speech_text") or format_text_for_speech(reply, lang=detected_lang)
                    if ctx_turn.get("map_action"):
                        await websocket.send_json({
                            "type": "map_action",
                            **ctx_turn["map_action"]
                        })
                else:
                    reply = await llm_client.generate_response(session.history, language_code=detected_lang)
                    speech_text = format_text_for_speech(reply, lang=detected_lang)

                if session.is_interrupted or current_turn != session.current_turn_id:
                    continue

                await websocket.send_json({
                    "type": "transcript",
                    "role": "assistant",
                    "text": reply,
                    "language": detected_lang
                })

                tts_result = await tts_client.synthesize(text=speech_text, language_code=detected_lang)
                session.add_assistant_message(reply, current_turn)

                tts_format = tts_result.get("format", "audio/mpeg")
                if tts_format == "audio/mp3":
                    tts_format = "audio/mpeg"

                await websocket.send_json({"type": "status", "status": "speaking"})
                await websocket.send_json({
                    "type": "audio",
                    "audio": tts_result.get("audio_base64", ""),
                    "format": tts_format,
                    "turn_id": current_turn,
                    "language": detected_lang
                })

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {session.session_id}")
    except Exception as e:
        logger.error(f"WebSocket exception: {e}", exc_info=True)
    finally:
        if caller_phone and call_registered:
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

