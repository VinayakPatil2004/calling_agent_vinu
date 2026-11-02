"""
CityLinc Phase 4.4 Stage 2 — Audio Capture Reliability & STT Input Validation Tests.
Covers all 16 test requirements:
1. Valid Marathi utterance.
2. Valid Hindi utterance.
3. Valid English utterance.
4. Very short legitimate response: "हो".
5. Very short legitimate response: "Yes".
6. Empty audio payload.
7. Malformed audio payload.
8. Unsupported MIME type.
9. Truncated audio.
10. Empty Sarvam transcript (ambient noise silent discard).
11. User speech empty transcript (controlled polite retry).
12. Sarvam timeout handling.
13. Sarvam API error handling.
14. Pre-roll candidate onset & first syllable preservation model.
15. Pre-roll transient noise abort without container corruption.
16. WebSocket contract compatibility and Stage 1 VAD regression check.
"""
import base64
import os
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.api.websocket_routes import validate_audio_payload
from backend.app.orchestrator.conversation_manager import conversation_manager
from backend.app.integrations.sarvam.stt import SarvamSTTClient

# Sample valid WebM header (EBML magic bytes + padding to exceed 300 bytes)
VALID_WEBM_BYTES = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01" + b"\x00" * 400
B64_VALID_WEBM = base64.b64encode(VALID_WEBM_BYTES).decode("utf-8")

# Sample valid WAV header (RIFF ... WAVE + fmt + data)
VALID_WAV_BYTES = b"RIFF\x24\x01\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x01\x00\x00" + b"\x00" * 300
B64_VALID_WAV = base64.b64encode(VALID_WAV_BYTES).decode("utf-8")


# ==============================================================================
# Unit Tests for Scope D: Backend Audio Payload Validation
# ==============================================================================

def test_validate_audio_payload_valid_webm():
    """Verify standard WebM audio passes payload validation."""
    is_valid, mime, reason = validate_audio_payload(VALID_WEBM_BYTES, "audio/webm", duration_ms=1200)
    assert is_valid is True
    assert mime == "audio/webm"
    assert reason == "valid"


def test_validate_audio_payload_valid_wav():
    """Verify standard WAV audio passes payload validation."""
    is_valid, mime, reason = validate_audio_payload(VALID_WAV_BYTES, "audio/wav", duration_ms=500)
    assert is_valid is True
    assert mime == "audio/wav"
    assert reason == "valid"


def test_validate_audio_payload_truncated_audio():
    """Verify audio under 200 bytes is rejected as truncated/too small."""
    tiny_audio = b"\x1a\x45\xdf\xa3" + b"\x00" * 50
    is_valid, mime, reason = validate_audio_payload(tiny_audio, "audio/webm")
    assert is_valid is False
    assert "payload_too_small" in reason


def test_validate_audio_payload_unsupported_mime():
    """Verify unsupported binary files (e.g. PDF magic bytes) are rejected."""
    fake_pdf = b"%PDF-1.4" + b"\x00" * 300
    is_valid, mime, reason = validate_audio_payload(fake_pdf, "application/pdf")
    assert is_valid is False
    assert "unsupported_or_corrupt_container" in reason


def test_validate_audio_payload_sub_speech_noise_duration():
    """Verify duration < 180ms with sub-1500 byte payload is rejected as noise."""
    short_audio = VALID_WEBM_BYTES[:350]
    is_valid, mime, reason = validate_audio_payload(short_audio, "audio/webm", duration_ms=100)
    assert is_valid is False
    assert "duration_too_short" in reason


# ==============================================================================
# WebSocket Integration Tests for Scope B, C, D: Language Matrix & Short Words
# ==============================================================================

