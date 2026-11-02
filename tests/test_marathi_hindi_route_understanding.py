"""
Comprehensive Automated Test Suite for Marathi and Hindi Route Understanding & Language Consistency.
Verifies fixes for the 5 confirmed failures from real-time browser testing:
1. Marathi: 'मला दत्त मंदिरपासून ते नाशिक रोडला जायचं आहे.'
2. Marathi: 'हॅलो, मला दत्त मंदिर बस स्टॉपपासून ते आपलं मुंबई नाक्याला जायचं आहे.'
3. Marathi: 'मला दत्त मंदिरपासून नाशिक रोडला जाण्यासाठी कोणती बस उपलब्ध आहे?'
4. Marathi: 'दत्त मंदिरपासून शालीमारकडे कोणती बस जाते?'
5. Hindi: 'बस बंद कर देंगे।'
"""
import pytest
from backend.app.services.language_detector import language_detector
from backend.app.services.conversation_context import context_engine
from backend.app.services.route_engine import route_engine


class TestConfirmedFailuresBugFixes:
    """Verifies all five confirmed browser conversation bug fixes."""

    def test_bug_1_datta_mandir_to_nashik_road_with_te(self):
        """Bug 1: 'मला दत्त मंदिरपासून ते नाशिक रोडला जायचं आहे.'
        Must NOT truncate destination to 'ते'. Must resolve Datta Mandir -> Nashik Road."""
        inp = "मला दत्त मंदिरपासून ते नाशिक रोडला जायचं आहे."
        det = language_detector.detect(inp)
        assert det.language == "mr-IN"

        context_engine.clear_context("test_sess_bug1")
        res = context_engine.process_turn("test_sess_bug1", inp, language=det.language)

        assert res["handled_deterministically"] is True
        assert res["intent"] == "route_search"
        ctx = context_engine.get_context("test_sess_bug1")
        assert ctx is not None
        assert ctx.origin == "दत्त मंदिर"
        assert ctx.destination == "नाशिक रोड"
        assert "ते दरम्यान थेट किंवा जोडणी मार्ग आढळला नाही" not in res["response_text"]
        assert len(ctx.last_direct_routes) > 0
        assert "थेट बस मार्ग" in res["response_text"]
        assert "245EXP" in [r["rno"] for r in ctx.last_direct_routes] or "201" in [r["rno"] for r in ctx.last_direct_routes]

    def test_bug_2_colloquial_fillers_and_oblique_mumbai_naka(self):
        """Bug 2: 'हॅलो, मला दत्त मंदिर बस स्टॉपपासून ते आपलं मुंबई नाक्याला जायचं आहे.'
        Must strip 'हॅलो', 'बस स्टॉप', 'ते', 'आपलं', and normalize 'मुंबई नाक्या' to 'मुंबई नाका'."""
        inp = "हॅलो, मला दत्त मंदिर बस स्टॉपपासून ते आपलं मुंबई नाक्याला जायचं आहे."
        det = language_detector.detect(inp)
        assert det.language == "mr-IN"

        context_engine.clear_context("test_sess_bug2")
        res = context_engine.process_turn("test_sess_bug2", inp, language=det.language)

        assert res["handled_deterministically"] is True
        assert res["intent"] == "route_search"
        ctx = context_engine.get_context("test_sess_bug2")
        assert ctx is not None
        assert ctx.origin == "दत्त मंदिर"
        assert ctx.destination == "मुंबई नाका"
        assert "ते आपलं मुंबई" not in res["response_text"]
        assert len(ctx.last_direct_routes) > 0
        assert "201" in [r["rno"] for r in ctx.last_direct_routes]

    def test_bug_3_datta_mandir_to_nashik_road_with_jasathi(self):
        """Bug 3: 'मला दत्त मंदिरपासून नाशिक रोडला जाण्यासाठी कोणती बस उपलब्ध आहे?'
        Must resolve Datta Mandir -> Nashik Road without dropping destination."""
        inp = "मला दत्त मंदिरपासून नाशिक रोडला जाण्यासाठी कोणती बस उपलब्ध आहे?"
        det = language_detector.detect(inp)
        assert det.language == "mr-IN"

        context_engine.clear_context("test_sess_bug3")
        res = context_engine.process_turn("test_sess_bug3", inp, language=det.language)

        assert res["handled_deterministically"] is True
        assert res["intent"] == "route_search"
        ctx = context_engine.get_context("test_sess_bug3")
        assert ctx is not None
        assert ctx.origin == "दत्त मंदिर"
        assert ctx.destination == "नाशिक रोड"
        assert len(ctx.last_direct_routes) > 0
        assert "थेट बस मार्ग" in res["response_text"]

    def test_bug_4_datta_mandir_to_shalimar_routes(self):
        """Bug 4: 'दत्त मंदिरपासून शालीमारकडे कोणती बस जाते?'
        Must recognize 'शालीमार' (दीर्घ ई) and find direct CityLinc bus routes."""
        inp = "दत्त मंदिरपासून शालीमारकडे कोणती बस जाते?"
        det = language_detector.detect(inp)
        assert det.language == "mr-IN"

        context_engine.clear_context("test_sess_bug4")
        res = context_engine.process_turn("test_sess_bug4", inp, language=det.language)

        assert res["handled_deterministically"] is True
        assert res["intent"] == "route_search"
        ctx = context_engine.get_context("test_sess_bug4")
        assert ctx is not None
        assert ctx.origin == "दत्त मंदिर"
        assert ctx.destination == "शालीमार"
        assert len(ctx.last_direct_routes) > 0
        assert "मार्ग आढळला नाही" not in res["response_text"]
        assert "थेट बस मार्ग" in res["response_text"]

    def test_bug_5_hindi_verbal_inflection_and_termination(self):
        """Bug 5: 'बस बंद कर देंगे।'
        Must detect Hindi (hi-IN) with high confidence and terminate call politely in Hindi."""
        inp = "बस बंद कर देंगे।"
        det = language_detector.detect(inp)
        assert det.language == "hi-IN"
        assert det.confidence >= 0.80

        context_engine.clear_context("test_sess_bug5")
        res = context_engine.process_turn("test_sess_bug5", inp, language=det.language)

        assert res["handled_deterministically"] is True
        assert res["intent"] == "termination"
        assert res["is_terminated"] is True
        # Must respond in pure Hindi, NOT Marathi
        assert "सिटीलिंक नाशिक बस सेवा का उपयोग करने के लिए धन्यवाद" in res["response_text"]
        assert "अलविदा" in res["response_text"]
        assert "बाय" not in res["response_text"] or "अलविदा" in res["response_text"]


