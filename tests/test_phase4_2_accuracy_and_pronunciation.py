"""
Unit and Integration Regression Suite for CityLinc Voice Agent Phase 4.2.
Verifies fixes for 5 core bugs:
1. Bug 1: Conversation Context Management & Stale Journey Memory Invalidation.
2. Bug 2: Route Accuracy Engine (Anandavalli, CCM, ABT Circle, Dwarka, no false Route 101).
3. Bug 3: Geodesic Hospital Ranking across all 7 test locations (no default MVP).
4. Bug 4: Multilingual Number Pronunciation & 'आकडे मराठीत बोल' Intent.
5. Bug 5: Timetable Departure Accuracy against active journey.
"""
import pytest
import re
from typing import Dict, Any

from backend.app.services.conversation_context import context_engine
from backend.app.services.route_engine import route_engine, GENERIC_TRANSIT_TOKENS
from backend.app.services.location_service import location_service
from backend.app.services.timetable_engine import timetable_engine
from backend.app.services.number_formatter import (
    format_route_number,
    format_time_natural,
    format_currency,
    format_text_for_speech,
    number_to_marathi_words,
    number_to_hindi_words,
    number_to_english_words
)


# ==============================================================================
# BUG 1 & BUG 5 TESTS: CONTEXT REPLACEMENT & TIMETABLE ACCURACY
# ==============================================================================

def test_context_replacement_and_stale_memory_prevention():
    """
    Verifies that a new journey query completely replaces prior journey context.
    Scenario:
      Turn 1: CBS -> Nashik Road (Route 202)
      Turn 2: KBT Circle -> Nimani (Route 131 / 331)
      Turn 3: 'पुढची बस किती वाजता आहे?'
    Must query departures for the new route (131/331), NEVER Route 202.
    """
    sid = "test_phase4_2_context_replacement"
    context_engine.clear_context(sid)

    # Turn 1: CBS -> Nashik Road
    t1 = context_engine.process_turn(sid, "मला CBS पासून नाशिक रोडला जायचंय", language="mr-IN")
    assert t1["handled_deterministically"] is True
    assert t1["intent"] == "route_search"
    ctx1 = context_engine.sessions[sid]
    assert ctx1.selected_rno == "202"

    # Turn 2: KBT Circle -> Nimani
    t2 = context_engine.process_turn(sid, "मला केबीटी सर्कल पासून निमाणीला जायचंय", language="mr-IN")
    assert t2["handled_deterministically"] is True
    assert t2["intent"] == "route_search"
    ctx2 = context_engine.sessions[sid]
    # Active route must NOT be 202
    assert ctx2.selected_rno != "202"
    assert ctx2.selected_rno in ("131", "331", "331A", "331B", "331S")
    assert "Nimani" in (ctx2.destination_display or "")

    # Turn 3: Follow-up next bus
    t3 = context_engine.process_turn(sid, "पुढची बस किती वाजता आहे?", language="mr-IN")
    assert t3["handled_deterministically"] is True
    assert t3["intent"] == "next_bus"
    resp3 = t3["response_text"]
    speech3 = t3["speech_text"]

    # Must refer to the active route (131 or 331), strictly NOT 202 or CBS
    assert "202" not in resp3
    assert "दोनशे दोन" not in speech3
    assert any(r in resp3 for r in ("131", "331"))
    context_engine.clear_context(sid)


def test_context_invalidation_on_unserviceable_journey():
    """
    Verifies that querying an unserviceable journey cleanly clears prior active route context.
    """
    sid = "test_phase4_2_invalidation"
    context_engine.clear_context(sid)

    # Turn 1: Valid journey CBS -> Nashik Road
    t1 = context_engine.process_turn(sid, "CBS ते नाशिक रोड", language="mr-IN")
    assert t1["handled_deterministically"] is True
    assert context_engine.sessions[sid].selected_rno == "202"

    # Turn 2: Unserviceable journey (e.g. out of network)
    t2 = context_engine.process_turn(sid, "नाशिक रोड ते पुणे", language="mr-IN")
    assert t2["handled_deterministically"] is True
    assert t2["intent"] == "route_search"
    assert "सापडला नाही" in t2["response_text"] or "आढळला नाही" in t2["response_text"]

    # Previous active route 202 MUST be invalidated
    ctx2 = context_engine.sessions[sid]
    assert ctx2.selected_rno is None
    assert ctx2.selected_route_id is None

    # Turn 3: Follow-up next bus should ask for clarification, never return Route 202
    t3 = context_engine.process_turn(sid, "पुढची बस कधी आहे?", language="mr-IN")
    assert t3["handled_deterministically"] is True
    assert t3["intent"] == "next_bus"
    assert "202" not in t3["response_text"]
    assert "कृपया सांगा" in t3["response_text"] or "जाणून घ्यायची आहे" in t3["response_text"]
    context_engine.clear_context(sid)


# ==============================================================================
# BUG 2 TESTS: ROUTE ACCURACY ENGINE
# ==============================================================================

