"""
tests/test_phase4_3_dataset_and_conversation.py
===============================================
Phase 4.3 Regression and Verification Test Suite
Covers:
- P0.1 Route confirmation question does not overwrite active journey
- P0.2 Single-slot updates and multi-turn journey context assembly
- P0.3 Language-specific distance pronunciation
- P1.1 Complete direct route discovery across all dataset corridors
- P1.2 Intermediate timetable estimate transparency
"""

import pytest
from backend.app.services.conversation_context import context_engine, SessionTransitContext
from backend.app.services.route_engine import route_engine
from backend.app.services.timetable_engine import timetable_engine
from backend.app.services.number_formatter import format_text_for_speech, format_distance


# =====================================================================
# P0.1 Tests: Route Confirmation Without Overwriting Journey
# =====================================================================

def test_confirmation_does_not_clear_journey():
    session_id = "test_p0_1_confirm_retain"
    # Turn 1: Search CBS to Nashik Road
    t1 = context_engine.process_turn(session_id, "मला CBS पासून नाशिक रोडला जायचंय", language="mr-IN")
    ctx = context_engine.get_context(session_id)
    assert ctx is not None
    assert ctx.selected_route_id is not None
    assert ctx.selected_rno is not None
    saved_route_id = ctx.selected_route_id
    saved_rno = ctx.selected_rno
    saved_origin = ctx.origin
    saved_dest = ctx.destination

    # Turn 2: Ask confirmation question "ही बस केबीटी सर्कलवरून पण जाते ना?"
    t2 = context_engine.process_turn(session_id, "ही बस केबीटी सर्कलवरून पण जाते ना?", language="mr-IN")
    assert t2["intent"] == "route_confirmation"
    # Active journey state must be strictly preserved
    assert ctx.selected_route_id == saved_route_id
    assert ctx.selected_rno == saved_rno
    assert ctx.origin == saved_origin
    assert ctx.destination == saved_dest
    assert "नाही" in t2["response_text"]


def test_confirmation_with_stop_on_route():
    session_id = "test_p0_1_on_route"
    # Search CBS to Nashik Road
    context_engine.process_turn(session_id, "मला CBS पासून नाशिक रोडला जायचंय", language="mr-IN")
    ctx = context_engine.get_context(session_id)
    assert ctx is not None

    # Ask if it serves Dwarka (which is on CBS -> Nashik Road corridor)
    t2 = context_engine.process_turn(session_id, "ही बस द्वारका वरून जाते का?", language="mr-IN")
    assert t2["intent"] == "route_confirmation"
    assert "होय" in t2["response_text"]
    assert ctx.selected_route_id is not None


def test_confirmation_when_route_does_not_serve_stop():
    session_id = "test_p0_1_not_on_route"
    context_engine.process_turn(session_id, "CBS to Nashik Road", language="en-IN")
    ctx = context_engine.get_context(session_id)
    assert ctx is not None
    orig_rno = ctx.selected_rno

    # Ask if it serves Gangapur Naka (which is in completely different direction)
    t2 = context_engine.process_turn(session_id, "Does this bus go through Gangapur?", language="en-IN")
    assert t2["intent"] == "route_confirmation"
    assert "No" in t2["response_text"]
    # Journey MUST NOT be cleared
    assert ctx.selected_rno == orig_rno
    assert ctx.destination is not None


def test_confirmation_without_active_route():
    session_id = "test_p0_1_no_active"
    t1 = context_engine.process_turn(session_id, "ही बस केबीटी सर्कलवरून पण जाते ना?", language="mr-IN")
    assert t1["intent"] == "route_confirmation"
    assert "कोणताही बस मार्ग निवडलेला नाही" in t1["response_text"]


# =====================================================================
# P0.2 Tests: Multi-turn Journey Assembly & Session Isolation
# =====================================================================

