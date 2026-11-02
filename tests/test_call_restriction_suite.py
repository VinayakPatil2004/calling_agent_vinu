"""
Comprehensive Automated Test Suite for CityLinc Phase 4.5:
User Call Restriction & Rate Limiting System.

Tests all mandatory requirements (A through U):
- Daily Limit Enforcement (Tests A, B, C, D)
- Missed / Failed / Busy Non-Increment (Tests E, F, G)
- Idempotent Webhook Handling (Test H)
- Concurrency & Race-Safety (Tests I, J)
- 12:00 AM Asia/Kolkata Reset & Timezone Verification (Tests K, L, M)
- 180s Call Duration Enforcement & Cleanup (Tests N, O, P, Q, R, U)
- Failure Handling & Server Restart Recovery (Tests S, T)
- Phone Normalization & Privacy Masking
- Telephony Inbound, Status, and Usage HTTP Endpoints
"""
import os
import sys
import time
import asyncio
import sqlite3
import pytest
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.app.services.phone_normalizer import (
    normalize_mobile_number,
    normalize_to_e164,
    mask_phone_number,
    hash_phone_number,
    is_valid_indian_mobile,
)
from backend.app.services.timezone_utils import (
    IST,
    get_utc_now,
    get_ist_now,
    get_ist_business_date,
)
from backend.app.database.call_storage import CallStorageEngine
from backend.app.services.call_restriction_service import (
    CallRestrictionService,
    DAILY_LIMIT_EXCEEDED_MESSAGE_MR,
    CallCheckResult,
    CallConnectResult,
    CallDisconnectResult,
)
from backend.app.main import app


# ==============================================================================
# 1. PHONE NORMALIZER & PRIVACY TESTS
# ==============================================================================

def test_phone_normalization_formats():
    """Verify various valid Indian phone formats normalize to canonical E.164 (+91XXXXXXXXXX)."""
    expected = "+919876543210"
    inputs = [
        "9876543210",
        "09876543210",
        "919876543210",
        "+919876543210",
        "+91 98765 43210",
        "098765-43210",
        "+91 (987) 654-3210",
    ]
    for inp in inputs:
        valid, normalized, err = normalize_mobile_number(inp)
        assert valid is True
        assert normalized == expected, f"Failed for input: {inp}"
        assert normalize_to_e164(inp) == expected
        assert is_valid_indian_mobile(inp) is True


def test_phone_normalization_invalid():
    """Verify invalid phone numbers are rejected."""
    invalid_inputs = [
        "",
        "   ",
        "12345",
        "98765",
        "1876543210",   # Starts with 1 (not 6-9)
        "4876543210",   # Starts with 4 (not 6-9)
        "abcdefghij",
        "+14155552671", # US number
        None,
    ]
    for inp in invalid_inputs:
        valid, normalized, err = normalize_mobile_number(inp)
        assert valid is False
        assert is_valid_indian_mobile(inp) is False
        with pytest.raises(ValueError):
            normalize_to_e164(inp)


def test_phone_masking_and_hashing():
    """Verify privacy masking and SHA-256 hashing."""
    phone = "+919876543210"
    masked = mask_phone_number(phone)
    assert masked == "+91XXXXXX3210"
    assert "987654" not in masked

    h = hash_phone_number(phone)
    assert len(h) == 64
    assert h == hash_phone_number("9876543210")  # Hashes canonical form


# ==============================================================================
# 2. TIMEZONE & IST RESET TESTS
# ==============================================================================

def test_timezone_utils():
    """Verify Asia/Kolkata timezone offset and date computation."""
    utc_now = get_utc_now()
    ist_now = get_ist_now()
    # IST must be exactly UTC + 5h 30m
    diff = ist_now.utcoffset()
    assert diff == timedelta(hours=5, minutes=30)


