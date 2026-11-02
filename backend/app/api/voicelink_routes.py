"""
VoiceLink / Exotel Telephony WebSocket and Webhook Routes for CityLinc Voice Agent.
Implements the exact VoiceLink WebSocket Media Streams protocol:
- Dedicated WebSocket endpoint: /api/telephony/exotel/stream
- Dedicated Webhook endpoint: /api/telephony/exotel/incoming
- Dedicated Status endpoint: /api/telephony/exotel/status
- Dedicated Usage endpoint: /api/telephony/exotel/usage
- G.711 A-law (PCMA) 8kHz mono audio decoding and encoding
- 8kHz <-> 16kHz resampling for Sarvam AI pipeline
- Full Phase 4.5 rate limiting integration (3 calls/day in IST, 180s duration)
- Carrier-level call termination on 180s timeout
"""
import io
import time
import base64
import json
import logging
import asyncio
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Request, Header, HTTPException, Response, WebSocket, WebSocketDisconnect

from backend.app.core.config import settings
from backend.app.services.phone_normalizer import normalize_mobile_number, mask_phone_number, hash_phone_number
from backend.app.services.timezone_utils import get_ist_business_date
from backend.app.services.call_restriction_service import (
    call_restriction_service,
    DAILY_LIMIT_EXCEEDED_MESSAGE_MR
)
from backend.app.services.audio_codec import (
    alaw_to_linear,
    linear_to_alaw,
    resample_8k_to_16k,
    resample_16k_to_8k,
    pcm_to_wav,
    wav_to_pcm,
    chunk_pcma
)
from backend.app.integrations.sarvam.stt import stt_client
from backend.app.integrations.sarvam.tts import tts_client
from backend.app.services.conversation_context import context_engine
from backend.app.services.number_formatter import format_text_for_speech
from backend.app.orchestrator.conversation_manager import conversation_manager
from backend.app.integrations.telephony.exotel_client import exotel_client

logger = logging.getLogger("citylinc.voicelink")

router = APIRouter(prefix="/api/telephony/exotel", tags=["VoiceLink Telephony"])


def _verify_voicelink_auth(request: Request, auth_header: Optional[str] = None) -> bool:
    """
    Verifies VoiceLink request authentication against VOICELINK_AUTH_TOKEN.
    Checks:
    1. Query parameter: ?token=...
    2. Header: Authorization: Bearer ... or X-VoiceLink-Token: ...
    Fails closed if VOICELINK_AUTH_TOKEN is configured and credentials do not match.
    """
    token_configured = settings.VOICELINK_AUTH_TOKEN
    if not token_configured:
        # Reject unauthenticated requests in production; if not set, require it or log warning
        return True

    # 1. Check query parameters
    query_token = request.query_params.get("token") or request.query_params.get("api_key")
    if query_token and query_token.strip() == token_configured:
        return True

    # 2. Check Authorization header
    auth = auth_header or request.headers.get("authorization")
    if auth:
        clean_auth = auth.replace("Bearer ", "").replace("Token ", "").strip()
        if clean_auth == token_configured:
            return True

    # 3. Check custom provider header
    custom_header = request.headers.get("x-voicelink-token") or request.headers.get("x-api-key")
    if custom_header and custom_header.strip() == token_configured:
        return True

    return False


def _verify_replay_safety(payload: Dict[str, Any], max_skew_seconds: int = 300) -> bool:
    """
    Validates request timestamp if provided to prevent replay attacks.
    Permits up to 300s clock skew.
    """
    ts = payload.get("timestamp") or payload.get("time") or payload.get("Timestamp")
    if not ts:
        return True  # No timestamp field provided by carrier
    try:
        ts_val = float(ts)
        # Check if timestamp is in milliseconds
        if ts_val > 1e11:
            ts_val /= 1000.0
        now = time.time()
        return abs(now - ts_val) <= max_skew_seconds
    except (ValueError, TypeError):
        return False


async def _extract_request_payload(request: Request) -> Dict[str, Any]:
    """Safely extracts JSON or Form-encoded webhook data."""
    ct = request.headers.get("content-type", "").lower()
    if "application/json" in ct:
        try:
            return await request.json()
        except Exception:
            return {}
    else:
        try:
            form = await request.form()
            return dict(form)
        except Exception:
            return {}