def test_route_accuracy_anandavalli_satpur_no_route_101():
    """
    Verifies that Anandavalli to Satpur returns 0 direct routes (no false Route 101)
    and successfully provides transfer routes via Shalimar.
    """
    res = route_engine.search_routes("Anandavalli", "Satpur")
    assert res.success is True
    assert len(res.direct_routes) == 0
    # Must NOT return Route 101 as direct
    assert not any(r.rno == "101" for r in res.direct_routes)
    # Must provide transfer routes
    assert len(res.transfer_routes) > 0
    assert any("Shalimar" in tr.interchange_hub for tr in res.transfer_routes)


def test_route_accuracy_ccm_abt_dwarka_bidirectional():
    """
    Verifies bidirectional routing for colloquial landmarks:
    - CCM to CBS and CBS to CCM
    - ABT Circle to CBS and CBS to ABT Circle
    - Dwarka to CBS and CBS to Dwarka
    """
    # 1. CCM
    ccm_cbs = route_engine.search_routes("CCM", "CBS")
    assert ccm_cbs.success is True
    assert any(r.rno in ("103", "103M", "128", "203", "203I") for r in ccm_cbs.direct_routes)

    cbs_ccm = route_engine.search_routes("CBS", "CCM")
    assert cbs_ccm.success is True
    assert any(r.rno in ("103", "128", "203", "303A", "328A") for r in cbs_ccm.direct_routes)

    # 2. ABT Circle
    abt_cbs = route_engine.search_routes("ABT Circle", "CBS")
    assert abt_cbs.success is True
    assert any(r.rno in ("102B", "102G", "302", "302A", "302T") for r in abt_cbs.direct_routes)

    cbs_abt = route_engine.search_routes("CBS", "ABT Circle")
    assert cbs_abt.success is True
    assert any(r.rno in ("102B", "302", "302A", "302T", "202") for r in cbs_abt.direct_routes)

    # 3. Dwarka Circle
    dwk_cbs = route_engine.search_routes("Dwarka", "CBS")
    assert dwk_cbs.success is True
    assert any(r.rno in ("104", "304", "304A", "304T", "201") for r in dwk_cbs.direct_routes)


def test_generic_token_filtering_and_legitimate_aliases():
    """
    Verifies:
    1. Lone generic tokens ('circle', 'road', 'stand', 'nagar') return 0 matches.
    2. 10 legitimate station aliases containing generic keywords resolve accurately.
    """
    # 1. Lone generic tokens
    for token in ("circle", "road", "stand", "nagar"):
        res = route_engine.resolve_place(token)
        assert len(res.matched_station_ids) == 0

    # 2. 10 Legitimate station aliases containing generic keywords
    legitimate_aliases = [
        ("kbt circle", "KBT Circle"),
        ("thatte nagar", "Thatte Nagar"),
        ("vidya vikas circle", "Vidya Vikas Circle"),
        ("dwarka circle", "Dwarka Circle"),
        ("mahatma nagar", "Mahatma Nagar"),
        ("parijat nagar", "Parijat Nagar"),
        ("college road", "College Road"),
        ("gangapur road", "Gangapur Road"),
        ("mumbai naka", "Mumbai Naka"),
        ("jehan circle", "Jehan Circle")
    ]
    for alias_query, expected_label in legitimate_aliases:
        res = route_engine.resolve_place(alias_query)
        assert len(res.matched_station_ids) > 0, f"Failed to resolve legitimate alias: {alias_query}"


# ==============================================================================
# BUG 3 TESTS: GEODESIC HOSPITAL RANKING ACROSS ALL 7 TEST LOCATIONS
# ==============================================================================

def test_geodesic_hospital_ranking_all_7_locations():
    """
    Verifies deterministic geodesic nearest hospital ranking across 7 test locations:
    1. KBT Circle -> District Civil Hospital / Hridayaksha (~2.06 km)
    2. ABT Circle -> ESIC Hospital, Satpur (~2.15 km)
    3. Anandavalli -> Hridayaksha Cardiac Care (~1.62 km)
    4. CCM -> District Civil Hospital (~1.65 km)
    5. CBS -> District Civil Hospital (~0.59 km)
    6. Satpur -> NMC Satpur Urban Dispensary (~0.36 km)
    7. Nashik Road -> Bytco Municipal Hospital (~1.73 km)
    None of them default to MVP Medical College Hospital.
    """
    locations_test_data = [
        ("kbt circle", 20.0097, 73.7658, "civil_hospital"),
        ("abt circle", 19.9974, 73.7540, "esi_hospital"),
        ("anandavalli", 20.0123, 73.7478, "hridayaksha_hospital"),
        ("ccm", 19.9904, 73.7640, "civil_hospital"),
        ("cbs", 20.0007, 73.7810, "civil_hospital"),
        ("satpur", 19.9906, 73.7252, "manpa_satpur_dispensary"),
        ("nashik road", 19.9568, 73.8338, "bytco_hospital")
    ]

    for loc_name, lat, lon, expected_hosp_id in locations_test_data:
        nearest = location_service.get_nearest_hospital(lat, lon)
        assert nearest is not None
        assert nearest["hospital_id"] == expected_hosp_id, (
            f"Location '{loc_name}' matched hospital '{nearest['hospital_id']}', expected '{expected_hosp_id}'"
        )
        # Strictly ensure MVP hospital is NOT returned
        assert nearest["hospital_id"] != "mvp_medical_college_hospital", (
            f"Location '{loc_name}' falsely defaulted to MVP Medical College Hospital!"
        )


