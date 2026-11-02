"""
Tests for Sarvam STT MIME type normalization and WebM multipart upload handling.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.app.integrations.sarvam.stt import SarvamSTTClient
from backend.app.main import app


@pytest.mark.asyncio
async def test_stt_mime_normalization_webm_opus():
    """Verify that audio/webm;codecs=opus is normalized to audio/webm and named recording.webm."""
    stt = SarvamSTTClient(api_key="test_dummy_key_12345")
    sample_bytes = b"GORM" + b"\x01" * 300

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "transcript": "नाशिक सिटीलिंक",
            "language_code": "mr-IN"
        }
        mock_post.return_value = mock_resp

        result = await stt.transcribe(
            audio_bytes=sample_bytes,
            language_code="mr-IN",
            mime_type="audio/webm;codecs=opus"
        )

        assert result["transcript"] == "नाशिक सिटीलिंक"
        assert result["language_code"] == "mr-IN"

        # Check call arguments to httpx
        assert mock_post.called
        _, kwargs = mock_post.call_args
        files = kwargs.get("files", {})
        filename, sent_bytes, content_type = files["file"]

        # Exact requirements:
        # 1. Preserve actual recorded WebM/Opus audio bytes
        assert sent_bytes == sample_bytes
        # 2. Normalize multipart upload Content-Type to audio/webm by removing ;codecs=opus
        assert content_type == "audio/webm"
        # 3. Use matching filename recording.webm
        assert filename == "recording.webm"


@pytest.mark.asyncio
async def test_stt_mime_normalization_wav_and_mp3():
    """Verify normalization for wav and mp3 formats."""
    stt = SarvamSTTClient(api_key="test_dummy_key_12345")
    sample_bytes = b"RIFF" + b"\x00" * 300

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"transcript": "Hello", "language_code": "en-IN"}
        mock_post.return_value = mock_resp

        # WAV
        await stt.transcribe(sample_bytes, mime_type="audio/wav;rate=16000")
        _, kwargs_wav = mock_post.call_args
        filename, _, content_type = kwargs_wav["files"]["file"]
        assert filename == "recording.wav"
        assert content_type == "audio/wav"

        # MP3
        await stt.transcribe(sample_bytes, mime_type="audio/mpeg;codecs=mp3")
        _, kwargs_mp3 = mock_post.call_args
        filename, _, content_type = kwargs_mp3["files"]["file"]
        assert filename == "recording.mp3"
        assert content_type == "audio/mpeg"


def test_websocket_accepts_webm_with_codecs_parameter():
    """Verify WebSocket route cleanly accepts raw format with codecs parameter and dispatches to STT."""
    client = TestClient(app)
    with client.websocket_connect("/ws/conversation") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"

        # Send audio event with audio/webm;codecs=opus
        import base64
        fake_b64 = base64.b64encode(b"FAKE_AUDIO_DATA" * 20).decode("utf-8")
        ws.send_json({
            "type": "audio",
            "data": fake_b64,
            "format": "audio/webm;codecs=opus",
            "language": "mr-IN"
        })

        # Next message must be status: thinking with stage: stt
        status_msg = ws.receive_json()
        assert status_msg["type"] == "status"
        assert status_msg["stage"] == "stt"
        assert status_msg["bytes_received"] == len(b"FAKE_AUDIO_DATA" * 20)

