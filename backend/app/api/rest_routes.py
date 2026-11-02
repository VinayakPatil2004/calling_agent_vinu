"""
REST API routes for status, testing STT, TTS, and text chat.
"""
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from backend.app.core.config import settings
from backend.app.integrations.sarvam.stt import stt_client
from backend.app.integrations.sarvam.tts import tts_client
from backend.app.integrations.llm.client import llm_client
from backend.app.services.citylinc_knowledge import knowledge_service
from backend.app.services.language_detector import language_detector
from backend.app.orchestrator.conversation_manager import conversation_manager

router = APIRouter(prefix="/api", tags=["API"])

class StatusResponse(BaseModel):
    project: str
    version: str
    sarvam_key_configured: bool
    stt_model: str
    tts_model: str
    supported_languages: List[str]
    loaded_timetables: int
    loaded_routes: int
    loaded_pass_categories: int

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    language: Optional[str] = "mr-IN"

class TTSRequest(BaseModel):
    text: str
    language: Optional[str] = "mr-IN"
    speaker: Optional[str] = None
    pace: Optional[float] = 1.0


@router.get("/status", response_model=StatusResponse)
async def get_status():
    """Returns application status, loaded transit data, and API key availability."""
    return StatusResponse(
        project=settings.PROJECT_NAME,
        version=settings.VERSION,
        sarvam_key_configured=settings.has_sarvam_key,
        stt_model=settings.SARVAM_STT_MODEL,
        tts_model=settings.SARVAM_TTS_MODEL,
        supported_languages=settings.SUPPORTED_LANGUAGES,
        loaded_timetables=len(knowledge_service.timetables),
        loaded_routes=len(knowledge_service.routes_by_id),
        loaded_pass_categories=len(knowledge_service.bus_passes)
    )


@router.post("/stt/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...),
    language: Optional[str] = Form("unknown")
):
    """Transcribes an uploaded audio file using Sarvam STT (Saaras v4)."""
    try:
        audio_bytes = await file.read()
        res = await stt_client.transcribe(
            audio_bytes=audio_bytes,
            language_code=language or "unknown",
            filename=file.filename or "audio.wav",
            mime_type=file.content_type or "audio/wav"
        )
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tts/synthesize")
async def synthesize_speech(req: TTSRequest):
    """Synthesizes text into audio using Sarvam TTS (Bulbul v3)."""
    try:
        res = await tts_client.synthesize(
            text=req.text,
            language_code=req.language or "mr-IN",
            speaker=req.speaker,
            pace=req.pace or 1.0
        )
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat")
async def chat(req: ChatRequest):
    """Text-based chat endpoint with context tracking and multilingual response."""
    try:
        session = conversation_manager.get_or_create_session(req.session_id, language="mr-IN")
        
        # Detect language dynamically from message text and session context
        det_result = language_detector.detect(
            text=req.message,
            context_language=session.current_language,
            user_preference=req.language if req.language in ("mr-IN", "hi-IN", "en-IN") else None
        )
        detected_lang = det_result.language

        turn_id = session.add_user_message(req.message, language=detected_lang)
        reply = await llm_client.generate_response(session.history, language_code=detected_lang)
        session.add_assistant_message(reply, turn_id)
        
        return {
            "session_id": session.session_id,
            "turn_id": turn_id,
            "language": detected_lang,
            "confidence": det_result.confidence,
            "response": reply
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

