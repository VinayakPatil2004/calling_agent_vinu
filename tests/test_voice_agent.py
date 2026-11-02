"""
Automated End-to-End Tests for CityLinc Multilingual Voice Agent.
"""
import pytest
import asyncio
import wave
import io
import base64
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.services.citylinc_knowledge import knowledge_service
from backend.app.integrations.sarvam.stt import stt_client
from backend.app.integrations.sarvam.tts import tts_client, generate_chime_wav
from backend.app.integrations.llm.client import llm_client
from backend.app.orchestrator.conversation_manager import conversation_manager
from backend.app.main import app


def test_config():
    """Verify application configuration loads cleanly."""
    assert settings.PROJECT_NAME == "CityLinc Multilingual Voice Agent"
    assert "mr-IN" in settings.SUPPORTED_LANGUAGES
    assert "hi-IN" in settings.SUPPORTED_LANGUAGES
    assert "en-IN" in settings.SUPPORTED_LANGUAGES


def test_knowledge_service_loading():
    """Verify that all timetables and bus passes are parsed and indexed."""
    assert len(knowledge_service.timetables) >= 100, f"Expected >= 100 timetables, got {len(knowledge_service.timetables)}"
    assert "open_ended_pass" in knowledge_service.bus_passes
    assert "student_pass" in knowledge_service.bus_passes
    assert "101" in knowledge_service.routes_by_number
    assert "104" in knowledge_service.routes_by_number


def test_multilingual_knowledge_responses():
    """Test exact transit factual answering across Marathi, Hindi, and English."""
    # Marathi student pass
    mr_ans = knowledge_service.answer_query_locally("विद्यार्थी पास ची माहिती द्या", "mr-IN")
    assert mr_ans is not None
    assert "५०%" in mr_ans or "सवलत" in mr_ans

    # Hindi bus 101
    hi_ans = knowledge_service.answer_query_locally("बस 101 कब आएगी?", "hi-IN")
    assert hi_ans is not None
    assert "101" in hi_ans

    # English open pass
    en_ans = knowledge_service.answer_query_locally("What are the bus pass rates?", "en-IN")
    assert en_ans is not None
    assert "110" in en_ans or "Pass" in en_ans


def test_chime_wav_generator():
    """Verify that fallback audio produces a valid 16kHz mono WAV file."""
    wav_bytes = generate_chime_wav(0.2, 440.0)
    assert len(wav_bytes) > 100
    with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000


@pytest.mark.asyncio
async def test_sarvam_stt_fallback():
    """Test STT client fallback behavior when key is not configured."""
    from backend.app.integrations.sarvam.stt import SarvamSTTClient
    mock_stt = SarvamSTTClient(api_key="")
    res = await mock_stt.transcribe(b"test_dummy_audio_bytes" * 10)
    assert "transcript" in res
    assert "language_code" in res


@pytest.mark.asyncio
async def test_sarvam_tts_fallback():
    """Test TTS client fallback audio generation."""
    res = await tts_client.synthesize("नमस्कार, CityLinc मध्ये आपले स्वागत आहे", language_code="mr-IN")
    assert "audio_base64" in res
    assert len(res["audio_base64"]) > 100
    # Decode to ensure valid base64
    decoded = base64.b64decode(res["audio_base64"])
    assert len(decoded) > 100


def test_conversation_barge_in():
    """Verify interruption / barge-in logic in session manager."""
    session = conversation_manager.get_or_create_session("test_session_123")
    turn_1 = session.add_user_message("Hello", language="en-IN")
    assert turn_1 == 1
    assert session.current_language == "en-IN"

    # Interrupt during assistant response
    new_turn = session.interrupt()
    assert new_turn == 2
    assert session.is_interrupted is True

    # Assistant message for turn 1 should be dropped
    accepted = session.add_assistant_message("Late response", turn_id=turn_1)
    assert accepted is False
    assert len(session.history) == 1  # Only user message remains


def test_rest_endpoints():
    """Test FastAPI HTTP endpoints."""
    client = TestClient(app)
    
    # Health check
    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json()["status"] == "healthy"

    # API Status
    res_status = client.get("/api/status")
    assert res_status.status_code == 200
    data = res_status.json()
    assert data["stt_model"] == "saaras:v4"
    assert data["tts_model"] == "bulbul:v3"
    assert data["loaded_timetables"] >= 100

    # Static HTML UI
    res_ui = client.get("/")
    assert res_ui.status_code == 200
    assert "CityLinc" in res_ui.text

    # Chat API
    res_chat = client.post("/api/chat", json={
        "message": "विद्यार्थी पास ची माहिती द्या",
        "language": "mr-IN"
    })
    assert res_chat.status_code == 200
    chat_data = res_chat.json()
    assert "response" in chat_data
    assert "विद्यार्थी" in chat_data["response"] or "सवलत" in chat_data["response"]


def test_websocket_interaction():
    """Test real-time WebSocket communication."""
    client = TestClient(app)
    with client.websocket_connect("/ws/conversation") as ws:
        # Initial ready message
        init_msg = ws.receive_json()
        assert init_msg["type"] == "ready"

        # Ping
        ws.send_json({"type": "ping"})
        pong_msg = ws.receive_json()
        assert pong_msg["type"] == "pong"

        # Text conversation
        ws.send_json({
            "type": "text",
            "text": "What are the bus pass rates?",
            "language": "en-IN"
        })
        
        # User transcript event
        user_t = ws.receive_json()
        assert user_t["type"] == "transcript"
        assert user_t["role"] == "user"

        # Status thinking event
        status_msg = ws.receive_json()
        assert status_msg["type"] == "status"
        assert status_msg["status"] == "thinking"

        # Assistant transcript event
        asst_t = ws.receive_json()
        assert asst_t["type"] == "transcript"
        assert asst_t["role"] == "assistant"

        # Status speaking event
        speak_status = ws.receive_json()
        assert speak_status["type"] == "status"
        assert speak_status["status"] == "speaking"

        # Audio event
        audio_msg = ws.receive_json()
        assert audio_msg["type"] == "audio"
        assert "audio" in audio_msg
        assert len(audio_msg["audio"]) > 100

        # Test interruption / barge-in
        ws.send_json({"type": "interrupt"})
        inter_msg = ws.receive_json()
        assert inter_msg["type"] == "interrupted"