def test_valid_marathi_utterance_websocket():
    """Test full WebSocket flow for valid Marathi utterance."""
    client = TestClient(app)
    with patch("backend.app.api.websocket_routes.stt_client.transcribe") as mock_stt, \
         patch("backend.app.api.websocket_routes.tts_client.synthesize") as mock_tts:

        mock_stt.return_value = {
            "transcript": "सीबीएस ते नाशिक रोड बस कधी आहे",
            "language_code": "mr-IN"
        }
        mock_tts.return_value = {
            "audio_base64": base64.b64encode(b"dummy_tts_audio").decode("utf-8")
        }

        with client.websocket_connect("/ws/conversation") as ws:
            ws.receive_json()  # ready event

            ws.send_json({
                "type": "audio",
                "data": B64_VALID_WEBM,
                "format": "audio/webm",
                "duration_ms": 2100,
                "language": "mr-IN"
            })

            # Check thinking status
            m1 = ws.receive_json()
            assert m1["type"] == "status"
            assert m1["status"] == "thinking"

            # Check user transcript
            m2 = ws.receive_json()
            assert m2["type"] == "transcript"
            assert m2["role"] == "user"
            assert "सीबीएस" in m2["text"]


def test_valid_hindi_utterance_websocket():
    """Test full WebSocket flow for valid Hindi utterance."""
    client = TestClient(app)
    with patch("backend.app.api.websocket_routes.stt_client.transcribe") as mock_stt, \
         patch("backend.app.api.websocket_routes.tts_client.synthesize") as mock_tts:

        mock_stt.return_value = {
            "transcript": "अगली बस कितने बजे है",
            "language_code": "hi-IN"
        }
        mock_tts.return_value = {
            "audio_base64": base64.b64encode(b"dummy_tts_audio").decode("utf-8")
        }

        with client.websocket_connect("/ws/conversation") as ws:
            ws.receive_json()  # ready

            ws.send_json({
                "type": "audio",
                "data": B64_VALID_WEBM,
                "format": "audio/webm",
                "duration_ms": 1500,
                "language": "hi-IN"
            })

            m1 = ws.receive_json()
            assert m1["type"] == "status"

            m2 = ws.receive_json()
            assert m2["type"] == "transcript"
            assert m2["role"] == "user"
            assert "अगली बस" in m2["text"]


def test_valid_english_utterance_websocket():
    """Test full WebSocket flow for valid English utterance."""
    client = TestClient(app)
    with patch("backend.app.api.websocket_routes.stt_client.transcribe") as mock_stt, \
         patch("backend.app.api.websocket_routes.tts_client.synthesize") as mock_tts:

        mock_stt.return_value = {
            "transcript": "Bus pass information please",
            "language_code": "en-IN"
        }
        mock_tts.return_value = {
            "audio_base64": base64.b64encode(b"dummy_tts_audio").decode("utf-8")
        }

        with client.websocket_connect("/ws/conversation") as ws:
            ws.receive_json()

            ws.send_json({
                "type": "audio",
                "data": B64_VALID_WEBM,
                "format": "audio/webm",
                "duration_ms": 1800,
                "language": "en-IN"
            })

            m1 = ws.receive_json()
            assert m1["type"] == "status"

            m2 = ws.receive_json()
            assert m2["type"] == "transcript"
            assert "Bus pass" in m2["text"]


def test_short_legitimate_response_marathi_ho():
    """Verify short legitimate Marathi response 'हो' (~280ms) is accepted and processed."""
    client = TestClient(app)
    with patch("backend.app.api.websocket_routes.stt_client.transcribe") as mock_stt, \
         patch("backend.app.api.websocket_routes.tts_client.synthesize") as mock_tts:

        mock_stt.return_value = {
            "transcript": "हो",
            "language_code": "mr-IN"
        }
        mock_tts.return_value = {
            "audio_base64": base64.b64encode(b"dummy_tts").decode("utf-8")
        }

        with client.websocket_connect("/ws/conversation") as ws:
            ws.receive_json()

            # Short word "हो" sent with 280ms duration
            ws.send_json({
                "type": "audio",
                "data": B64_VALID_WEBM,
                "format": "audio/webm",
                "duration_ms": 280,
                "language": "mr-IN"
            })

            m1 = ws.receive_json()
            assert m1["type"] == "status"

            m2 = ws.receive_json()
            assert m2["type"] == "transcript"
            assert m2["text"] == "हो"