def test_location_declaration_followed_by_destination():
    session_id = "test_p0_2_multi_turn_loc_dest"
    # Turn 1: User declares current location
    t1 = context_engine.process_turn(session_id, "मी KBT Circle जवळ आहे", language="mr-IN")
    ctx = context_engine.get_context(session_id)
    assert ctx is not None
    assert "kbt" in ctx.current_location.lower()
    assert "kbt" in (ctx.origin or "").lower()

    # Turn 2: User specifies destination
    t2 = context_engine.process_turn(session_id, "मला तपोवन जायचंय", language="mr-IN")
    assert t2["handled_deterministically"] is True
    assert ctx.destination is not None
    assert ctx.selected_route_id is not None
    assert len(ctx.last_direct_routes) >= 5


def test_single_slot_destination_update():
    session_id = "test_p0_2_single_dest"
    # Turn 1: User specifies destination only
    t1 = context_engine.process_turn(session_id, "मला नाशिक रोडला जायचंय", language="mr-IN")
    ctx = context_engine.get_context(session_id)
    assert ctx is not None
    assert ctx.destination is not None
    assert "प्रवास सुरू करणार आहात" in t1["response_text"] or "थांब्यावरून" in t1["response_text"]

    # Turn 2: User supplies origin
    t2 = context_engine.process_turn(session_id, "मी CBS येथे आहे", language="mr-IN")
    assert ctx.selected_route_id is not None
    assert "थेट बस" in t2["response_text"] or "मार्ग" in t2["response_text"]


def test_single_slot_origin_update():
    session_id = "test_p0_2_origin_update"
    # Turn 1: CBS to Nashik Road
    context_engine.process_turn(session_id, "CBS to Nashik Road", language="en-IN")
    ctx = context_engine.get_context(session_id)
    assert ctx.destination is not None

    # Turn 2: User updates origin to Dwarka
    t2 = context_engine.process_turn(session_id, "I am now at Dwarka", language="en-IN")
    assert "Dwarka" in (ctx.origin_display or ctx.origin)
    assert ctx.selected_route_id is not None


def test_new_journey_replaces_old_journey():
    session_id = "test_p0_2_replace_journey"
    # Journey 1: CBS to Nashik Road
    context_engine.process_turn(session_id, "CBS to Nashik Road", language="en-IN")
    ctx = context_engine.get_context(session_id)
    assert "202" in [r["rno"] for r in ctx.last_direct_routes]

    # Journey 2: Anandavalli to Satpur
    context_engine.process_turn(session_id, "Anandavalli to Satpur", language="en-IN")
    assert "202" not in [r["rno"] for r in ctx.last_direct_routes]
    assert ctx.origin != "CBS"
    assert ctx.destination == "Satpur"


def test_context_session_isolation():
    sess1 = "test_isolation_user_1"
    sess2 = "test_isolation_user_2"

    context_engine.process_turn(sess1, "मला CBS वरून नाशिक रोडला जायचंय", language="mr-IN")
    ctx1 = context_engine.get_context(sess1)
    ctx2 = context_engine.get_context(sess2)

    assert ctx1.selected_route_id is not None
    assert ctx2 is None  # sess2 has zero contaminated state


# =====================================================================
# P0.3 Tests: Language-Specific Distance Pronunciation
# =====================================================================

def test_english_distance_formatting():
    raw_text = "The distance is 6.6 km and walking is 2.5 km."
    spoken = format_text_for_speech(raw_text, lang="en-IN")
    assert "six point six kilometers" in spoken
    assert "two point five kilometers" in spoken
    # Ensure zero Marathi or Hindi words leak into English output
    assert "पॉईंट" not in spoken
    assert "किमी" not in spoken
    assert "सहा" not in spoken


def test_hindi_distance_formatting():
    raw_text = "दूरी 6.6 किमी है।"
    spoken = format_text_for_speech(raw_text, lang="hi-IN")
    assert "छह पॉइंट छह किलोमीटर" in spoken


def test_marathi_distance_formatting():
    raw_text = "अंतर 6.6 किमी आहे."
    spoken = format_text_for_speech(raw_text, lang="mr-IN")
    assert "सहा पॉईंट सहा किलोमीटर" in spoken