class TestPlaceResolutionIntegrity:
    """Verifies place resolution for key transit hubs and colloquial landmarks."""

    def test_datta_mandir_resolution(self):
        p1 = route_engine.resolve_place("दत्त मंदिर")
        assert 529 in p1.matched_station_ids
        assert 530 in p1.matched_station_ids
        assert p1.canonical_name == "datta mandir"

        p2 = route_engine.resolve_place("दत्त मंदिर बस स्टॉप")
        assert 529 in p2.matched_station_ids

        p3 = route_engine.resolve_place("दत्ता मंदिर")
        assert 529 in p3.matched_station_ids
        assert p3.canonical_name == "datta mandir"

    def test_mumbai_naka_resolution(self):
        p1 = route_engine.resolve_place("मुंबई नाका")
        assert 202 in p1.matched_station_ids or 887 in p1.matched_station_ids
        assert p1.canonical_name == "mumbai naka"

        p2 = route_engine.resolve_place("मुंबई नाक्याला")
        assert 202 in p2.matched_station_ids or 887 in p2.matched_station_ids

    def test_shalimar_resolution(self):
        p_ee = route_engine.resolve_place("शालीमार")
        assert p_ee.canonical_name == "shalimar"
        assert len(p_ee.matched_station_ids) > 0

        p_i = route_engine.resolve_place("शालिमार")
        assert p_i.canonical_name == "shalimar"
        assert len(p_i.matched_station_ids) > 0


class TestMultilingualConsistency:
    """Verifies that Hindi, Marathi, and English inputs produce matching language outputs."""

    def test_hindi_journey_search(self):
        inp = "मुझे दत्त मंदिर से नाशिक रोड जाना है"
        det = language_detector.detect(inp)
        assert det.language == "hi-IN"

        res = context_engine.process_turn("test_hi_1", inp, language=det.language)
        assert res["handled_deterministically"] is True
        assert "सीधी बसें" in res["response_text"]

        inp2 = "दत्ता मंदिर से नाशिक रोड जाना है।"
        det2 = language_detector.detect(inp2)
        assert det2.language == "hi-IN"
        res2 = context_engine.process_turn("test_hi_2", inp2, language=det2.language)
        assert res2["handled_deterministically"] is True
        assert "सीधी बसें" in res2["response_text"]

    def test_english_journey_search(self):
        inp = "How can I reach Shalimar from Datta Mandir?"
        det = language_detector.detect(inp)
        assert det.language == "en-IN"

        res = context_engine.process_turn("test_en_1", inp, language=det.language)
        assert res["handled_deterministically"] is True
        assert "Direct CityLinc routes" in res["response_text"]