def test_ist_business_date_computation():
    """Verify date transitions exactly at midnight IST."""
    # 2026-11-02 23:59:59 IST -> Date is 2026-11-02
    t1 = datetime(2026, 11, 2, 23, 59, 59, tzinfo=IST)
    assert get_ist_business_date(t1) == "2026-11-02"

    # 2026-11-03 00:00:01 IST -> Date is 2026-11-03
    t2 = datetime(2026, 11, 3, 0, 0, 1, tzinfo=IST)
    assert get_ist_business_date(t2) == "2026-11-03"

    # Cross-check from UTC equivalent:
    # 2026-11-02 18:29:59 UTC = 2026-11-02 23:59:59 IST -> 2026-11-02
    utc_pre_midnight = datetime(2026, 11, 2, 18, 29, 59, tzinfo=timezone.utc)
    assert get_ist_business_date(utc_pre_midnight) == "2026-11-02"

    # 2026-11-02 18:30:01 UTC = 2026-11-03 00:00:01 IST -> 2026-11-03
    utc_post_midnight = datetime(2026, 11, 2, 18, 30, 1, tzinfo=timezone.utc)
    assert get_ist_business_date(utc_post_midnight) == "2026-11-03"


# ==============================================================================
# 3. DAILY LIMIT ENFORCEMENT (TESTS A, B, C, D)
# ==============================================================================

@pytest.mark.asyncio
async def test_a_b_c_d_daily_limit_enforcement(tmp_path):
    """
    Test A: Call 1 from User A -> Allowed, daily count = 1.
    Test B: Call 2 from User A -> Allowed, daily count = 2.
    Test C: Call 3 from User A -> Allowed, daily count = 3.
    Test D: Call 4 from User A -> Rejected with Busy / Limit message, daily count remains 3.
    """
    db_file = tmp_path / "test_calls.db"
    storage = CallStorageEngine(db_path=db_file)
    service = CallRestrictionService(storage=storage)

    user_a = "+919876500001"

    # Call 1 (Test A)
    res1 = await service.register_connected_call(provider_call_id="call_1", caller_phone=user_a)
    assert res1.allowed is True
    assert res1.connected_count == 1
    assert res1.remaining_calls == 2

    # Call 2 (Test B)
    res2 = await service.register_connected_call(provider_call_id="call_2", caller_phone=user_a)
    assert res2.allowed is True
    assert res2.connected_count == 2
    assert res2.remaining_calls == 1

    # Call 3 (Test C)
    res3 = await service.register_connected_call(provider_call_id="call_3", caller_phone=user_a)
    assert res3.allowed is True
    assert res3.connected_count == 3
    assert res3.remaining_calls == 0

    # Call 4 (Test D)
    res4 = await service.register_connected_call(provider_call_id="call_4", caller_phone=user_a)
    assert res4.allowed is False
    assert res4.connected_count == 3
    assert res4.remaining_calls == 0
    assert res4.status == "daily_limit_exceeded"
    assert res4.message == DAILY_LIMIT_EXCEEDED_MESSAGE_MR

    # Verify check_daily_call_limit also returns rejected
    check = service.check_daily_call_limit(user_a)
    assert check.allowed is False
    assert check.connected_count == 3
    assert check.remaining_calls == 0
    assert check.message == DAILY_LIMIT_EXCEEDED_MESSAGE_MR


# ==============================================================================
# 4. MISSED / FAILED / BUSY NON-INCREMENT (TESTS E, F, G)
# ==============================================================================

@pytest.mark.asyncio
async def test_e_f_g_non_connected_calls_do_not_increment(tmp_path):
    """
    Test E: Call placed, user hangs up before answer -> daily count does NOT increment.
    Test F: Telephony provider reports failed call -> daily count does NOT increment.
    Test G: Busy / line unavailable -> daily count does NOT increment.
    """
    db_file = tmp_path / "test_calls.db"
    storage = CallStorageEngine(db_path=db_file)
    service = CallRestrictionService(storage=storage)

    user_phone = "+919876500002"

    # Pre-check: 0 calls
    check0 = service.check_daily_call_limit(user_phone)
    assert check0.connected_count == 0
    assert check0.allowed is True

    # Record incoming attempt
    caller_hash = hash_phone_number(user_phone)
    storage.record_incoming_attempt("call_missed", caller_hash, mask_phone_number(user_phone), get_ist_business_date())

    # Test E: User hangs up before answer (canceled / no-answer)
    storage.record_call_termination(
        provider_call_id="call_missed",
        termination_reason="canceled"
    )
    assert storage.get_connected_count(caller_hash, get_ist_business_date()) == 0

    # Test F: Telephony provider reports failed call
    storage.record_incoming_attempt("call_failed", caller_hash, mask_phone_number(user_phone), get_ist_business_date())
    storage.record_call_termination(
        provider_call_id="call_failed",
        termination_reason="provider_failure"
    )
    assert storage.get_connected_count(caller_hash, get_ist_business_date()) == 0

    # Test G: Busy / line unavailable
    storage.record_incoming_attempt("call_busy", caller_hash, mask_phone_number(user_phone), get_ist_business_date())
    storage.record_call_termination(
        provider_call_id="call_busy",
        termination_reason="user_busy"
    )
    assert storage.get_connected_count(caller_hash, get_ist_business_date()) == 0

    # Confirm user still has 3 full remaining calls
    check_final = service.check_daily_call_limit(user_phone)
    assert check_final.connected_count == 0
    assert check_final.remaining_calls == 3
    assert check_final.allowed is True