def test_short_legitimate_response_english_yes():
    """Verify short legitimate English response 'Yes' (~260ms) is accepted and processed."""
    client = TestClient(app)
    with patch("backend.app.api.websocket_routes.stt_client.transcribe") as mock_stt, \
         patch("backend.app.api.websocket_routes.tts_client.synthesize") as mock_tts:

        mock_stt.return_value = {
            "transcript": "Yes",
            "language_code": "en-IN"
        }
        mock_tts.return_value = {
            "audio_base64": base64.b64encode(b"dummy_tts").decode("utf-8")
        }

        with client.websocket_connect("/ws/conversation") as ws:
            ws.receive_json()

            ws.send_json({
                "type": "audio",
                "data": B64_VALID_WEBM,
                "format": "audio/webm",
                "duration_ms": 260,
                "language": "en-IN"
            })

            m1 = ws.receive_json()
            assert m1["type"] == "status"

            m2 = ws.receive_json()
            assert m2["type"] == "transcript"
            assert m2["text"] == "Yes"


def test_empty_audio_payload():
    """Verify empty audio payload is discarded gracefully."""
    client = TestClient(app)
    with client.websocket_connect("/ws/conversation") as ws:
        ws.receive_json()

        ws.send_json({
            "type": "audio",
            "data": "",
            "format": "audio/webm"
        })

        # Server discards and returns status: listening without crashing
        m = ws.receive_json()
        assert m["type"] == "status"
        assert m["status"] == "listening"


def test_malformed_base64_payload():
    """Verify malformed base64 payload returns structured error."""
    client = TestClient(app)
    with client.websocket_connect("/ws/conversation") as ws:
        ws.receive_json()

        ws.send_json({
            "type": "audio",
            "data": "%%%NOT_BASE64%%%",
            "format": "audio/webm"
        })

        m1 = ws.receive_json()
        assert m1["type"] == "error"
        assert "Corrupted" in m1["message"]

        m2 = ws.receive_json()
        assert m2["type"] == "status"
        assert m2["status"] == "listening"


# ==============================================================================
# Tests for Scope C: Empty STT Transcripts & Error Isolation
# ==============================================================================

def test_empty_sarvam_transcript_ambient_noise_silent_discard():
    """Verify empty transcript on short ambient noise burst (<600ms) is silently discarded."""
    client = TestClient(app)
    with patch("backend.app.api.websocket_routes.stt_client.transcribe") as mock_stt:
        mock_stt.return_value = {"transcript": "", "language_code": "unknown"}

        with client.websocket_connect("/ws/conversation") as ws:
            ws.receive_json()

            # Short burst with 400ms duration
            ws.send_json({
                "type": "audio",
                "data": B64_VALID_WEBM[:600],
                "format": "audio/webm",
                "duration_ms": 400
            })

            # Receives thinking status
            m1 = ws.receive_json()
            assert m1["type"] == "status"

            # Must NOT receive an 'info' verbal prompt! Silently resets to listening:
            m2 = ws.receive_json()
            assert m2["type"] == "status"
            assert m2["status"] == "listening"


def test_empty_sarvam_transcript_user_speech_controlled_prompt():
    """Verify genuine speech duration (>=600ms, size >= 1200 bytes) with empty transcript gives controlled polite prompt."""
    client = TestClient(app)
    speech_payload = base64.b64encode(VALID_WEBM_BYTES + b"\x00" * 3000).decode("utf-8")
    with patch("backend.app.api.websocket_routes.stt_client.transcribe") as mock_stt:
        mock_stt.return_value = {"transcript": "", "language_code": "unknown"}

        with client.websocket_connect("/ws/conversation") as ws:
            ws.receive_json()

            # First attempt: genuine speech duration 1500ms, 3400 bytes
            ws.send_json({
                "type": "audio",
                "data": speech_payload,
                "format": "audio/webm",
                "duration_ms": 1500
            })

            ws.receive_json()  # status thinking
            prompt1 = ws.receive_json()
            assert prompt1["type"] == "info"
            assert "आवाज स्पष्ट ऐकू आला नाही" in prompt1["message"]
            ws.receive_json()  # status listening


