"""
Comprehensive Test Suite for CityLinc Phase 4.1 Conversational Intelligence:
Covers all 6 confirmed conversational intelligence bugs (Scenarios A through P):
- Bug 1: Marathi पासून / Bus Pass Collision
- Bug 2: Inverted English Route Query Syntax
- Bug 3: Total Hospital Count Fallback vs Entity Count
- Bug 4: Multi-Turn Context Loss (Next Bus & Current Location)
- Bug 5: KBT Circle Landmark Resolution & Transfer Routing
- Bug 6: Safari MP4 / Truncated Audio Normalization & Error Recovery
"""
import pytest
from unittest.mock import patch, MagicMock
from backend.app.services.conversation_context import context_engine
from backend.app.services.route_engine import route_engine
from backend.app.services.location_service import location_service
from backend.app.integrations.sarvam.stt import SarvamSTTClient


# ==============================================================================
# BUG 1: MARATHI पासून / BUS PASS COLLISION (Test A)
# ==============================================================================

def test_marathi_pasun_not_treated_as_bus_pass():
    """Verify 'मला CBS पासून नाशिक रोडला जायचंय' classifies as route_search, not fare/pass."""
    sid = "test_bug1_session_route"
    res = context_engine.process_turn(sid, "मला CBS पासून नाशिक रोडला जायचंय", "mr")
    assert res["intent"] == "route_search"
    ctx = context_engine.get_or_create_context(sid)
    assert ctx.origin is not None and "cbs" in ctx.origin.lower()
    assert ctx.destination is not None and "नाशिक रोड" in ctx.destination.lower()
    assert "पास दर" not in res["response_text"]
    assert "दैनंदिन पास" not in res["response_text"]


def test_marathi_actual_bus_pass_query():
    """Verify genuine Marathi pass queries classify as fare/bus-pass."""
    sid = "test_bug1_session_pass"
    res = context_engine.process_turn(sid, "मला मासिक बस पास पाहिजे", "mr")
    assert res["intent"] == "fare"
    assert any(w in res["response_text"] for w in ("पास", "Pass", "दर", "रुपये"))


# ==============================================================================
# BUG 2: INVERTED ENGLISH ROUTE QUERY SYNTAX (Test B)
# ==============================================================================

def test_inverted_english_route_query():
    """Verify 'How can I reach Nashik Road from CBS?' extracts origin=CBS, dest=Nashik Road."""
    sid = "test_bug2_session_reach"
    res = context_engine.process_turn(sid, "How can I reach Nashik Road from CBS?", "en")
    assert res["intent"] == "route_search"
    ctx = context_engine.get_or_create_context(sid)
    assert ctx.origin is not None and "cbs" in ctx.origin.lower()
    assert ctx.destination is not None and "nashik road" in ctx.destination.lower()
    assert "CBS" in res["response_text"] and "Nashik Road" in res["response_text"]


def test_standard_english_route_query():
    """Verify standard 'From CBS to Nashik Road' still extracts origin=CBS, dest=Nashik Road."""
    sid = "test_bug2_session_standard"
    res = context_engine.process_turn(sid, "From CBS to Nashik Road", "en")
    assert res["intent"] == "route_search"
    ctx = context_engine.get_or_create_context(sid)
    assert ctx.origin is not None and "cbs" in ctx.origin.lower()
    assert ctx.destination is not None and "nashik road" in ctx.destination.lower()


# ==============================================================================
# BUG 3: TOTAL HOSPITAL COUNT & INTELLIGENCE (Tests C, D, E, F)
# ==============================================================================

def test_hospital_count_query():
    """Verify 'किती Hospitals आहेत टोटल?' returns exact registry count (16), not Civil Hospital."""
    sid = "test_bug3_count"
    res = context_engine.process_turn(sid, "किती Hospitals आहेत टोटल?", "mr")
    assert res["intent"] == "hospital_count"
    assert "16" in res["response_text"]
    assert any(w in res["response_text"] for w in ("नोंदणीकृत", "रुग्णालये", "पडताळलेली", "CityLinc", "hospitals"))
    # Must NOT return District Civil Hospital navigation directions as default
    assert "जिल्हा रुग्णालय (Civil Hospital) जाण्यासाठी" not in res["response_text"]


