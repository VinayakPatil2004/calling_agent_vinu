"""
Phase L1 Targeted Final Browser Test Fixes - Verification & Regression Suite.
Covers the 5 confirmed defects:
1. Marathi stop normalization ('पंचवडी' -> Panchavati, 'हाशिकरोड' -> Nashik Road)
2. Hindi response consistency (sticky explicit language preference, no acoustic override)
3. Repeat intent (Marathi, Hindi, English, replaying last completed assistant response)
4. Goodbye and conversation closing ('No', 'No, thank you', 'Have a nice day', etc.)
5. Fare safety notice (transparent disclaimer stating Jan-2023 tariff with pending verification)
"""
import pytest
from backend.app.services.conversation_context import context_engine, IntentStr
from backend.app.services.route_engine import route_engine
from backend.app.services.language_detector import language_detector
from backend.app.orchestrator.conversation_manager import conversation_manager


def test_defect1_marathi_stop_normalization_panchavati():
    """
    Defect 1: 'पंचवडी' should resolve to canonical 'Panchavati' with correct station IDs.
    'मला सीबीएसवरून पंचवडीला जायचे आहे.'
    """
    # 1. Resolve stop query directly
    res_place = route_engine.resolve_place("पंचवडी")
    assert res_place.canonical_name == "panchavati"
    assert res_place.display_name == "Panchavati"
    assert set(res_place.matched_station_ids) == {691, 694, 704, 707, 956, 1194}

    # 2. End-to-end conversation turn
    session_id = "test_l1_panchavadi"
    turn = context_engine.process_turn(session_id, "मला सीबीएसवरून पंचवडीला जायचे आहे.")
    assert turn["intent"] == "ROUTE_QUERY"
    assert turn.get("language") == "mr-IN"
    assert "Panchavati" in turn["response_text"] or "पंचवटी" in turn["response_text"]
    assert "थेट बस" in turn["response_text"]

    ctx = context_engine.get_context(session_id)
    assert ctx.origin_display == "Cbs" or ctx.origin_display == "CBS"
    assert ctx.destination_display == "Panchavati"
    assert ctx.selected_route_id is not None


def test_defect1_marathi_stop_normalization_hashikroad():
    """
    Defect 1: 'हाशिकरोड' should resolve to canonical 'Nashik Road' with correct station IDs.
    """
    res_place = route_engine.resolve_place("हाशिकरोड")
    assert res_place.canonical_name == "nashik road"
    assert res_place.display_name == "Nashik Road"
    assert set(res_place.matched_station_ids) == {529, 530, 787, 1440, 1683}

    session_id = "test_l1_hashikroad"
    turn = context_engine.process_turn(session_id, "मला सीबीएसवरून हाशिकरोडला जायचे आहे.")
    assert turn["intent"] == "ROUTE_QUERY"
    assert turn.get("language") == "mr-IN"
    assert "Nashik Road" in turn["response_text"]
    assert "थेट बस" in turn["response_text"]


def test_defect2_hindi_response_consistency_and_journey():
    """
    Defect 2: After user explicitly switches to Hindi, all subsequent responses remain Hindi.
    Tests:
    Turn 1: 'माझ्यासोबत हिंदीमध्ये बोल.' -> switches to hi-IN
    Turn 2: 'मुझे सिविल हॉस्पिटल से नाशिक रोड जाना है।' -> generates Hindi direct journey
    """
    session_id = "test_l1_hindi_sticky"

    # Turn 1: Explicit switch
    turn1 = context_engine.process_turn(session_id, "माझ्यासोबत हिंदीमध्ये बोल.")
    assert turn1["intent"] == "LANGUAGE_CHANGE"
    assert turn1.get("new_language") == "hi-IN"
    assert "हिंदी" in turn1["response_text"]

    ctx = context_engine.get_context(session_id)
    assert ctx.language == "hi-IN"
    assert ctx.has_explicit_language_preference is True
    assert ctx.explicit_language == "hi-IN"

    # Turn 2: Hindi journey query (without passing explicit language - session must persist it)
    turn2 = context_engine.process_turn(session_id, "मुझे सिविल हॉस्पिटल से नाशिक रोड जाना है।")
    assert turn2["intent"] == "ROUTE_QUERY"
    assert turn2.get("language") == "hi-IN"
    assert "सीधी बस" in turn2["response_text"]
    assert "रूट" in turn2["response_text"]
    assert "से बैठें" in turn2["response_text"]
    # Ensure no Marathi leakage in Hindi turn
    assert "थेट बस" not in turn2["response_text"]
    assert "थांबे" not in turn2["response_text"]


def test_defect2_language_detector_sticky_preference_protection():
    """
    Defect 2: Acoustic STT metadata or Devanagari stop tokens must NOT override explicit session preference.
    """
    # Even if acoustic STT says 'mr-IN', sticky Hindi preference must protect session
    det = language_detector.detect(
        text="नाशिक रोड",
        stt_language="mr-IN",
        context_language="hi-IN",
        user_preference="hi-IN"
    )
    # The session logic in websocket_routes preserves session.current_language when explicit flag is active
    sess = conversation_manager.get_or_create_session("test_sess_sticky")
    sess.set_language("hi-IN", explicit=True)
    assert sess.has_explicit_language_preference is True

    # Attempt dynamic non-explicit switch to mr-IN
    sess.set_language("mr-IN", explicit=False)
    assert sess.current_language == "hi-IN"  # Must remain Hindi!

    # Only explicit switch overrides
    sess.set_language("mr-IN", explicit=True)
    assert sess.current_language == "mr-IN"