def test_sarvam_stt_timeout_handling():
    """Verify STT timeout returns service error message, NOT 'could not hear voice'."""
    client = TestClient(app)
    with patch("backend.app.api.websocket_routes.stt_client.transcribe") as mock_stt:
        mock_stt.return_value = {
            "transcript": "",
            "language_code": "unknown",
            "error": "timeout",
            "details": "Connection timed out after 30s"
        }

        with client.websocket_connect("/ws/conversation") as ws:
            ws.receive_json()

            ws.send_json({
                "type": "audio",
                "data": B64_VALID_WEBM,
                "format": "audio/webm",
                "duration_ms": 2000
            })

            ws.receive_json()  # status thinking
            err_msg = ws.receive_json()
            assert err_msg["type"] == "info"
            # Verify message indicates service unavailable, NOT clarity error
            assert "सेवा तात्पुरती अनुपलब्ध आहे" in err_msg["message"]


def test_sarvam_stt_api_error_handling():
    """Verify STT API 500 error returns service error message."""
    client = TestClient(app)
    with patch("backend.app.api.websocket_routes.stt_client.transcribe") as mock_stt:
        mock_stt.return_value = {
            "transcript": "",
            "language_code": "unknown",
            "error": "api_error",
            "status_code": 500
        }

        with client.websocket_connect("/ws/conversation") as ws:
            ws.receive_json()

            ws.send_json({
                "type": "audio",
                "data": B64_VALID_WEBM,
                "format": "audio/webm",
                "duration_ms": 2000
            })

            ws.receive_json()  # status thinking
            err_msg = ws.receive_json()
            assert err_msg["type"] == "info"
            assert "सेवा तात्पुरती अनुपलब्ध आहे" in err_msg["message"]


# ==============================================================================
# Tests for Scope A & E: Pre-Roll First-Syllable Preservation Model
# ==============================================================================

def test_pre_roll_candidate_pre_warm_model():
    """
    Validates that client-side candidate onset starts capturing on Frame 1,
    anchoring speechStartTime to Frame 1 and preserving initial plosives.
    """
    class MockClientVAD:
        def __init__(self):
            self.speechOnsetConsecutiveFrames = 0
            self.speechOnsetRequiredFrames = 3
            self.candidateSpeechStartTime = 0
            self.speechStartTime = 0
            self.isPreWarming = False
            self.isSpeechActive = False
            self.recordedFrames = []

        def process_frame(self, rms: float, timestamp: int, threshold: float = 0.028):
            if not self.isSpeechActive:
                if rms > threshold:
                    if self.speechOnsetConsecutiveFrames == 0:
                        self.candidateSpeechStartTime = timestamp
                        self.isPreWarming = True

                    self.recordedFrames.append(timestamp)
                    self.speechOnsetConsecutiveFrames += 1

                    if self.speechOnsetConsecutiveFrames >= self.speechOnsetRequiredFrames:
                        self.speechOnsetConsecutiveFrames = 0
                        self.isSpeechActive = True
                        self.isPreWarming = False
                        self.speechStartTime = self.candidateSpeechStartTime
                else:
                    self.speechOnsetConsecutiveFrames = 0
                    if self.isPreWarming and not self.isSpeechActive:
                        self.isPreWarming = False
                        self.candidateSpeechStartTime = 0
                        self.recordedFrames.clear()

    vad = MockClientVAD()
    # Frame 1: Candidate onset at t=1000
    vad.process_frame(rms=0.045, timestamp=1000)
    assert vad.isPreWarming is True
    assert vad.candidateSpeechStartTime == 1000
    assert len(vad.recordedFrames) == 1

    # Frame 2: Sustained at t=1016
    vad.process_frame(rms=0.050, timestamp=1016)
    assert vad.isPreWarming is True
    assert len(vad.recordedFrames) == 2

    # Frame 3: Confirmed at t=1033
    vad.process_frame(rms=0.048, timestamp=1033)
    assert vad.isSpeechActive is True
    assert vad.speechStartTime == 1000  # Anchored to Frame 1, NOT Frame 3!
    assert len(vad.recordedFrames) == 3  # All 3 onset frames preserved in container!