def test_numeric_api_response_unchanged():
    # Route search API model must retain numeric float distance
    res = route_engine.search_routes("CBS", "Nashik Road")
    assert res.success is True
    top_r = res.direct_routes[0]
    assert isinstance(top_r.distance_km, float)
    assert top_r.distance_km > 0


# =====================================================================
# P1.1 Tests: Complete Direct Route Discovery (Layer 1 & Layer 2)
# =====================================================================

def test_abt_circle_cbs_returns_all_direct_routes():
    res = route_engine.search_routes("ABT Circle", "CBS", limit_direct=None)
    assert res.success is True
    found_rnos = {r.rno for r in res.direct_routes}
    expected = {"102B", "102G", "302", "302A", "302T", "202"}
    assert expected.issubset(found_rnos), f"Missing direct routes! Found: {found_rnos}"
    assert len(res.direct_routes) >= 6


def test_cbs_nashik_road_returns_all_direct_routes():
    res = route_engine.search_routes("CBS", "Nashik Road", limit_direct=None)
    assert res.success is True
    # In full dataset, 10 direct route variants serve CBS -> Nashik Road
    assert len(res.direct_routes) >= 10


def test_kbt_tapovan_returns_all_direct_routes():
    res = route_engine.search_routes("KBT Circle", "Tapovan", limit_direct=None)
    assert res.success is True
    found_rnos = {r.rno for r in res.direct_routes}
    expected = {"131", "331", "331S", "331T", "331G"}
    assert expected.issubset(found_rnos), f"Missing direct routes! Found: {found_rnos}"


def test_no_valid_route_suppressed_by_voice_limit():
    session_id = "test_p1_1_voice_limit"
    t = context_engine.process_turn(session_id, "ABT Circle to CBS", language="en-IN")
    ctx = context_engine.get_context(session_id)
    # The session context and map payload hold ALL 6 direct routes
    assert len(ctx.last_direct_routes) >= 6
    assert t["map_action"] is not None
    assert len(t["map_action"]["direct_routes"]) >= 6
    # Spoken message presents top 3 + mention of additional
    assert "more options" in t["response_text"]


def test_routes_without_timetable_are_retained():
    # Route search must not filter out routes that lack timetable entries
    res = route_engine.search_routes("ABT Circle", "CBS", limit_direct=None)
    rnos = [r.rno for r in res.direct_routes]
    assert "202" in rnos


# =====================================================================
# P1.2 Tests: Intermediate Timetable Estimate Transparency
# =====================================================================

def test_terminal_departure_is_official():
    # Trip from origin terminal of route 5463 (Tapovan Terminus)
    res = timetable_engine.query_route_departures("5463", boarding_stop_name="Tapovan Terminus")
    assert res.success is True
    assert res.scheduled_trips[0].is_estimated is False
    assert "अंदाजे" not in res.message_mr
    assert "estimated" not in res.message_en.lower()


def test_intermediate_departure_is_estimated():
    # Intermediate stop Ved Mandir (idx > 0)
    res = timetable_engine.query_route_departures("101", boarding_stop_name="Ved Mandir")
    assert res.success is True
    assert res.scheduled_trips[0].is_estimated is True
    assert "अंदाजे" in res.message_mr


def test_estimate_label_marathi():
    res = timetable_engine.query_route_departures("101", boarding_stop_name="Ved Mandir")
    assert res.success is True
    assert "अंदाजे" in res.message_mr
    assert "सुटेल" in res.message_mr


def test_estimate_label_hindi():
    res = timetable_engine.query_route_departures("101", boarding_stop_name="Ved Mandir")
    assert res.success is True
    assert "लगभग" in res.message_hi
    assert "रवाना होगी" in res.message_hi


def test_estimate_label_english():
    res = timetable_engine.query_route_departures("101", boarding_stop_name="Ved Mandir")
    assert res.success is True
    assert "approximately" in res.message_en.lower() or "estimated" in res.message_en.lower()