# ==============================================================================
# 5. DUPLICATE WEBHOOKS / IDEMPOTENCY (TEST H)
# ==============================================================================

@pytest.mark.asyncio
async def test_h_duplicate_connected_webhook_idempotency(tmp_path):
    """
    Test H: Telephony provider sends duplicate 'connected' webhook for Call 1
    -> daily count increments ONLY ONCE.
    """
    db_file = tmp_path / "test_calls.db"
    storage = CallStorageEngine(db_path=db_file)
    service = CallRestrictionService(storage=storage)

    user_phone = "+919876500003"

    # First 'connected' webhook
    res1 = await service.register_connected_call(provider_call_id="unique_call_101", caller_phone=user_phone)
    assert res1.allowed is True
    assert res1.connected_count == 1
    assert res1.status == "connected"

    # Second duplicate 'connected' webhook for the same provider_call_id
    res2 = await service.register_connected_call(provider_call_id="unique_call_101", caller_phone=user_phone)
    assert res2.allowed is True
    assert res2.connected_count == 1  # Still 1, NOT 2
    assert res2.status == "already_registered"

    # Third duplicate webhook
    res3 = await service.register_connected_call(provider_call_id="unique_call_101", caller_phone=user_phone)
    assert res3.allowed is True
    assert res3.connected_count == 1
    assert res3.status == "already_registered"


# ==============================================================================
# 6. CONCURRENCY & RACE-SAFETY (TESTS I, J)
# ==============================================================================

@pytest.mark.asyncio
async def test_i_j_concurrency_race_safety(tmp_path):
    """
    Test I: User A initiates two simultaneous calls when daily count is 2
    -> Exactly one connects (becomes Call 3), the other is rejected (would be Call 4).
    Test J: User A (at limit 3) and User B (at count 0) call concurrently
    -> User A rejected, User B connected.
    """
    db_file = tmp_path / "test_calls.db"
    storage = CallStorageEngine(db_path=db_file)
    service = CallRestrictionService(storage=storage)

    user_a = "+919876500004"
    user_b = "+919876500005"

    # User A performs 2 calls first
    await service.register_connected_call("call_a_1", user_a)
    await service.register_connected_call("call_a_2", user_a)

    # Test I: User A launches 2 concurrent calls (call_a_3a, call_a_3b)
    # Both try to claim the remaining 1 slot
    results = await asyncio.gather(
        service.register_connected_call("call_a_3a", user_a),
        service.register_connected_call("call_a_3b", user_a)
    )

    allowed_count = sum(1 for r in results if r.allowed)
    rejected_count = sum(1 for r in results if not r.allowed)

    assert allowed_count == 1, f"Expected exactly 1 allowed call, got {allowed_count}"
    assert rejected_count == 1, f"Expected exactly 1 rejected call, got {rejected_count}"

    # Verify total daily count for User A is strictly 3
    check_a = service.check_daily_call_limit(user_a)
    assert check_a.connected_count == 3
    assert check_a.allowed is False

    # Test J: User A (at count 3) and User B (at count 0) call concurrently
    results_j = await asyncio.gather(
        service.register_connected_call("call_a_late", user_a),
        service.register_connected_call("call_b_1", user_b)
    )

    res_user_a = results_j[0]
    res_user_b = results_j[1]

    assert res_user_a.allowed is False
    assert res_user_a.connected_count == 3
    assert res_user_a.message == DAILY_LIMIT_EXCEEDED_MESSAGE_MR

    assert res_user_b.allowed is True
    assert res_user_b.connected_count == 1
    assert res_user_b.remaining_calls == 2


# ==============================================================================
# 7. RESET AT 12:00 AM ASIA/KOLKATA (TESTS K, L, M)
# ==============================================================================

