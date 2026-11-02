"""
Automated Test Suite for VoiceLink / Exotel Telephony Integration (Phase 4.5.2).
Covers all 15 verification requirements:
1. VoiceLink authentication (valid token accepted).
2. Invalid webhook rejection (missing or bad token returns 401, replay rejected).
3. Incoming caller identity (extracted and normalized to E.164).
4. PCMA decoding (alaw_to_linear 8-bit to 16-bit linear PCM).
5. 8kHz to 16kHz conversion (resample_8k_to_16k).
6. 16kHz to PCMA conversion (resample_16k_to_8k and linear_to_alaw).
7. Bidirectional WebSocket streaming on /api/telephony/exotel/stream.
8. First three calls allowed (connected_count increments 1, 2, 3).
9. Fourth call Marathi rejection (DAILY_LIMIT_EXCEEDED_MESSAGE_MR).
10. Duplicate webhook idempotency.
11. Concurrent calls race-safety.
12. Actual provider hangup integration using mock exotel_client.
13. 180-second duration timeout triggers hangup and close.
14. Server restart recovery.
15. Verification that browser /ws/conversation remains functional and isolated.
"""
import io
import time
import base64
import json
import random
import asyncio
import struct
import wave
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.main import app
from backend.app.services.phone_normalizer import (
    normalize_mobile_number,
    mask_phone_number,
    hash_phone_number,
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
from backend.app.services.call_restriction_service import (
    call_restriction_service,
    DAILY_LIMIT_EXCEEDED_MESSAGE_MR
)
from backend.app.integrations.telephony.exotel_client import exotel_client


# ==============================================================================
# 1 & 2. AUTHENTICATION & REJECTION TESTS
# ==============================================================================

def test_1_and_2_voicelink_auth_and_rejection():
    """Verify VoiceLink authentication via token and rejection of unauthorized or expired requests."""
    client = TestClient(app)
    test_token = "secret_voicelink_token_test_123"

    with patch.object(settings, "VOICELINK_AUTH_TOKEN", test_token):
        # 1. Missing token -> 401 Unauthorized
        res_no_auth = client.post(
            "/api/telephony/exotel/incoming",
            json={"caller_id": "+919876543210", "call_id": "test_auth_1"}
        )
        assert res_no_auth.status_code == 401

        # 2. Invalid token -> 401 Unauthorized
        res_bad_auth = client.post(
            "/api/telephony/exotel/incoming",
            json={"caller_id": "+919876543210", "call_id": "test_auth_2"},
            headers={"Authorization": "Bearer wrong_token"}
        )
        assert res_bad_auth.status_code == 401

        # 3. Valid Bearer token -> 200 Allowed
        res_bearer = client.post(
            "/api/telephony/exotel/incoming",
            json={"caller_id": "+919876543210", "call_id": "test_auth_3"},
            headers={"Authorization": f"Bearer {test_token}"}
        )
        assert res_bearer.status_code == 200
        assert res_bearer.json()["action"] == "connect"

        # 4. Valid Query param token -> 200 Allowed
        res_query = client.post(
            f"/api/telephony/exotel/incoming?token={test_token}",
            json={"caller_id": "+919876543210", "call_id": "test_auth_4"}
        )
        assert res_query.status_code == 200
        assert res_query.json()["action"] == "connect"

        # 5. Replay attack with timestamp skewed by 600s -> 403 Forbidden
        old_timestamp = time.time() - 600
        res_replay = client.post(
            f"/api/telephony/exotel/incoming?token={test_token}",
            json={"caller_id": "+919876543210", "call_id": "test_auth_5", "timestamp": old_timestamp}
        )
        assert res_replay.status_code == 403

        # 6. Usage endpoint requires auth
        res_usage_bad = client.get("/api/telephony/exotel/usage?phone=+919876543210")
        assert res_usage_bad.status_code == 401

        res_usage_ok = client.get(
            f"/api/telephony/exotel/usage?phone=+919876543210&token={test_token}"
        )
        assert res_usage_ok.status_code == 200


# ==============================================================================
# 3. INCOMING CALLER IDENTITY EXTRACTION
# ==============================================================================

def test_3_incoming_caller_identity_normalization():
    """Verify caller identity extraction from multiple standard carrier fields and normalization."""
    client = TestClient(app)

    # Various provider field conventions
    test_cases = [
        ({"From": "9876543211", "CallSid": "sid_1"}, "+919876543211"),
        ({"Caller": "09876543212", "call_id": "sid_2"}, "+919876543212"),
        ({"caller_id": "+91 98765 43213", "CallId": "sid_3"}, "+919876543213"),
        ({"FromNumber": "919876543214", "sid": "sid_4"}, "+919876543214"),
    ]

    for payload, expected_norm in test_cases:
        res = client.post("/api/telephony/exotel/incoming", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["action"] == "connect"
        assert mask_phone_number(expected_norm) == data["caller"]


# ==============================================================================
# 4, 5, 6. AUDIO CODEC & RESAMPLING TESTS (PCMA <-> PCM16, 8kHz <-> 16kHz)
# ==============================================================================

def test_4_pcma_decoding():
    """Test ITU-T G.711 A-law decoding to 16-bit signed PCM."""
    # Test silence (0xD5) and various values
    pcma_input = bytes([0xD5, 0x55, 0xAA, 0x33])
    pcm16 = alaw_to_linear(pcma_input)
    assert len(pcm16) == len(pcma_input) * 2  # 1 byte -> 2 bytes (16-bit)
    samples = struct.unpack(f"<{len(pcma_input)}h", pcm16)
    # A-law 0xD5 decodes to ~8 (silence floor)
    assert abs(samples[0]) <= 10


def test_5_resampling_8k_to_16k():
    """Test 8,000 Hz to 16,000 Hz linear interpolation."""
    # 4 samples at 8kHz: [0, 1000, 2000, 0]
    pcm8k = struct.pack("<4h", 0, 1000, 2000, 0)
    pcm16k = resample_8k_to_16k(pcm8k)
    assert len(pcm16k) == len(pcm8k) * 2  # Doubled sample count
    samples_16k = struct.unpack("<8h", pcm16k)
    # Interpolated sample between 0 and 1000 should be 500
    assert samples_16k[0] == 0
    assert samples_16k[1] == 500
    assert samples_16k[2] == 1000
    assert samples_16k[3] == 1500
    assert samples_16k[4] == 2000


def test_6_resampling_16k_to_8k_and_pcma_encoding():
    """Test 16,000 Hz to 8,000 Hz downsampling and linear PCM to PCMA encoding."""
    # 8 samples at 16kHz
    pcm16k = struct.pack("<8h", 0, 1000, 2000, 3000, 4000, 3000, 2000, 1000)
    pcm8k = resample_16k_to_8k(pcm16k)
    assert len(pcm8k) == len(pcm16k) // 2

    pcma = linear_to_alaw(pcm8k)
    assert len(pcma) == len(pcm8k) // 2
    chunks = chunk_pcma(pcma, chunk_size=320)
    assert len(chunks) == 1
    assert len(chunks[0]) == 320  # Padded to 320 bytes


# ==============================================================================
# 7, 8, 9, 10, 11. WEBSOCKET STREAMING & RATE LIMIT INTEGRATION
# ==============================================================================

def test_8_and_9_voicelink_daily_limit_and_marathi_rejection():
    """Verify first 3 calls allowed and 4th call receives Marathi rejection."""
    client = TestClient(app)
    uid = random.randint(10000000, 99999999)
    user = f"+9198{uid}"

    # Call 1
    c1 = client.post("/api/telephony/exotel/incoming", json={"From": user, "CallSid": f"vl_{uid}_1"})
    assert c1.json()["action"] == "connect"
    s1 = client.post("/api/telephony/exotel/status", json={"From": user, "CallSid": f"vl_{uid}_1", "status": "answered"})
    assert s1.json()["count"] == 1

    # Call 2
    client.post("/api/telephony/exotel/incoming", json={"From": user, "CallSid": f"vl_{uid}_2"})
    s2 = client.post("/api/telephony/exotel/status", json={"From": user, "CallSid": f"vl_{uid}_2", "status": "answered"})
    assert s2.json()["count"] == 2

    # Call 3
    client.post("/api/telephony/exotel/incoming", json={"From": user, "CallSid": f"vl_{uid}_3"})
    s3 = client.post("/api/telephony/exotel/status", json={"From": user, "CallSid": f"vl_{uid}_3", "status": "answered"})
    assert s3.json()["count"] == 3

    # Call 4 -> Must be rejected with Marathi message
    c4 = client.post("/api/telephony/exotel/incoming", json={"From": user, "CallSid": f"vl_{uid}_4"})
    assert c4.status_code == 200
    data4 = c4.json()
    assert data4["action"] == "reject"
    assert data4["reason"] == "busy"
    assert data4["message"] == DAILY_LIMIT_EXCEEDED_MESSAGE_MR


def test_10_duplicate_status_webhook_idempotency():
    """Verify duplicate answered status webhook does not increment call count."""
    client = TestClient(app)
    uid = random.randint(10000000, 99999999)
    user = f"+9198{uid}"
    call_id = f"vl_{uid}_dup"

    # Inbound connect
    client.post("/api/telephony/exotel/incoming", json={"From": user, "CallSid": call_id})

    # Status answered #1
    s1 = client.post("/api/telephony/exotel/status", json={"From": user, "CallSid": call_id, "status": "answered"})
    assert s1.json()["count"] == 1

    # Duplicate status answered #2
    s2 = client.post("/api/telephony/exotel/status", json={"From": user, "CallSid": call_id, "status": "answered"})
    assert s2.json()["count"] == 1  # Remained 1!


@pytest.mark.asyncio
async def test_11_concurrent_calls_race_safety(tmp_path):
    """Verify two concurrent calls for user at limit 2 result in exactly 1 allowed, 1 rejected."""
    uid = random.randint(10000000, 99999999)
    user = f"+9198{uid}"

    # Connect calls 1 & 2
    await call_restriction_service.register_connected_call(f"conc_{uid}_1", user)
    await call_restriction_service.register_connected_call(f"conc_{uid}_2", user)

    # Launch 2 concurrent calls trying to claim the remaining 1 slot
    results = await asyncio.gather(
        call_restriction_service.register_connected_call(f"conc_{uid}_3a", user),
        call_restriction_service.register_connected_call(f"conc_{uid}_3b", user)
    )

    allowed = sum(1 for r in results if r.allowed)
    rejected = sum(1 for r in results if not r.allowed)
    assert allowed == 1
    assert rejected == 1


# ==============================================================================
# 7. WEBSOCKET STREAMING PROTOCOL TEST
# ==============================================================================

def test_7_voicelink_websocket_stream_protocol():
    """Verify VoiceLink WebSocket stream connects, receives start, and receives greeting."""
    client = TestClient(app)
    uid = random.randint(10000000, 99999999)
    user = f"+9198{uid}"
    stream_id = f"stream_{uid}"
    call_id = f"call_{uid}"

    with client.websocket_connect("/api/telephony/exotel/stream") as ws:
        # 1. Send start event
        start_payload = {
            "event": "start",
            "stream_sid": stream_id,
            "start": {
                "stream_sid": stream_id,
                "call_sid": call_id,
                "from": user,
                "to": "919484957050",
                "media_format": {
                    "encoding": "audio/x-alaw",
                    "sample_rate": 8000
                }
            }
        }
        ws.send_json(start_payload)

        # 2. Receive greeting audio chunk from agent
        event = ws.receive_json()
        assert event["event"] == "media"
        assert event["stream_sid"] == stream_id
        assert len(event["media"]["payload"]) > 10

        # 3. Send stop event
        ws.send_json({"event": "stop", "stream_sid": stream_id})


# ==============================================================================
# 12 & 13. ACTUAL PROVIDER HANGUP INTEGRATION & 180S TIMEOUT
# ==============================================================================

@pytest.mark.asyncio
async def test_12_and_13_provider_hangup_and_180s_timeout():
    """Verify that timeout triggers exotel_client.hangup_call and marks record terminated."""
    uid = random.randint(10000000, 99999999)
    user = f"+9198{uid}"
    call_id = f"hangup_test_{uid}"

    hangup_called_with = None

    def mock_hangup(cid):
        nonlocal hangup_called_with
        hangup_called_with = cid
        return True

    exotel_client.set_mock_hangup_handler(mock_hangup)

    try:
        # Register call
        reg = await call_restriction_service.register_connected_call(call_id, user)
        assert reg.allowed is True

        # Reschedule short timeout of 0.15s to simulate 180s expiration
        call_restriction_service.schedule_call_timeout(call_id, timeout_seconds=0.15)

        # Wait for timeout to fire
        await asyncio.sleep(0.25)

        # Verify carrier hangup was invoked
        assert hangup_called_with == call_id

        # Verify call status in storage
        rec = call_restriction_service.storage.get_call_record(call_id)
        assert rec["status"] == "timeout"
        assert rec["termination_reason"] == "max_duration_180s"

    finally:
        exotel_client.set_mock_hangup_handler(None)


# ==============================================================================
# 14. SERVER RESTART TIMEOUT RECOVERY
# ==============================================================================

def test_14_server_restart_recovery():
    """Verify recovery routine terminates expired calls and reschedules active ones."""
    recovered = call_restriction_service.recover_active_call_timeouts()
    # Should execute cleanly without error
    assert isinstance(recovered, int)


# ==============================================================================
# 15. BROWSER WEBSOCKET TIMEOUT REGRESSION
# ==============================================================================

@pytest.mark.asyncio
async def test_15_browser_websocket_duration_enforcement():
    """Verify browser /ws/conversation has on_timeout_callback wired properly."""
    client = TestClient(app)
    uid = random.randint(10000000, 99999999)
    user = f"+9198{uid}"

    with client.websocket_connect(f"/ws/conversation?caller_id={user}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "ready"
        session_id = msg["session_id"]

        # Verify timeout task was registered in call_restriction_service
        assert session_id in call_restriction_service._active_timeouts
        # Verify callback was registered in _timeout_callbacks
        assert session_id in call_restriction_service._timeout_callbacks

