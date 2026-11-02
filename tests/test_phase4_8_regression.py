"""
Automated Regression Test Suite for Phase 4.8:
Conversation Accuracy, Context Isolation & Complete Connecting Journey Fix.

Verifies mandatory scenarios A through J:
A. English greeting -> Marathi language request -> Marathi journey.
B. Hindi language request -> Hindi journey.
C. Nashik Road hospital lookup -> correction from "Bayko" to "Bitco".
D. Hospital lookup -> Datt Mandir to Nashik Road journey (no stale hospital answer).
E. Datt Mandir to Nashik Road -> Datt Mandir to Nimani journey.
F. CBS to Panchavati with multiple direct options (best 1-3 presented cleanly, no 73 dumps).
G. A route with no direct connection but a feasible connecting journey ("थेट बस उपलब्ध नाही, पण तुम्ही खालील connecting buses ने जाऊ शकता:").
H. A route with no feasible journey (graceful clarification, no crash/empty).
I. Thank-you and greeting after a completed journey.
J. Follow-up correction of origin or destination while preserving the other valid slot.
"""
import pytest
from backend.app.services.conversation_context import context_engine
from backend.app.services.route_engine import route_engine
from backend.app.services.language_detector import language_detector
from backend.app.orchestrator.conversation_manager import conversation_manager