@pytest.mark.asyncio
async def test_k_l_m_midnight_ist_reset(tmp_path):
    """
    Test K: Call 3 at 11:59:59 PM IST -> Allowed.
    Test L: Call 4 attempted at 12:00:01 AM IST -> Allowed (new day, count = 1).
    Test M: Verify reset uses Asia/Kolkata time, not UTC or server local time.
    """
    db_file = tmp_path / "test_calls.db"
    storage = CallStorageEngine(db_path=db_file)
    service = CallRestrictionService(storage=storage)

    user = "+919876500006"

    # Day 1 IST Date: 2026-11-02
    day1_str = "2026-11-02"
    # Day 2 IST Date: 2026-11-03
    day2_str = "2026-11-03"

    # Call 1 on Day 1
    r1 = await service.register_connected_call("call_d1_1", user, ist_date=day1_str)
    assert r1.allowed is True
    assert r1.connected_count == 1

    # Call 2 on Day 1
    r2 = await service.register_connected_call("call_d1_2", user, ist_date=day1_str)
    assert r2.allowed is True
    assert r2.connected_count == 2

    # Test K: Call 3 on Day 1 (11:59:59 PM IST) -> Allowed
    r3 = await service.register_connected_call("call_d1_3", user, ist_date=day1_str)
    assert r3.allowed is True
    assert r3.connected_count == 3

    # 4th call on Day 1 -> Rejected
    r4 = await service.register_connected_call("call_d1_4", user, ist_date=day1_str)
    assert r4.allowed is False
    assert r4.connected_count == 3

    # Test L: Call attempted at 12:00:01 AM IST (Day 2) -> Allowed (new day, count = 1)
    check_d2 = service.check_daily_call_limit(user, ist_date=day2_str)
    assert check_d2.allowed is True
    assert check_d2.connected_count == 0
    assert check_d2.remaining_calls == 3

    r_new_day = await service.register_connected_call("call_d2_1", user, ist_date=day2_str)
    assert r_new_day.allowed is True
    assert r_new_day.connected_count == 1
    assert r_new_day.remaining_calls == 2

    # Test M: Verify UTC 18:29:59 is Day 1 (23:59:59 IST) and UTC 18:30:00 is Day 2 (00:00:00 IST)
    # Ensure system does not use UTC date (which would be 2026-11-02 for both)
    utc_time_1829 = datetime(2026, 11, 2, 18, 29, 59, tzinfo=timezone.utc)
    utc_time_1830 = datetime(2026, 11, 2, 18, 30, 0, tzinfo=timezone.utc)
    d1 = get_ist_business_date(utc_time_1829)
    d2 = get_ist_business_date(utc_time_1830)
    assert d1 == "2026-11-02"
    assert d2 == "2026-11-03", "At 18:30 UTC, IST date must have rolled over to next day Nov 3"


# ==============================================================================
# 8. CALL DURATION ENFORCEMENT & CLEANUP (TESTS N, O, P, Q, R, U)
# ==============================================================================

@pytest.mark.asyncio
async def test_n_o_call_duration_timeout_enforcement(tmp_path):
    """
    Test N: Call active at < 180 seconds -> Still connected.
    Test O: Call reaches 180 seconds -> Server triggers automatic disconnect.
    """
    db_file = tmp_path / "test_calls.db"
    storage = CallStorageEngine(db_path=db_file)
    service = CallRestrictionService(storage=storage)

    user = "+919876500007"
    call_id = "call_duration_test"

    timeout_fired = False

    async def on_timeout(cid):
        nonlocal timeout_fired
        timeout_fired = True

    # Register connected call
    res = await service.register_connected_call(call_id, user, on_timeout_callback=on_timeout)
    assert res.allowed is True

    # Reschedule short timeout of 0.2s for deterministic fast execution
    service.schedule_call_timeout(call_id, timeout_seconds=0.2)

    # Test N: Before timeout
    assert timeout_fired is False
    assert call_id in service._active_timeouts

    # Test O: Wait for timeout to fire
    await asyncio.sleep(0.3)
    assert timeout_fired is True
    assert call_id not in service._active_timeouts

    # Verify call is recorded as timeout with max_duration_180s
    rec = storage.get_call_record(call_id)
    assert rec["status"] == "timeout"
    assert rec["termination_reason"] == "max_duration_180s"