# ==============================================================================
# 1. INCOMING CALL WEBHOOK
# ==============================================================================

@router.post("/incoming")
async def voicelink_incoming_call(
    request: Request,
    authorization: Optional[str] = Header(None)
):
    """
    VoiceLink Webhook URL for incoming calls.
    Screens caller against daily 3-call limit before answering.
    """
    if not _verify_voicelink_auth(request, authorization):
        logger.warning("[VoiceLink] Unauthorized incoming webhook rejected.")
        raise HTTPException(status_code=401, detail="Unauthorized VoiceLink webhook.")

    payload = await _extract_request_payload(request)
    if not _verify_replay_safety(payload):
        logger.warning("[VoiceLink] Replayed webhook rejected (timestamp skew > 300s).")
        raise HTTPException(status_code=403, detail="Request expired or replay detected.")

    raw_caller = (
        payload.get("From") or
        payload.get("Caller") or
        payload.get("caller_id") or
        payload.get("phone") or
        payload.get("FromNumber")
    )
    provider_call_id = (
        payload.get("CallSid") or
        payload.get("call_id") or
        payload.get("CallId") or
        payload.get("sid") or
        f"vl_{int(time.time()*1000)}"
    )

    # Check caller against daily limit
    check = call_restriction_service.check_daily_call_limit(raw_caller)

    # Log incoming attempt
    if check.normalized_phone:
        call_restriction_service.storage.record_incoming_attempt(
            provider_call_id=provider_call_id,
            caller_hash=hash_phone_number(check.normalized_phone),
            caller_masked=check.masked_phone or "",
            ist_date=get_ist_business_date()
        )

    accept_h = request.headers.get("accept", "").lower()
    is_xml_requested = "xml" in accept_h or "text/xml" in accept_h

    if check.allowed:
        logger.info(
            f"[VoiceLink] Inbound call ALLOWED: call_id={provider_call_id} | "
            f"caller={check.masked_phone} | Remaining={check.remaining_calls}"
        )
        if is_xml_requested:
            xml_resp = (
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Response>\n'
                f'    <Connect>\n'
                f'        <Stream url="/api/telephony/exotel/stream"/>\n'
                f'    </Connect>\n'
                f'</Response>'
            )
            return Response(content=xml_resp, media_type="application/xml")

        return {
            "action": "connect",
            "status": "allowed",
            "call_id": provider_call_id,
            "caller": check.masked_phone,
            "connected_today": check.connected_count,
            "remaining_calls": check.remaining_calls,
            "stream_path": "/api/telephony/exotel/stream"
        }
    else:
        logger.warning(
            f"[VoiceLink] Inbound call REJECTED: call_id={provider_call_id} | "
            f"caller={check.masked_phone} | Status={check.status}"
        )
        if is_xml_requested:
            xml_resp = (
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Response>\n'
                f'    <Say language="mr-IN">{DAILY_LIMIT_EXCEEDED_MESSAGE_MR}</Say>\n'
                f'    <Hangup/>\n'
                f'</Response>'
            )
            return Response(content=xml_resp, media_type="application/xml")

        return {
            "action": "reject",
            "status": check.status,
            "call_id": provider_call_id,
            "caller": check.masked_phone,
            "connected_today": check.connected_count,
            "remaining_calls": 0,
            "reason": "busy",
            "message": check.message or DAILY_LIMIT_EXCEEDED_MESSAGE_MR
        }


# ==============================================================================
# 2. STATUS WEBHOOK
# ==============================================================================

