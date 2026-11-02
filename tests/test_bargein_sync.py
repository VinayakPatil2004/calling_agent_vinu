"""
Targeted tests for Audio Playback, Barge-In Synchronization, and Turn Tracking.
"""
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.orchestrator.conversation_manager import conversation_manager


def test_bargein_turn_synchronization():
    """Verify turn id synchronization and idempotency during interruption."""
    session = conversation_manager.get_or_create_session("sync_test_session")
    session.reset()

    # User starts turn 1
    t1 = session.add_user_message("Query 1", language="mr-IN")
    assert t1 == 1
    assert session.current_turn_id == 1
    assert session.is_interrupted is False

    # Genuine barge-in for turn 1
    t_inter = session.interrupt(turn_id=1)
    assert t_inter == 2
    assert session.is_interrupted is True

    # Redundant barge-in event for already-interrupted turn 1 should be idempotent
    t_inter_dup = session.interrupt(turn_id=1)
    assert t_inter_dup == 2
    assert session.current_turn_id == 2

    # User starts turn 2
    t2 = session.add_user_message("Query 2", language="mr-IN")
    assert t2 == 3
    assert session.is_interrupted is False


def test_websocket_audio_format_and_bargein_flow():
    """Verify WebSocket emits standard audio/mpeg format and handles turn-specific interruptions."""
    client = TestClient(app)
    with client.websocket_connect("/ws/conversation") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert "has_sarvam_key" in ready

        # Send text message to trigger full response & audio generation
        ws.send_json({
            "type": "text",
            "text": "विद्यार्थी पास ची माहिती द्या",
            "language": "mr-IN"
        })

        # Receive user transcript
        msg1 = ws.receive_json()
        assert msg1["type"] == "transcript"
        assert msg1["role"] == "user"

        # Receive thinking status
        msg2 = ws.receive_json()
        assert msg2["type"] == "status"
        assert msg2["status"] == "thinking"

        # Receive assistant transcript
        msg3 = ws.receive_json()
        assert msg3["type"] == "transcript"
        assert msg3["role"] == "assistant"

        # Receive speaking status
        msg4 = ws.receive_json()
        assert msg4["type"] == "status"
        assert msg4["status"] == "speaking"

        # Receive audio payload
        audio_msg = ws.receive_json()
        assert audio_msg["type"] == "audio"
        assert audio_msg["format"] in ("audio/mpeg", "audio/wav")
        assert "turn_id" in audio_msg
        current_turn = audio_msg["turn_id"]

        # Send targeted interruption for this turn
        ws.send_json({
            "type": "interrupt",
            "turn_id": current_turn
        })

        # Server acknowledges interruption
        inter_resp = ws.receive_json()
        assert inter_resp["type"] == "interrupted"
        assert inter_resp["status"] == "listening"