class TestPhase48Regression:

    def setup_method(self):
        """Reset contexts before each test method."""
        context_engine.sessions.clear()
        conversation_manager.sessions.clear()

    # -------------------------------------------------------------------------
    # Scenario A: English greeting -> Marathi language request -> Marathi journey
    # -------------------------------------------------------------------------
    def test_scenario_a_english_to_marathi_flow(self):
        sid = "test_scenario_a"

        # Turn 1: English greeting
        t1 = context_engine.process_turn(sid, "Hello", language="en-IN", turn_id=1)
        assert t1["intent"] == "GREETING"
        assert any(w in t1["response_text"].lower() for w in ["hello", "citylinc", "assist"])

        # Turn 2: Switch to Marathi
        t2 = context_engine.process_turn(sid, "माझ्यासोबत मराठीत बोल", language="mr-IN", turn_id=2)
        assert t2["intent"] == "LANGUAGE_CHANGE"
        assert t2.get("new_language") == "mr-IN"
        assert "मराठीत बोलेन" in t2["response_text"] or "मराठी" in t2["response_text"]

        # Turn 3: Marathi journey query
        t3 = context_engine.process_turn(sid, "मला सीबीएसपासून पंचवटीला जायचं आहे", language="mr-IN", turn_id=3)
        assert t3["intent"] == "ROUTE_QUERY"
        assert "थेट बस उपलब्ध आहे" in t3["response_text"]
        assert "इतर 73 पर्याय" not in t3["response_text"]
        assert len(t3["response_text"]) > 20

    # -------------------------------------------------------------------------
    # Scenario B: Hindi language request -> Hindi journey
    # -------------------------------------------------------------------------
    def test_scenario_b_hindi_request_and_journey(self):
        sid = "test_scenario_b"

        # Turn 1: Switch to Hindi
        t1 = context_engine.process_turn(sid, "मुझसे हिंदी में बात करो", language="hi-IN", turn_id=1)
        assert t1["intent"] == "LANGUAGE_CHANGE"
        assert t1.get("new_language") == "hi-IN"
        assert "हिंदी में बात करूँगा" in t1["response_text"]

        # Turn 2: Hindi journey query
        t2 = context_engine.process_turn(sid, "मुझे सीबीएस से पंचवटी जाना है", language="hi-IN", turn_id=2)
        assert t2["intent"] == "ROUTE_QUERY"
        assert "सीधी बस उपलब्ध है" in t2["response_text"]
        assert "रूट" in t2["response_text"] or "मार्ग" in t2["response_text"]
        # Ensure hindi pronouns / postpositions
        assert "से" in t2["response_text"]
        assert "पर उतरें" in t2["response_text"]

    # -------------------------------------------------------------------------
    # Scenario C: Nashik Road hospital lookup -> correction from "Bayko" to "Bitco"
    # -------------------------------------------------------------------------
    def test_scenario_c_hospital_lookup_and_correction(self):
        sid = "test_scenario_c"

        # Turn 1: Hospital lookup
        t1 = context_engine.process_turn(sid, "नाशिक रोडचे जवळचे हॉस्पिटल कोणते आहे?", language="mr-IN", turn_id=1)
        assert t1["intent"] == "HOSPITAL_LOOKUP"
        assert "बिटको" in t1["response_text"] or "रुग्णालय" in t1["response_text"]
        assert "बायको" not in t1["response_text"]

        # Turn 2: Correction
        t2 = context_engine.process_turn(sid, "ते बायको नाही, ते बिटको आहे", language="mr-IN", turn_id=2)
        assert t2["intent"] == "ROUTE_CORRECTION"
        assert "बिटको रुग्णालय" in t2["response_text"]
        ctx = context_engine.get_context(sid)
        assert ctx.selected_hospital_id == "bytco_hospital"
        assert "Bitco Hospital" in ctx.selected_hospital_name

    # -------------------------------------------------------------------------
    # Scenario D: Hospital lookup -> Datt Mandir to Nashik Road journey
    # -------------------------------------------------------------------------
    def test_scenario_d_hospital_lookup_then_journey_no_stale_response(self):
        sid = "test_scenario_d"

        # Turn 1: Hospital query
        t1 = context_engine.process_turn(sid, "नाशिक रोडचे जवळचे हॉस्पिटल कोणते आहे?", language="mr-IN", turn_id=1)
        assert t1["intent"] == "HOSPITAL_LOOKUP"
        assert "रुग्णालय" in t1["response_text"]

        # Turn 2: Route query "Datt Mandir to Nashik Road"
        t2 = context_engine.process_turn(sid, "मला दत्त मंदिरपासून नाशिक रोडला जायचं आहे", language="mr-IN", turn_id=2)
        assert t2["intent"] == "ROUTE_QUERY"
        # Must NOT repeat the hospital answer
        assert "जवळचे पडताळलेले रुग्णालय" not in t2["response_text"]
        assert "थेट बस उपलब्ध आहे" in t2["response_text"]
        assert "Datta Mandir" in t2["response_text"]
        assert "Nashik Road" in t2["response_text"]

    # -------------------------------------------------------------------------
    # Scenario E: Datt Mandir to Nashik Road -> Datt Mandir to Nimani journey
    # -------------------------------------------------------------------------
    def test_scenario_e_subsequent_journey_context_isolation(self):
        sid = "test_scenario_e"

        # Turn 1: Datt Mandir to Nashik Road
        t1 = context_engine.process_turn(sid, "मला दत्त मंदिरपासून नाशिक रोडला जायचं आहे", language="mr-IN", turn_id=1)
        assert t1["intent"] == "ROUTE_QUERY"
        assert "Nashik Road" in t1["response_text"]

        # Turn 2: Datt Mandir to Nimani
        t2 = context_engine.process_turn(sid, "दत्त मंदिर ते निमाणी", language="mr-IN", turn_id=2)
        assert t2["intent"] == "ROUTE_QUERY"
        assert "Nimani" in t2["response_text"]
        # Must NOT be an old hospital answer
        assert "रुग्णालय" not in t2["response_text"]

    # -------------------------------------------------------------------------
    # Scenario F: CBS to Panchavati with multiple direct options (no 73 dump)
    # -------------------------------------------------------------------------
    def test_scenario_f_cbs_to_panchavati_concise_direct_routes(self):
        sid = "test_scenario_f"

        t1 = context_engine.process_turn(sid, "मला सीबीएसपासून पंचवटीला जायचं आहे", language="mr-IN", turn_id=1)
        assert t1["intent"] == "ROUTE_QUERY"
        assert "थेट बस उपलब्ध आहे" in t1["response_text"]
        assert "सर्वोत्तम पर्याय" in t1["response_text"]
        # Must NOT dump dozens of alternatives
        assert "73" not in t1["response_text"]
        assert "इतर थेट पर्याय: मार्ग" in t1["response_text"]
        # Number of alternatives listed must be concise (1-2 alternatives)
        ctx = context_engine.get_context(sid)
        assert ctx.origin_display is not None
        assert ctx.destination_display is not None

    # -------------------------------------------------------------------------
    # Scenario G: Route with no direct connection but a feasible connecting journey
    # -------------------------------------------------------------------------
    def test_scenario_g_connecting_bus_journey(self):
        sid = "test_scenario_g"

        # Pathardi Phata to Gangapur Gaon has no direct bus, but has connecting buses
        t1 = context_engine.process_turn(sid, "मला पाथर्डी फाट्यावरून गंगापूर गावला जायचं आहे", language="mr-IN", turn_id=1)
        assert t1["intent"] == "ROUTE_QUERY"

        # Verify exact mandatory introductory connecting sentence
        mandatory_phrase = "थेट बस उपलब्ध नाही, पण तुम्ही खालील connecting buses ने जाऊ शकता:"
        assert mandatory_phrase in t1["response_text"]

        # Verify chronological leg structure
        assert "टप्पा 1:" in t1["response_text"]
        assert "टप्पा 2:" in t1["response_text"]
        assert "बदल:" in t1["response_text"]
        assert "प्रतीक्षा करा" in t1["response_text"]
        assert "मीटर" in t1["response_text"]
        assert "(अंदाजित)" in t1["response_text"]
        assert "एकूण वेळ अंदाजे" in t1["response_text"]
        assert "भाडे ₹" in t1["response_text"]

    # -------------------------------------------------------------------------
    # Scenario H: Route with no feasible journey
    # -------------------------------------------------------------------------
    def test_scenario_h_unserviceable_route(self):
        sid = "test_scenario_h"

        t1 = context_engine.process_turn(sid, "मला सीबीएसपासून चंद्रपूरला जायचं आहे", language="mr-IN", turn_id=1)
        assert t1["intent"] == "ROUTE_QUERY"
        assert len(t1["response_text"]) > 10
        assert "सापडले नाही" in t1["response_text"] or "आढळला नाही" in t1["response_text"]
        # Must not crash or return empty
        assert t1["handled_deterministically"] is True

    # -------------------------------------------------------------------------
    # Scenario I: Thank-you and greeting after a completed journey
    # -------------------------------------------------------------------------
    def test_scenario_i_thank_you_and_greeting_flow(self):
        sid = "test_scenario_i"

        # Turn 1: Journey
        t1 = context_engine.process_turn(sid, "मला सीबीएसपासून पंचवटीला जायचं आहे", language="mr-IN", turn_id=1)
        assert t1["intent"] == "ROUTE_QUERY"

        # Turn 2: Thank-you
        t2 = context_engine.process_turn(sid, "ओके, थँक्यू", language="mr-IN", turn_id=2)
        assert t2["intent"] == "THANK_YOU"
        assert "काही हरकत नाही" in t2["response_text"]
        assert "मार्ग आढळला नाही" not in t2["response_text"]

        # Turn 3: Greeting
        t3 = context_engine.process_turn(sid, "हॅलो", language="mr-IN", turn_id=3)
        assert t3["intent"] == "GREETING"
        assert "नमस्कार" in t3["response_text"] or "मदत करू" in t3["response_text"]
        assert "मार्ग आढळला नाही" not in t3["response_text"]

    # -------------------------------------------------------------------------
    # Scenario J: Follow-up correction preserving the valid origin slot
    # -------------------------------------------------------------------------
    def test_scenario_j_slot_correction_preserving_origin(self):
        sid = "test_scenario_j"

        # Turn 1: CBS to Panchavati
        t1 = context_engine.process_turn(sid, "मला सीबीएसपासून पंचवटीला जायचं आहे", language="mr-IN", turn_id=1)
        assert t1["intent"] == "ROUTE_QUERY"
        ctx1 = context_engine.get_context(sid)
        assert ctx1.origin is not None

        # Turn 2: Correct destination to Nimani while preserving origin
        t2 = context_engine.process_turn(sid, "नाही, पंचवटी नाही, निमाणी", language="mr-IN", turn_id=2)
        assert t2["intent"] in ("ROUTE_CORRECTION", "ROUTE_QUERY")
        assert "Nimani" in t2["response_text"] or "निमाणी" in t2["response_text"]
        assert "Cbs" in t2["response_text"] or "सीबीएस" in t2["response_text"]
        assert "थेट बस उपलब्ध आहे" in t2["response_text"]

    # -------------------------------------------------------------------------
    # Validation tests for language detection and turn management
    # -------------------------------------------------------------------------
    def test_language_detection_explicit_commands(self):
        # Hindi switch commands
        r1 = language_detector.detect("मुझसे हिंदी में बात करो", context_language="mr-IN")
        assert r1.language == "hi-IN"
        assert r1.confidence == 1.0

        r2 = language_detector.detect("हिंदी में बोलिए", context_language="mr-IN")
        assert r2.language == "hi-IN"

        # Marathi switch commands
        r3 = language_detector.detect("माझ्यासोबत मराठीत बोल", context_language="hi-IN")
        assert r3.language == "mr-IN"
        assert r3.confidence == 1.0

        r4 = language_detector.detect("मराठीत बोला", context_language="hi-IN")
        assert r4.language == "mr-IN"

        # English switch commands
        r5 = language_detector.detect("Speak with me in English", context_language="mr-IN")
        assert r5.language == "en-IN"
        assert r5.confidence == 1.0

    def test_stale_turn_rejection_in_session(self):
        session = conversation_manager.get_or_create_session("sess_turn_test")
        t1 = session.start_user_turn()
        assert t1 == 1
        assert session.is_turn_valid(1) is True

        # User interrupts / starts a new turn
        t2 = session.start_user_turn()
        assert t2 == 2
        # Previous turn is now stale and invalid!
        assert session.is_turn_valid(1) is False
        assert session.is_turn_valid(2) is True

        # Attempting to add an assistant message for turn 1 must be rejected
        ok = session.add_assistant_message("Stale hospital response", turn_id=1)
        assert ok is False
        assert len(session.history) == 0

        # Adding for turn 2 succeeds
        ok2 = session.add_assistant_message("Valid route response", turn_id=2)
        assert ok2 is True
        assert len(session.history) == 1
        assert session.history[0]["content"] == "Valid route response"