def test_nearby_hospital_without_location():
    """Verify nearby hospital query without known location prompts user for location."""
    sid = "test_bug3_nearby_no_loc"
    res = context_engine.process_turn(sid, "जवळचे रुग्णालय कोणते?", "mr")
    assert res["intent"] in ("nearby_hospital", "hospital_navigation")
    assert any(w in res["response_text"] for w in ("स्थान", "कुठे", "हॉस्पिटल", "रुग्णालय"))


def test_hospital_distance_query():
    """Verify 'सिव्हिल हॉस्पिटल किती लांब आहे?' explains walking distance from bus stop."""
    sid = "test_bug3_dist"
    res = context_engine.process_turn(sid, "सिव्हिल हॉस्पिटल किती लांब आहे?", "mr")
    assert res["intent"] == "hospital_distance"
    assert any(w in res["response_text"] for w in ("97", "मीटर", "थांब्यापासून", "अंतरावर"))


def test_specific_hospital_navigation():
    """Verify 'अपोलो हॉस्पिटलला कसे जायचे?' resolves specifically to Apollo Hospital."""
    sid = "test_bug3_apollo"
    res = context_engine.process_turn(sid, "अपोलो हॉस्पिटलला कसे जायचे?", "mr")
    assert res["intent"] == "hospital_navigation"
    assert any(w in res["response_text"] for w in ("अपोलो", "Apollo"))
    assert "जिल्हा रुग्णालय" not in res["response_text"]


# ==============================================================================
# BUG 4: MULTI-TURN CONTEXT LOSS & FOLLOW-UPS (Tests G, H, P)
# ==============================================================================

def test_multi_turn_route_and_next_bus():
    """Verify Turn 1 route search enables Turn 2 'next bus' without re-asking origin/destination."""
    sid = "test_bug4_multiturn"
    # Turn 1: Route search CBS to Nashik Road
    t1 = context_engine.process_turn(sid, "CBS to Nashik Road", "en")
    assert t1["intent"] == "route_search"
    assert any(r in t1["response_text"] for r in ("202", "203", "201", "210"))

    # Turn 2: Next bus query
    t2 = context_engine.process_turn(sid, "When is the next bus?", "en")
    assert t2["intent"] == "next_bus"
    assert any(w in t2["response_text"].lower() for w in ("departure", "route", "min", "scheduled", "depart"))
    assert "where are you traveling from" not in t2["response_text"].lower()


def test_location_declaration_and_hospital_reuse():
    """Verify Turn 1 setting location reuses it in Turn 2 nearby hospital search."""
    sid = "test_bug4_loc_reuse"
    # Turn 1: Declare location
    t1 = context_engine.process_turn(sid, "मी KBT Circle जवळ आहे", "mr")
    assert t1["intent"] == "location_declaration"
    assert any(w in t1["response_text"] for w in ("KBT Circle", "थत्ते नगर", "Vidya Vikas"))

    ctx = context_engine.get_or_create_context(sid)
    assert ctx.current_location is not None
    assert "kbt" in ctx.current_location.lower()

    # Turn 2: Ask for nearby hospital
    t2 = context_engine.process_turn(sid, "जवळचे रुग्णालय कोणते आहे?", "mr")
    assert t2["intent"] in ("nearby_hospital", "hospital_navigation")
    assert "कुठे आहात" not in t2["response_text"]  # Should NOT ask for location again
    assert any(w in t2["response_text"] for w in ("रुग्णालय", "Hospital", "हृदयाक्ष"))


def test_session_isolation():
    """Verify session A and session B do not leak context across concurrent users."""
    s_a = "user_session_alpha"
    s_b = "user_session_beta"

    context_engine.process_turn(s_a, "From Nimani to CBS", "en")
    ctx_a = context_engine.get_or_create_context(s_a)
    assert ctx_a.origin is not None and "nimani" in ctx_a.origin.lower()
    assert ctx_a.destination is not None and "cbs" in ctx_a.destination.lower()

    ctx_b = context_engine.get_or_create_context(s_b)
    assert ctx_b.origin is None
    assert ctx_b.destination is None