def test_nearby_hospital_process_turn_with_location_alias():
    """
    Verifies that 'process_turn' automatically extracts and resolves location when mentioned in hospital query.
    """
    sid = "test_hosp_direct_turn"
    context_engine.clear_context(sid)

    t = context_engine.process_turn(sid, "आनंदवल्ली जवळचे हॉस्पिटल कोणते?", language="mr-IN")
    assert t["handled_deterministically"] is True
    assert t["intent"] == "nearby_hospital"
    assert "हृदयाक्षा" in t["response_text"] or "Hridayaksha" in t["response_text"]
    assert "1.62" in t["response_text"] or "1.6" in t["response_text"]

    ctx = context_engine.sessions[sid]
    assert ctx.selected_hospital_id == "hridayaksha_hospital"
    context_engine.clear_context(sid)


# ==============================================================================
# BUG 4 TESTS: MULTILINGUAL NUMBER PRONUNCIATION & INTENT
# ==============================================================================

def test_number_pronunciation_intent_preference():
    """
    Verifies that 'आकडे मराठीत बोल' is classified as 'number_pronunciation_preference'
    and NOT as a bus route or general query.
    """
    sid = "test_pronounce_pref"
    context_engine.clear_context(sid)

    queries = [
        "आकडे मराठीत बोल",
        "संख्या मराठीत सांगा",
        "मराठीत आकडे बोल",
        "speak numbers in marathi",
        "speak numbers in english"
    ]
    for q in queries:
        t = context_engine.process_turn(sid, q, language="mr-IN")
        assert t["handled_deterministically"] is True
        assert t["intent"] == "number_pronunciation_preference"
        assert t["is_terminated"] is False
        assert "रुपये" in t["response_text"] or "words" in t["response_text"] or "शब्दात" in t["response_text"]

    context_engine.clear_context(sid)


def test_isolated_route_number_formatting():
    """
    Verifies route number pronunciation in Marathi, Hindi, and English:
    101 -> एकशे एक / एक सौ एक / One Hundred One
    102 -> एकशे दोन
    151 -> एकशे एकावन्न
    202 -> दोनशे दोन / दो सौ दो / Two Hundred Two
    300 -> तीनशे / तीन सौ / Three Hundred
    331 -> तीनशे एकतीस / तीन सौ इकतीस / Three Hundred Thirty-One
    331A -> तीनशे एकतीस ए
    """
    assert format_route_number("101", "mr-IN") == "एकशे एक"
    assert format_route_number("102", "mr-IN") == "एकशे दोन"
    assert format_route_number("151", "mr-IN") == "एकशे एकावन्न"
    assert format_route_number("202", "mr-IN") == "दोनशे दोन"
    assert format_route_number("300", "mr-IN") == "तीनशे"
    assert format_route_number("331", "mr-IN") == "तीनशे एकतीस"
    assert format_route_number("331A", "mr-IN") == "तीनशे एकतीस ए"

    assert format_route_number("101", "hi-IN") == "एक सौ एक"
    assert format_route_number("202", "hi-IN") == "दो सौ दो"
    assert format_route_number("300", "hi-IN") == "तीन सौ"

    assert format_route_number("101", "en-IN") == "One Hundred One"
    assert format_route_number("202", "en-IN") == "Two Hundred Two"


def test_isolated_time_and_currency_formatting():
    """
    Verifies natural time and currency formatting:
    08:36 -> आठ वाजून छत्तीस मिनिटांनी (MR), आठ बजकर छत्तीस मिनट पर (HI), Eight Thirty Six (EN)
    ₹20 -> वीस रुपये (MR), बीस रुपये (HI), Twenty rupees (EN)
    """
    assert format_time_natural("08:36", "mr-IN") == "आठ वाजून छत्तीस मिनिटांनी"
    assert format_time_natural("08:36", "hi-IN") == "आठ बजकर छत्तीस मिनट पर"
    assert format_time_natural("08:36", "en-IN") == "Eight Thirty Six"

    assert format_currency(20, "mr-IN") == "वीस रुपये"
    assert format_currency(20, "hi-IN") == "बीस रुपये"
    assert format_currency(20, "en-IN") == "Twenty rupees"


def test_format_text_for_speech_full_sentence():
    """
    Verifies that 'format_text_for_speech' transforms digits in a complete transit sentence.
    """
    sample_mr = "मार्ग 101 ची पुढची बस 08:36 वाजता सुटेल. तिकीट भाडे ₹20 आहे."
    spoken_mr = format_text_for_speech(sample_mr, "mr-IN")
    assert "एकशे एक" in spoken_mr
    assert "आठ वाजून छत्तीस मिनिटांनी" in spoken_mr
    assert "वीस रुपये" in spoken_mr
    # Original raw digits must be converted
    assert "101" not in spoken_mr
    assert "08:36" not in spoken_mr
    assert "₹20" not in spoken_mr