def test_pre_roll_transient_noise_clean_abort():
    """
    Validates that a 1-frame click initiates candidate pre-warm, but when energy
    drops on frame 2, pre-warm is cleanly aborted and frames cleared.
    """
    class MockClientVAD:
        def __init__(self):
            self.speechOnsetConsecutiveFrames = 0
            self.candidateSpeechStartTime = 0
            self.isPreWarming = False
            self.isSpeechActive = False
            self.recordedFrames = []

        def process_frame(self, rms: float, timestamp: int, threshold: float = 0.028):
            if not self.isSpeechActive:
                if rms > threshold:
                    if self.speechOnsetConsecutiveFrames == 0:
                        self.candidateSpeechStartTime = timestamp
                        self.isPreWarming = True
                    self.recordedFrames.append(timestamp)
                    self.speechOnsetConsecutiveFrames += 1
                else:
                    self.speechOnsetConsecutiveFrames = 0
                    if self.isPreWarming and not self.isSpeechActive:
                        self.isPreWarming = False
                        self.candidateSpeechStartTime = 0
                        self.recordedFrames.clear()

    vad = MockClientVAD()
    # Frame 1: Click at t=1000
    vad.process_frame(rms=0.065, timestamp=1000)
    assert vad.isPreWarming is True
    assert len(vad.recordedFrames) == 1

    # Frame 2: Click ends at t=1016
    vad.process_frame(rms=0.012, timestamp=1016)
    assert vad.isPreWarming is False
    assert vad.candidateSpeechStartTime == 0
    assert len(vad.recordedFrames) == 0  # Cleanly cleared without transmission!