def test_defect3_repeat_intent_marathi():
    """
    Defect 3: Support Marathi repeat commands: 'परत सांग मला.', 'पुन्हा सांग', 'एकदा रिपीट कर'.
    Replays last completed assistant response and preserves active slots.
    """
    session_id = "test_l1_repeat_mr"
    turn1 = context_engine.process_turn(session_id, "मला सीबीएसवरून पंचवडीला जायचे आहे.")
    assert turn1["intent"] == "ROUTE_QUERY"

    # Repeat query
    turn2 = context_engine.process_turn(session_id, "परत सांग मला.")
    assert turn2["intent"] == "REPEAT_QUERY"
    assert turn2["response_text"] == turn1["response_text"]
    assert turn2.get("language") == "mr-IN"

    # Verify slots were not destroyed
    ctx = context_engine.get_context(session_id)
    assert ctx.origin is not None
    assert ctx.destination is not None
    assert ctx.destination_display == "Panchavati"

    # Additional Marathi repeat variations
    turn3 = context_engine.process_turn(session_id, "पुन्हा सांग")
    assert turn3["intent"] == "REPEAT_QUERY"
    assert turn3["response_text"] == turn1["response_text"]

    turn4 = context_engine.process_turn(session_id, "एकदा रिपीट कर")
    assert turn4["intent"] == "REPEAT_QUERY"
    assert turn4["response_text"] == turn1["response_text"]


def test_defect3_repeat_intent_english_and_hindi():
    """
    Defect 3: Support Hindi and English repeat commands.
    """
    # English
    session_en = "test_l1_repeat_en"
    t1_en = context_engine.process_turn(session_en, "travel from CBS to Nashik Road", language="en-IN")
    t2_en = context_engine.process_turn(session_en, "Can you please repeat again?", language="en-IN")
    assert t2_en["intent"] == "REPEAT_QUERY"
    assert t2_en["response_text"] == t1_en["response_text"]

    t3_en = context_engine.process_turn(session_en, "Repeat that", language="en-IN")
    assert t3_en["intent"] == "REPEAT_QUERY"
    assert t3_en["response_text"] == t1_en["response_text"]

    # Hindi
    session_hi = "test_l1_repeat_hi"
    context_engine.process_turn(session_hi, "माझ्यासोबत हिंदीमध्ये बोल.")
    t1_hi = context_engine.process_turn(session_hi, "मुझे सिविल हॉस्पिटल से नाशिक रोड जाना है।")
    t2_hi = context_engine.process_turn(session_hi, "फिर से बताइए")
    assert t2_hi["intent"] == "REPEAT_QUERY"
    assert t2_hi["response_text"] == t1_hi["response_text"]

    t3_hi = context_engine.process_turn(session_hi, "दोबारा बोलिए")
    assert t3_hi["intent"] == "REPEAT_QUERY"
    assert t3_hi["response_text"] == t1_hi["response_text"]


def test_defect4_goodbye_and_closing():
    """
    Defect 4: Support 'No', 'No, thank you', 'Have a nice day', 'Goodbye'.
    Enters is_terminated=True state without repeatedly asking for more help.
    """
    closings = [
        ("No", "en-IN"),
        ("No, thank you.", "en-IN"),
        ("Have a nice day.", "en-IN"),
        ("Goodbye", "en-IN"),
        ("नाही धन्यवाद", "mr-IN"),
        ("शुभ दिवस", "mr-IN"),
        ("नहीं शुक्रिया", "hi-IN")
    ]

    for phrase, lang in closings:
        sid = f"test_closing_{phrase[:5]}"
        turn = context_engine.process_turn(sid, phrase, language=lang)
        assert turn["intent"] == "GOODBYE"
        assert turn["is_terminated"] is True
        # Must not ask if user needs more help
        assert "आणखी काही मदत करू का" not in turn["response_text"]
        assert "कोई और सहायता" not in turn["response_text"]
        assert "further assistance" not in turn["response_text"]


def test_defect5_fare_safety_notice():
    """
    Defect 5: Fare safety notice must explicitly state that fares reflect Jan-2023 tariff
    and current official live gazette verification is pending.
    """
    # 1. Bus Pass Query (Marathi)
    sid_mr = "test_fare_mr"
    turn_pass_mr = context_engine.process_turn(sid_mr, "बस पासचे दर काय आहेत?")
    assert turn_pass_mr["intent"] == "BUS_PASS_QUERY"
    assert "१ जानेवारी २०२३" in turn_pass_mr["response_text"]
    assert "पडताळणी प्रलंबित" in turn_pass_mr["response_text"]

    # 2. Bus Pass Query (Hindi)
    sid_hi = "test_fare_hi"
    context_engine.process_turn(sid_hi, "माझ्यासोबत हिंदीमध्ये बोल.")
    turn_pass_hi = context_engine.process_turn(sid_hi, "बस पास का किराया क्या है?")
    assert turn_pass_hi["intent"] == "BUS_PASS_QUERY"
    assert "1 जनवरी 2023" in turn_pass_hi["response_text"]
    assert "सत्यापन लंबित" in turn_pass_hi["response_text"]

    # 3. Fare Query (English)
    sid_en = "test_fare_en"
    turn_fare_en = context_engine.process_turn(sid_en, "how much is the bus fare?", language="en-IN")
    assert turn_fare_en["intent"] == "FARE_QUERY"
    assert "Jan-2023" in turn_fare_en["response_text"]
    assert "verification is currently pending" in turn_fare_en["response_text"]