@pytest.mark.asyncio
async def test_p_q_user_disconnect_and_idempotency(tmp_path):
    """
    Test P: Call disconnected by user at 60 seconds -> Session cleaned up, duration recorded, daily count unaffected.
    Test Q: Duplicate disconnect webhook for same call -> Idempotent, no error.
    """
    db_file = tmp_path / "test_calls.db"
    storage = CallStorageEngine(db_path=db_file)
    service = CallRestrictionService(storage=storage)

    user = "+919876500008"
    call_id = "call_user_hangup"

    await service.register_connected_call(call_id, user)
    service.schedule_call_timeout(call_id, timeout_seconds=60)

    # User hangs up
    res = service.register_call_disconnect(call_id, termination_reason="normal_hangup")
    assert res.success is True
    assert res.status == "terminated"
    assert res.termination_reason == "normal_hangup"
    assert call_id not in service._active_timeouts

    # Daily count is still 1
    caller_hash = hash_phone_number(user)
    assert storage.get_connected_count(caller_hash, get_ist_business_date()) == 1

    # Test Q: Duplicate disconnect webhook
    res_dup = service.register_call_disconnect(call_id, termination_reason="normal_hangup")
    assert res_dup.success is True
    assert res_dup.status == "already_terminated"
    assert storage.get_connected_count(caller_hash, get_ist_business_date()) == 1