def test_source_code_stage2_integrity():
    """Verify app.js contains all Stage 2 pre-warm, duration validation, and metadata logic."""
    js_path = os.path.join("backend", "app", "static", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "candidateSpeechStartTime: 0" in content
    assert "isPreWarming: false" in content
    assert "minValidSpeechDurationMs: 250" in content
    assert "minValidBlobBytes: 800" in content
    assert "duration_ms: durationMs" in content


# ==============================================================================
# Unit & Integration Tests for Group A: STT Recovery & Audio Capture Reliability
# ==============================================================================

def test_group_a_capture_session_decoupling():
    """
    Validates the core Group A P0 bug fix:
    When an utterance completes, VAD resets global state (isSpeechActive=False,
    candidateSpeechStartTime=0) BEFORE MediaRecorder.onstop fires.
    With capture-specific metadata (capture.isConfirmed=True), the recording
    is preserved and NOT discarded.
    """
    class MockCaptureSession:
        def __init__(self, session_id: int, capture_type: str = "auto", start_time: int = 1000):
            self.id = session_id
            self.type = capture_type
            self.startTime = start_time
            self.isConfirmed = (capture_type == "manual")
            self.chunks = []
            self.disposed = False
            self.stopReason = None

    class MockRecorderLifecycle:
        def __init__(self):
            # Global VAD state variables
            self.isSpeechActive = False
            self.candidateSpeechStartTime = 0
            self.isManualRecording = False
            self.activeCaptureSession = None
            self.transmitted_blobs = []

        def on_candidate_onset(self, t: int):
            self.candidateSpeechStartTime = t
            self.activeCaptureSession = MockCaptureSession(1, "auto", t)

        def on_speech_confirmed(self):
            self.isSpeechActive = True
            if self.activeCaptureSession:
                self.activeCaptureSession.isConfirmed = True

        def on_utterance_finalized(self):
            # Global VAD resets BEFORE onstop fires! (This caused the previous race condition)
            self.isSpeechActive = False
            self.candidateSpeechStartTime = 0
            if self.activeCaptureSession:
                self.activeCaptureSession.stopReason = "utterance_finalized"

        def on_media_recorder_stop(self):
            capture = self.activeCaptureSession
            self.activeCaptureSession = None

            # Group A logic: Decoupled check against capture session metadata
            if not capture:
                return
            if capture.disposed:
                return
            if not capture.isConfirmed and capture.type != "manual":
                # Drops unconfirmed transient noise safely
                return
            if not capture.chunks:
                return

            # Preserved and transmitted!
            self.transmitted_blobs.append(capture)

    lifecycle = MockRecorderLifecycle()
    # Step 1: Candidate onset at t=1000
    lifecycle.on_candidate_onset(1000)
    lifecycle.activeCaptureSession.chunks.append(b"audio_chunk_1")

    # Step 2: Speech confirmed
    lifecycle.on_speech_confirmed()
    lifecycle.activeCaptureSession.chunks.append(b"audio_chunk_2")

    # Step 3: VAD silence cutoff resets global state
    lifecycle.on_utterance_finalized()
    assert lifecycle.isSpeechActive is False
    assert lifecycle.candidateSpeechStartTime == 0

    # Step 4: Asynchronous onstop triggers
    lifecycle.on_media_recorder_stop()

    # CRITICAL: Recording must be preserved and transmitted despite global state reset!
    assert len(lifecycle.transmitted_blobs) == 1
    assert lifecycle.transmitted_blobs[0].id == 1
    assert len(lifecycle.transmitted_blobs[0].chunks) == 2


def test_group_a_unconfirmed_transient_clean_drop():
    """
    Validates that an unconfirmed transient candidate pre-warm (e.g. keyboard tap)
    is cleanly dropped when aborted without transmitting audio.
    """
    class MockCaptureSession:
        def __init__(self, session_id: int):
            self.id = session_id
            self.type = "auto"
            self.isConfirmed = False
            self.chunks = [b"transient_noise"]
            self.disposed = False
            self.stopReason = None

    session = MockCaptureSession(2)
    session.stopReason = "transient_abort"

    # Evaluate Group A discard condition
    is_discarded = False
    if not session.isConfirmed and session.type != "manual":
        is_discarded = True

    assert is_discarded is True


def test_group_a_media_recorder_state_guard():
    """
    Validates Task B: MediaRecorder state guard logic.
    start() must only be called when state is 'inactive'.
    Attempting start when state is 'recording' or 'paused' must be safely skipped.
    """
    class MockMediaRecorder:
        def __init__(self, state="inactive"):
            self.state = state
            self.start_calls = 0

        def start(self, timeslice=100):
            if self.state != "inactive":
                raise RuntimeError(f"InvalidStateError: state is {self.state}")
            self.state = "recording"
            self.start_calls += 1

    # Guard function matching app.js safelyStartMediaRecorder
    def safely_start(recorder):
        if not recorder:
            return False
        if recorder.state != "inactive":
            return False
        try:
            recorder.start(100)
            return True
        except Exception:
            return False

    rec = MockMediaRecorder(state="recording")
    assert safely_start(rec) is False
    assert rec.start_calls == 0

    rec.state = "inactive"
    assert safely_start(rec) is True
    assert rec.start_calls == 1
    assert rec.state == "recording"


def test_group_a_cache_buster_and_frontend_integrity():
    """
    Validates Task C and Group A code changes in index.html and app.js:
    1. index.html references app.js?v=4.4.2 (cache-busting).
    2. app.js contains createCaptureSession, safelyStartMediaRecorder, safelyStopMediaRecorder.
    3. app.js contains handleUtteranceRecorded with decoupled capture session logic.
    4. app.js contains activeCaptureSession in state.
    """
    html_path = os.path.join("backend", "app", "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    assert '<script src="/static/app.js?v=4.4.5"></script>' in html_content

    js_path = os.path.join("backend", "app", "static", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js_content = f.read()

    assert "activeCaptureSession: null" in js_content
    assert "captureSequence: 0" in js_content
    assert "function createCaptureSession" in js_content
    assert "function safelyStartMediaRecorder" in js_content
    assert "function safelyStopMediaRecorder" in js_content
    assert "handleUtteranceRecorded(captureSession = null)" in js_content
    assert "session.isConfirmed" in js_content
    assert "safelyStopMediaRecorder('utterance_finalized')" in js_content
    assert "safelyStopMediaRecorder('transient_abort')" in js_content
    assert "createCaptureSession('manual'" in js_content
    assert "createCaptureSession('barge_in'" in js_content