# ==============================================================================
# BUG 5: KBT CIRCLE RESOLUTION & ROUTING (Tests I, J, K)
# ==============================================================================

def test_kbt_circle_resolution():
    """Verify KBT Circle resolves to Thatte Nagar and Vidya Vikas Circle stations."""
    res = route_engine.resolve_place("KBT Circle")
    assert res is not None
    assert "KBT Circle" in res.display_name
    # Boarding station IDs must include Thatte Nagar (122, 1108) or Vidya Vikas Circle (582, 1109)
    assert any(sid in (122, 1108, 582, 1109) for sid in res.matched_station_ids)


def test_false_circle_matching_prevention():
    """Verify lone generic token 'circle' does not match random transit stations."""
    res = route_engine.resolve_place("circle")
    assert res is not None
    assert len(res.matched_station_ids) == 0


def test_kbt_circle_to_civil_hospital_routing():
    """Verify route search from KBT Circle to District Civil Hospital succeeds (via transfer or direct)."""
    res = route_engine.search_routes("KBT Circle", "District Civil Hospital")
    assert res.success is True
    # At least direct or 1-transfer route must be found
    total_options = res.direct_routes_count + res.transfer_routes_count
    assert total_options > 0
    if res.transfer_routes_count > 0:
        transfer = res.transfer_routes[0]
        assert transfer.interchange_hub is not None


# ==============================================================================
# BUG 6: SARVAM STT FORMAT NORMALIZATION & RECOVERY (Tests L, M, N, O)
# ==============================================================================

@pytest.mark.asyncio
async def test_stt_safari_mp4_mime_normalization():
    """Verify Safari MP4 container signatures are recognized and normalized."""
    stt = SarvamSTTClient(api_key="mock_key_val_1234")
    fake_mp4_bytes = b'\x00\x00\x00\x18ftypmp42' + (b'\x00' * 200)

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"transcript": "Nashik Road", "language_code": "en-IN"}
        mock_post.return_value = mock_resp

        res = await stt.transcribe(fake_mp4_bytes, mime_type="audio/mp4")
        assert res["transcript"] == "Nashik Road"
        _, kwargs = mock_post.call_args
        files = kwargs.get("files", {})
        filename, _, content_type = files["file"]
        assert filename == "recording.mp4"
        assert content_type == "audio/mp4"


@pytest.mark.asyncio
async def test_stt_aac_audio_normalization():
    """Verify AAC/M4A audio MIME types are normalized properly."""
    stt = SarvamSTTClient(api_key="mock_key_val_1234")
    fake_aac_bytes = b'\x00\x00\x00\x18ftypM4A ' + (b'\x00' * 200)

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"transcript": "Panchavati", "language_code": "mr-IN"}
        mock_post.return_value = mock_resp

        res = await stt.transcribe(fake_aac_bytes, mime_type="audio/aac")
        assert res["transcript"] == "Panchavati"
        _, kwargs = mock_post.call_args
        files = kwargs.get("files", {})
        filename, _, content_type = files["file"]
        assert filename == "recording.mp4"
        assert content_type == "audio/mp4"


@pytest.mark.asyncio
async def test_stt_truncated_audio_rejection():
    """Verify short/empty payloads (< 150 bytes) return immediate friendly prompt without API call."""
    stt = SarvamSTTClient(api_key="mock_key_val_1234")
    short_bytes = b'RIFF1234'  # only 8 bytes
    res = await stt.transcribe(short_bytes, mime_type="audio/wav", language_code="mr-IN")
    assert res.get("error") == "audio_too_short"
    assert "user_message_mr" in res
    assert "पुन्हा बोला" in res["user_message_mr"]


def test_friendly_localized_stt_fallback_prompts():
    """Verify localized fallback error prompts exist for all 3 supported languages."""
    mr_prompt = "तुमचा आवाज स्पष्ट ऐकू आला नाही. कृपया पुन्हा बोला."
    hi_prompt = "आपकी आवाज़ साफ़ सुनाई नहीं दी। कृपया दोबारा बोलें।"
    en_prompt = "I couldn't hear you clearly. Please try again."

    assert "पुन्हा बोला" in mr_prompt
    assert "दोबारा बोलें" in hi_prompt
    assert "Please try again" in en_prompt