@pytest.mark.asyncio
async def test_r_websocket_disconnect_cleans_up():
    """
    Test R: Browser / WebSocket disconnect before 180 seconds -> Server cleans up timeout task.
    """
    import random
    client = TestClient(app)
    user_phone = f"+9198{random.randint(10000000, 99999999)}"

    with client.websocket_connect(f"/ws/conversation?caller_id={user_phone}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "ready"
        session_id = msg["session_id"]

        from backend.app.services.call_restriction_service import call_restriction_service
        # Timer should be scheduled for this session_id
        assert session_id in call_restriction_service._active_timeouts

    # Disconnect happened when exiting 'with' block
    await asyncio.sleep(0.05)
    from backend.app.services.call_restriction_service import call_restriction_service
    assert session_id not in call_restriction_service._active_timeouts


@pytest.mark.asyncio
async def test_u_telephony_disconnect_fails_to_arrive_server_terminates(tmp_path):
    """
    Test U: Telephony disconnect webhook fails to arrive -> Server-side 180s timer still terminates call.
    """
    db_file = tmp_path / "test_calls.db"
    storage = CallStorageEngine(db_path=db_file)
    service = CallRestrictionService(storage=storage)

    user = "+919876500010"
    call_id = "call_missing_disconnect"

    await service.register_connected_call(call_id, user)
    # Short timer simulating 180s expiration
    service.schedule_call_timeout(call_id, timeout_seconds=0.2)

    # Disconnect webhook never arrives
    await asyncio.sleep(0.3)

    rec = storage.get_call_record(call_id)
    assert rec["status"] == "timeout"
    assert rec["termination_reason"] == "max_duration_180s"


# ==============================================================================
# 9. FAILURE HANDLING & SERVER RESTART RECOVERY (TESTS S, T)
# ==============================================================================

@pytest.mark.asyncio
async def test_s_database_failure_fails_closed():
    """
    Test S: Database connection lost during call check -> System fails closed (does NOT allow call).
    """
    bad_storage = MagicMock()
    bad_storage.atomic_check_and_register_connected.side_effect = sqlite3.OperationalError("database is locked")
    bad_storage.get_connected_count.side_effect = sqlite3.OperationalError("disk I/O error")

    service = CallRestrictionService(storage=bad_storage)
    user = "+919876500011"

    # Pre-call check should fail closed
    check = service.check_daily_call_limit(user)
    assert check.allowed is False
    assert check.status == "service_unavailable"

    # Registration should fail closed
    reg = await service.register_connected_call("call_fail_db", user)
    assert reg.allowed is False
    assert reg.status in ("service_unavailable", "database_error")


@pytest.mark.asyncio
async def test_t_server_restart_recovery(tmp_path):
    """
    Test T: Server crashes and restarts while call is active
    -> On recovery, past deadline call is marked terminated; future deadline resumes timer.
    """
    db_file = tmp_path / "test_calls.db"
    storage = CallStorageEngine(db_path=db_file)

    user = "+919876500012"
    today = get_ist_business_date()
    now_utc = get_utc_now()

    # Call 1: connected in past, deadline already passed (e.g. 5 minutes ago)
    past_connected = (now_utc - timedelta(seconds=400)).isoformat()
    past_deadline = (now_utc - timedelta(seconds=220)).isoformat()
    storage.atomic_check_and_register_connected(
        provider_call_id="call_past_expired",
        caller_hash=hash_phone_number(user),
        caller_masked=mask_phone_number(user),
        ist_date=today,
        duration_limit_sec=180
    )
    # Manually update deadline to past
    with storage._get_connection() as conn:
        conn.execute(
            "UPDATE call_records SET deadline_at=?, connected_at=? WHERE provider_call_id='call_past_expired'",
            (past_deadline, past_connected)
        )

    # Call 2: connected recently, deadline in the future (e.g. 100 seconds from now)
    future_connected = (now_utc - timedelta(seconds=80)).isoformat()
    future_deadline = (now_utc + timedelta(seconds=100)).isoformat()
    storage.atomic_check_and_register_connected(
        provider_call_id="call_future_active",
        caller_hash=hash_phone_number(user),
        caller_masked=mask_phone_number(user),
        ist_date=today,
        duration_limit_sec=180
    )
    with storage._get_connection() as conn:
        conn.execute(
            "UPDATE call_records SET deadline_at=?, connected_at=? WHERE provider_call_id='call_future_active'",
            (future_deadline, future_connected)
        )

    # Simulate server restart by creating a new Service instance pointing to same DB
    new_service = CallRestrictionService(storage=storage)
    recovered_count = new_service.recover_active_call_timeouts()

    assert recovered_count == 2

    # Call 1 should have been expired immediately
    rec1 = storage.get_call_record("call_past_expired")
    assert rec1["status"] == "timeout"
    assert rec1["termination_reason"] == "max_duration_180s"

    # Call 2 should have been resumed
    assert "call_future_active" in new_service._active_timeouts

    # Clean up timer
    new_service.cancel_call_timeout("call_future_active")


# ==============================================================================
# 10. TELEPHONY WEBHOOK HTTP ENDPOINTS TESTS
# ==============================================================================

def test_telephony_inbound_and_status_endpoints():
    """
    Verify /api/telephony/inbound, /api/telephony/status, and /api/telephony/usage HTTP endpoints.
    """
    import random
    client = TestClient(app)
    uid = random.randint(10000000, 99999999)
    user = f"+9198{uid}"
    c1, c2, c3, c4 = f"tel_{uid}_1", f"tel_{uid}_2", f"tel_{uid}_3", f"tel_{uid}_4"

    # Inbound check before any calls
    res_inbound_1 = client.post("/api/telephony/inbound", json={"caller_id": user, "call_id": c1})
    assert res_inbound_1.status_code == 200
    assert res_inbound_1.json()["action"] == "connect"

    # Simulate Call 1 answered
    res_stat_1 = client.post("/api/telephony/status", json={"call_id": c1, "caller_id": user, "call_status": "answered"})
    assert res_stat_1.status_code == 200
    assert res_stat_1.json()["allowed"] is True
    assert res_stat_1.json()["count"] == 1

    # Simulate Call 2 answered
    res_stat_2 = client.post("/api/telephony/status", json={"call_id": c2, "caller_id": user, "call_status": "answered"})
    assert res_stat_2.json()["allowed"] is True
    assert res_stat_2.json()["count"] == 2

    # Simulate Call 3 answered
    res_stat_3 = client.post("/api/telephony/status", json={"call_id": c3, "caller_id": user, "call_status": "answered"})
    assert res_stat_3.json()["allowed"] is True
    assert res_stat_3.json()["count"] == 3

    # Now 4th inbound call attempt -> must be rejected with Marathi limit message
    res_inbound_4 = client.post("/api/telephony/inbound", json={"caller_id": user, "call_id": c4})
    assert res_inbound_4.status_code == 200
    data_4 = res_inbound_4.json()
    assert data_4["action"] == "reject"
    assert data_4["reason"] == "busy"
    assert data_4["message"] == DAILY_LIMIT_EXCEEDED_MESSAGE_MR

    # Query usage quota
    res_usage = client.get(f"/api/telephony/usage?phone={user}")
    assert res_usage.status_code == 200
    usage_data = res_usage.json()
    assert usage_data["connected_today"] == 3
    assert usage_data["remaining_calls"] == 0
    assert usage_data["allowed"] is False