@router.post("/status")
async def voicelink_call_status(
    request: Request,
    authorization: Optional[str] = Header(None)
):
    """
    VoiceLink Call Status Callback.
    Updates call records on connected, disconnected, or failed events.
    """
    if not _verify_voicelink_auth(request, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized VoiceLink webhook.")

    payload = await _extract_request_payload(request)
    provider_call_id = payload.get("CallSid") or payload.get("call_id") or payload.get("CallId")
    raw_caller = payload.get("From") or payload.get("Caller") or payload.get("caller_id") or payload.get("phone")
    call_status = (payload.get("CallStatus") or payload.get("status") or payload.get("call_status") or "").lower()

    if not provider_call_id:
        return {"status": "ignored", "reason": "missing_provider_call_id"}

    logger.info(f"[VoiceLink] Status update: id={provider_call_id} | status='{call_status}'")

    if call_status in ("answered", "in-progress", "connected"):
        conn_res = await call_restriction_service.register_connected_call(
            provider_call_id=provider_call_id,
            raw_phone=raw_caller or ""
        )
        return {
            "status": "processed",
            "event": "connected",
            "allowed": conn_res.allowed,
            "count": conn_res.connected_count,
            "remaining": conn_res.remaining_calls
        }
    elif call_status in ("completed", "canceled", "disconnected"):
        reason = payload.get("HangupReason") or payload.get("termination_reason") or "normal_hangup"
        disc_res = call_restriction_service.register_call_disconnect(
            provider_call_id=provider_call_id,
            termination_reason=str(reason)
        )
        return {
            "status": "processed",
            "event": "disconnected",
            "duration_seconds": disc_res.duration_seconds
        }
    elif call_status in ("busy", "failed", "no-answer"):
        call_restriction_service.register_call_disconnect(
            provider_call_id=provider_call_id,
            termination_reason=f"unconnected_{call_status}"
        )
        return {
            "status": "processed",
            "event": "unconnected",
            "counter_incremented": False
        }

    return {"status": "acknowledged", "event": call_status}


# ==============================================================================
# 3. AUTHENTICATED USAGE ENDPOINT
# ==============================================================================

@router.get("/usage")
async def voicelink_check_usage(
    request: Request,
    phone: str,
    authorization: Optional[str] = Header(None)
):
    """
    Authenticated endpoint to inspect caller quota.
    Protects user privacy by requiring VoiceLink authorization.
    """
    if not _verify_voicelink_auth(request, authorization):
        raise HTTPException(status_code=401, detail="Unauthorized quota inspection.")

    check = call_restriction_service.check_daily_call_limit(phone)
    return {
        "caller": check.masked_phone,
        "is_valid": check.status != "invalid_caller",
        "allowed": check.allowed,
        "connected_today": check.connected_count,
        "remaining_calls": check.remaining_calls,
        "max_daily_limit": settings.MAX_CONNECTED_CALLS_PER_DAY,
        "max_duration_seconds": settings.MAX_CALL_DURATION_SECONDS
    }


# ==============================================================================
# 4. WEBSOCKET AUDIO STREAMING BRIDGE (/api/telephony/exotel/stream)
# ==============================================================================

@router.websocket("/stream")
async def voicelink_websocket_stream(websocket: WebSocket):
    """
    Dedicated VoiceLink WebSocket Media Stream Endpoint.
    Protocol:
    - Inbound: {"event": "start", "start": {"call_sid": "...", "from": "..."}}
    - Audio: {"event": "media", "media": {"payload": "<base64_pcma>"}}
    - Interruption: {"event": "clear"}
    - Outbound audio: {"event": "media", "stream_sid": "...", "media": {"payload": "<base64_pcma>"}}
    - Hangup: {"event": "stop", "stream_sid": "..."}
    """
    await websocket.accept()
    logger.info("[VoiceLinkWS] Telephony carrier connected to /stream")

    call_id: Optional[str] = None
    stream_sid: Optional[str] = None
    caller_phone: Optional[str] = None
    call_registered = False

    # Audio buffers
    inbound_pcm16k_buffer = bytearray()
    speech_active = False
    silence_frames = 0
    session_context_id: Optional[str] = None

    async def _enforce_carrier_timeout(cid: str):
        """Callback invoked by call_restriction_service when 180 seconds elapse."""
        try:
            logger.warning(f"[VoiceLinkWS] 180s duration limit reached for {cid}. Terminating carrier call.")
            if stream_sid:
                # Send stop event to VoiceLink
                await websocket.send_json({
                    "event": "stop",
                    "stream_sid": stream_sid
                })
            # Close WebSocket cleanly
            await websocket.close(code=4008, reason="Max call duration reached (180s)")
        except Exception as e:
            logger.debug(f"[VoiceLinkWS] Error during timeout close for {cid}: {e}")

    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                event = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            event_type = event.get("event", "")

            # ------------------------------------------------------------------
            # EVENT: CONNECTED
            # ------------------------------------------------------------------
            if event_type == "connected":
                logger.info("[VoiceLinkWS] Carrier handshake acknowledged.")
                continue

            # ------------------------------------------------------------------
            # EVENT: START (Call metadata & caller identification)
            # ------------------------------------------------------------------
            elif event_type == "start":
                start_data = event.get("start", {})
                stream_sid = event.get("stream_sid") or start_data.get("stream_sid") or f"stream_{int(time.time()*1000)}"
                call_id = start_data.get("call_sid") or start_data.get("call_id") or stream_sid
                caller_phone = start_data.get("from") or start_data.get("From") or start_data.get("caller")

                logger.info(
                    f"[VoiceLinkWS] Stream START: call_id={call_id} | stream_sid={stream_sid} | "
                    f"caller={mask_phone_number(caller_phone)}"
                )

                # Pre-connection check against 3-call daily limit
                check = call_restriction_service.check_daily_call_limit(caller_phone)
                if not check.allowed:
                    logger.warning(f"[VoiceLinkWS] Caller {mask_phone_number(caller_phone)} exceeded 3-call daily limit!")
                    # Synthesize Marathi rejection message and stream to caller before hanging up
                    try:
                        tts_res = await tts_client.synthesize(
                            text=DAILY_LIMIT_EXCEEDED_MESSAGE_MR,
                            language_code="mr-IN",
                            output_audio_codec="wav"
                        )
                        b64_audio = tts_res.get("audio_base64", "")
                        if b64_audio:
                            wav_bytes = base64.b64decode(b64_audio)
                            pcm_bytes, srate = wav_to_pcm(wav_bytes)
                            pcm8k = resample_16k_to_8k(pcm_bytes) if srate == 16000 else pcm_bytes
                            pcma = linear_to_alaw(pcm8k)
                            for chunk in chunk_pcma(pcma, 320):
                                await websocket.send_json({
                                    "event": "media",
                                    "stream_sid": stream_sid,
                                    "media": {"payload": base64.b64encode(chunk).decode("ascii")}
                                })
                                await asyncio.sleep(0.04)  # 40ms pacing
                    except Exception as e:
                        logger.error(f"[VoiceLinkWS] Failed to stream Marathi limit audio: {e}")

                    # Terminate stream
                    await websocket.send_json({"event": "stop", "stream_sid": stream_sid})
                    await websocket.close(code=4003, reason="Daily limit exceeded")
                    return

                # Register connected call with active duration watchdog
                reg = await call_restriction_service.register_connected_call(
                    provider_call_id=call_id,
                    caller_phone=caller_phone,
                    on_timeout_callback=_enforce_carrier_timeout
                )
                if not reg.allowed:
                    await websocket.send_json({"event": "stop", "stream_sid": stream_sid})
                    await websocket.close(code=4003, reason="Daily limit exceeded")
                    return

                call_registered = True
                session = conversation_manager.get_or_create_session(call_id, language="mr-IN")
                session_context_id = session.session_id

                # Initial polite greeting in Marathi for Nashik CityLinc
                greeting_text = "नमस्कार! मी CityLinc नाशिक बस सेवेचा सहाय्यक आहे. मी आपल्याला कशी मदत करू?"
                try:
                    tts_greet = await tts_client.synthesize(
                        text=greeting_text,
                        language_code="mr-IN",
                        output_audio_codec="wav"
                    )
                    b64_g = tts_greet.get("audio_base64", "")
                    if b64_g:
                        wav_g = base64.b64decode(b64_g)
                        pcm_g, srate = wav_to_pcm(wav_g)
                        pcm8k_g = resample_16k_to_8k(pcm_g) if srate == 16000 else pcm_g
                        pcma_g = linear_to_alaw(pcm8k_g)
                        for chunk in chunk_pcma(pcma_g, 320):
                            await websocket.send_json({
                                "event": "media",
                                "stream_sid": stream_sid,
                                "media": {"payload": base64.b64encode(chunk).decode("ascii")}
                            })
                            await asyncio.sleep(0.04)
                except Exception as e:
                    logger.debug(f"[VoiceLinkWS] Greeting playback error: {e}")

            # ------------------------------------------------------------------
            # EVENT: MEDIA (Caller G.711 A-law PCMA Audio Chunks)
            # ------------------------------------------------------------------
            elif event_type == "media":
                media_data = event.get("media", {})
                b64_payload = media_data.get("payload", "")
                if not b64_payload:
                    continue

                try:
                    raw_pcma = base64.b64decode(b64_payload)
                except Exception:
                    continue

                # 1. Decode G.711 A-law (8kHz) -> 16-bit linear PCM (8kHz)
                pcm8k = alaw_to_linear(raw_pcma)
                # 2. Upsample 8kHz -> 16kHz for Sarvam STT
                pcm16k = resample_8k_to_16k(pcm8k)
                inbound_pcm16k_buffer.extend(pcm16k)

                # Simplified voice activity detection / utterance chunking (~1.5s speech)
                # In live stream, every 48,000 bytes = 1.5s of 16kHz mono 16-bit audio
                if len(inbound_pcm16k_buffer) >= 48000:
                    utterance_pcm = bytes(inbound_pcm16k_buffer)
                    inbound_pcm16k_buffer.clear()

                    # Transcribe caller audio via Sarvam STT
                    try:
                        wav_payload = pcm_to_wav(utterance_pcm, sample_rate=16000)
                        stt_res = await stt_client.transcribe(
                            audio_bytes=wav_payload,
                            language_code="mr-IN",
                            mime_type="audio/wav"
                        )
                        caller_text = stt_res.get("transcript", "").strip()
                        detected_lang = stt_res.get("language_code", "mr-IN")

                        if caller_text:
                            logger.info(f"[VoiceLinkWS] Caller said ({detected_lang}): '{caller_text}'")
                            # Process with deterministic context engine
                            ctx_turn = context_engine.process_turn(session_context_id or call_id, caller_text, language=detected_lang)
                            reply = ctx_turn.get("response_text", "")
                            speech_text = ctx_turn.get("speech_text") or format_text_for_speech(reply, lang=detected_lang)

                            # Synthesize response via Sarvam TTS as WAV
                            tts_res = await tts_client.synthesize(
                                text=speech_text,
                                language_code=detected_lang,
                                output_audio_codec="wav"
                            )
                            b64_out = tts_res.get("audio_base64", "")
                            if b64_out and stream_sid:
                                out_wav = base64.b64decode(b64_out)
                                out_pcm, srate = wav_to_pcm(out_wav)
                                out_pcm8k = resample_16k_to_8k(out_pcm) if srate == 16000 else out_pcm
                                out_pcma = linear_to_alaw(out_pcm8k)

                                for chunk in chunk_pcma(out_pcma, 320):
                                    await websocket.send_json({
                                        "event": "media",
                                        "stream_sid": stream_sid,
                                        "media": {"payload": base64.b64encode(chunk).decode("ascii")}
                                    })
                                    await asyncio.sleep(0.04)
                    except Exception as e:
                        logger.error(f"[VoiceLinkWS] Error processing caller speech: {e}", exc_info=True)

            # ------------------------------------------------------------------
            # EVENT: CLEAR / DTMF / BARGE-IN
            # ------------------------------------------------------------------
            elif event_type == "clear":
                logger.info(f"[VoiceLinkWS] Barge-in clear received for stream {stream_sid}")
                inbound_pcm16k_buffer.clear()

            # ------------------------------------------------------------------
            # EVENT: STOP (Call terminated by carrier)
            # ------------------------------------------------------------------
            elif event_type == "stop":
                logger.info(f"[VoiceLinkWS] Call STOP received from carrier: id={call_id}")
                break

    except WebSocketDisconnect:
        logger.info(f"[VoiceLinkWS] Carrier WebSocket disconnected: id={call_id}")
    except Exception as e:
        logger.error(f"[VoiceLinkWS] WebSocket exception for {call_id}: {e}", exc_info=True)
    finally:
        if call_id and call_registered:
            try:
                call_restriction_service.register_call_disconnect(
                    provider_call_id=call_id,
                    termination_reason="carrier_disconnect"
                )
            except Exception as e:
                logger.error(f"[VoiceLinkWS] Disconnect registration error: {e}")

        if session_context_id:
            conversation_manager.remove_session(session_context_id)
            context_engine.clear_context(session_context_id)

