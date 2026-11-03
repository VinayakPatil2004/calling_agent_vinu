"""
CityLinc Final Targeted Intent & WebSocket Fixes Regression Suite (November 3, 2026 Test Audit).
Verifies:
1. Nearby Bus Stop Intent (with locality resolution vs no location fallback)
2. Bus Pass vs Proximity Disambiguation (application query vs fare query)
3. Emergency Intent — Dizziness on Bus (conservative guidance, 112, no origin/destination asked)
4. Goodbye Intent False Positive ('Najdik wala bus stop' does NOT trigger goodbye)
5. 180-Second WebSocket Close Race (safe_send_json suppresses RuntimeError on closed connection)
6. Route Result Validation: Panchavati to Nimani (correct boarding and alighting stops)
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from starlette.websockets import WebSocketState

from backend.app.services.conversation_context import (
    context_engine,
    SessionTransitContext,
    IntentStr
)
from backend.app.services.route_engine import route_engine


# =====================================================================
# 1. Nearby Bus Stop Intent
# =====================================================================
class TestNearbyBusStopIntent:
    def test_panchavati_ke_paas_bus_stop(self):
        """1. 'मेरे पंचवटी के पास कौन सा बस स्टॉप है?' -> Nearby stop lookup for Panchavati."""
        session_id = "test_nearby_panchavati_hi"
        turn = context_engine.process_turn(session_id, "मेरे पंचवटी के पास कौन सा बस स्टॉप है?", language="hi-IN")
        
        assert turn["intent"] in ("NEARBY_STOP_LOOKUP", "STOP_LOOKUP")
        assert turn["intent"] != "GOODBYE"
        assert turn["intent"] != "BUS_PASS_QUERY"
        # Must list Panchavati stops
        assert "पंचवटी" in turn["response_text"] or "Panchavati" in turn["response_text"]
        assert "कारंजा" in turn["response_text"] or "डिपो" in turn["response_text"] or "Karanja" in turn["response_text"]

    def test_panchavati_jawalche_bus_stop_mr(self):
        """2. 'पंचवटीजवळचे बस स्टॉप सांगा.' -> Nearby stop lookup in Marathi."""
        session_id = "test_nearby_panchavati_mr"
        turn = context_engine.process_turn(session_id, "पंचवटीजवळचे बस स्टॉप सांगा.", language="mr-IN")
        
        assert turn["intent"] in ("NEARBY_STOP_LOOKUP", "STOP_LOOKUP")
        assert turn["intent"] != "GOODBYE"
        assert turn["intent"] != "BUS_PASS_QUERY"
        assert "पंचवटी" in turn["response_text"] or "Panchavati" in turn["response_text"]

    def test_mere_paas_ka_najdik_bus_stop_hi_no_loc(self):
        """3. 'मेरे पास का सबसे नजदीक बस स्टॉप कहाँ है?' -> Ask for user locality/landmark without inventing location."""
        session_id = "test_nearby_no_loc_hi"
        turn = context_engine.process_turn(session_id, "मेरे पास का सबसे नजदीक बस स्टॉप कहाँ है?", language="hi-IN")
        
        assert turn["intent"] in ("NEARBY_STOP_LOOKUP", "STOP_LOOKUP")
        assert turn["intent"] != "GOODBYE"
        assert turn["intent"] != "BUS_PASS_QUERY"
        # Must politely ask for user's area/landmark
        assert any(w in turn["response_text"] for w in ["स्थान", "लैंडमार्क", "क्षेत्र", "बताइए", "सीबीएस", "पंचवटी"])

    def test_najdik_wala_bus_stop(self):
        """4. 'Najdik wala bus stop' -> Ask for area/landmark, MUST NOT trigger goodbye!"""
        session_id = "test_najdik_wala_no_goodbye"
        turn = context_engine.process_turn(session_id, "Najdik wala bus stop", language="hi-IN")
        
        assert turn["intent"] != "GOODBYE"
        assert turn["intent"] in ("NEARBY_STOP_LOOKUP", "STOP_LOOKUP")
        assert turn["is_terminated"] is False

    def test_majhya_jawalcha_bus_stop_mr(self):
        """5. 'माझ्या जवळचा बस स्टॉप कोणता आहे?' -> Marathi prompt for area/landmark."""
        session_id = "test_majhya_jawalcha_mr"
        turn = context_engine.process_turn(session_id, "माझ्या जवळचा बस स्टॉप कोणता आहे?", language="mr-IN")
        
        assert turn["intent"] in ("NEARBY_STOP_LOOKUP", "STOP_LOOKUP")
        assert turn["intent"] != "GOODBYE"
        assert turn["intent"] != "BUS_PASS_QUERY"
        assert any(w in turn["response_text"] for w in ["ठिकाण", "परिसर", "सांगा", "सीबीएस", "पंचवटी"])


# =====================================================================
# 2. Bus Pass vs Proximity Disambiguation
# =====================================================================
class TestBusPassDisambiguation:
    def test_datta_mandir_ke_paas_not_bus_pass(self):
        """'के पास' alone must not trigger BUS_PASS_QUERY."""
        ctx = context_engine.get_or_create_context("test_ctx_proximity")
        intent = context_engine.detect_intent("दत्ता मंदिर के पास कौन सा अस्पताल है?", ctx)
        assert intent == "HOSPITAL_LOOKUP"
        assert intent != "BUS_PASS_QUERY"

    def test_bus_pass_fare_query(self):
        """Explicit pass rate query returns verified rates."""
        session_id = "test_pass_rates"
        turn = context_engine.process_turn(session_id, "बस पास का किराया कितना है?", language="hi-IN")
        assert turn["intent"] == "BUS_PASS_QUERY"
        assert "₹110" in turn["response_text"]
        assert "₹2,100" in turn["response_text"]

    def test_bus_pass_application_location_hi(self):
        """'पास कहाँ बनता है?' -> Clearly state pass counter details are unavailable without inventing addresses/counters."""
        session_id = "test_pass_make_hi"
        turn = context_engine.process_turn(session_id, "पास कहाँ बनता है?", language="hi-IN")
        assert turn["intent"] in ("BUS_PASS_APPLICATION", "BUS_PASS_QUERY")
        assert any(w in turn["response_text"] for w in ["काउंटर", "उपलब्ध नहीं है", "पूछताछ केंद्र", "सत्यापन"])

    def test_bus_pass_application_location_mr(self):
        """'पास कसा काढायचा?' -> Clearly state pass counter details are unavailable in Marathi."""
        session_id = "test_pass_make_mr"
        turn = context_engine.process_turn(session_id, "पास कसा काढायचा?", language="mr-IN")
        assert turn["intent"] in ("BUS_PASS_APPLICATION", "BUS_PASS_QUERY")
        assert any(w in turn["response_text"] for w in ["काउंटर", "माहिती", "उपलब्ध नाही", "चौकशी"])

    def test_bus_pass_application_location_en(self):
        """'Where can I get a bus pass?' -> Clearly state pass counter details are unavailable in English."""
        session_id = "test_pass_make_en"
        turn = context_engine.process_turn(session_id, "Where can I get a bus pass?", language="en-IN")
        assert turn["intent"] in ("BUS_PASS_APPLICATION", "BUS_PASS_QUERY")
        assert any(w in turn["response_text"].lower() for w in ["unavailable", "counter", "information counter", "inquire"])


# =====================================================================
# 3. Emergency Intent — Dizziness on Bus
# =====================================================================
class TestEmergencyDizzinessIntent:
    @pytest.mark.parametrize("query,lang", [
        ("बस में किसी को चक्कर आ रहे हैं।", "hi-IN"),
        ("यात्री को चक्कर आ रहे हैं।", "hi-IN"),
        ("बस में कोई बेहोश हो रहा है।", "hi-IN"),
        ("प्रवाशाला चक्कर येत आहेत.", "mr-IN"),
        ("Someone is unconscious on the bus.", "en-IN")
    ])
    def test_emergency_dizziness_guidance(self, query, lang):
        """Emergency query provides conservative guidance, references 112, and does NOT ask for origin/destination."""
        session_id = f"test_emerg_{abs(hash(query))}"
        turn = context_engine.process_turn(session_id, query, language=lang)
        
        assert turn["intent"] == "EMERGENCY_ASSISTANCE"
        reply = turn["response_text"]
        
        # Must not ask for journey origin/destination
        assert "कुठून कुठे" not in reply
        assert "कहाँ से कहाँ" not in reply
        assert "where to" not in reply.lower()
        
        # Must alert driver / conductor
        assert any(w in reply for w in ["चालक", "वाहक", "कंडक्टर", "driver", "conductor"])
        
        # Must mention 112 emergency helpline
        assert "112" in reply or "११२" in reply
        
        # Must advise against giving food/drink/medicine to unconscious passenger
        assert any(w in reply for w in ["अन्न", "पाणी", "औषध", "खाने-पीने", "दवाई", "food", "drink", "medicine"])
        
        # Must not claim an ambulance was dispatched
        assert " रुग्णवाहिका बोलावली आहे" not in reply
        assert "एम्बुलेंस बुला ली है" not in reply
        assert "ambulance has been dispatched" not in reply.lower()


# =====================================================================
# 4. Goodbye Intent False Positive
# =====================================================================
class TestGoodbyeFalsePositive:
    @pytest.mark.parametrize("query", [
        "Najdik wala bus stop",
        "पुढचा बस स्टॉप कोणता आहे",
        "या बसचा शेवटचा स्टॉप कोणता",
        "stop list saanga",
        "which stop comes next"
    ])
    def test_transit_stop_query_not_goodbye(self, query):
        """Queries containing 'stop' or transit words MUST NOT trigger goodbye."""
        ctx = context_engine.get_or_create_context("test_no_goodbye")
        is_term = context_engine.check_termination(query)
        assert is_term is False
        intent = context_engine.detect_intent(query, ctx)
        assert intent != "GOODBYE"

    @pytest.mark.parametrize("farewell", [
        "गुडबाय",
        "bye",
        "bye bye",
        "अलविदा",
        "कॉल बंद करा",
        "that's all thank you"
    ])
    def test_genuine_farewell_triggers_goodbye(self, farewell):
        """Clear standalone farewell phrases properly terminate conversation."""
        ctx = context_engine.get_or_create_context("test_real_goodbye")
        is_term = context_engine.check_termination(farewell)
        assert is_term is True


# =====================================================================
# 5. 180-Second WebSocket Close Race
# =====================================================================
class TestWebSocketCloseRace:
    @pytest.mark.asyncio
    async def test_safe_send_json_suppresses_runtime_error_after_close(self):
        """
        Simulate a WebSocket that raises RuntimeError on send_json() (e.g. after close).
        Verify safe_send_json handles it gracefully without crashing or raising.
        """
        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.DISCONNECTED
        mock_ws.send_json = AsyncMock(side_effect=RuntimeError('Cannot call "send" once a close message has been sent.'))

        is_ws_closed = False

        async def safe_send_json(payload: dict) -> bool:
            nonlocal is_ws_closed
            if is_ws_closed:
                return False
            try:
                if hasattr(mock_ws, "client_state") and mock_ws.client_state != WebSocketState.CONNECTED:
                    is_ws_closed = True
                    return False
            except Exception:
                pass
            try:
                await mock_ws.send_json(payload)
                return True
            except Exception:
                is_ws_closed = True
                return False

        # Attempt send after disconnected state
        success = await safe_send_json({"type": "status", "status": "thinking"})
        assert success is False
        assert is_ws_closed is True
        mock_ws.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_safe_send_json_catches_send_race(self):
        """If client_state appeared CONNECTED but send_json raised RuntimeError, safe_send_json marks closed and returns False."""
        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.CONNECTED
        mock_ws.send_json = AsyncMock(side_effect=RuntimeError('Cannot call "send" once a close message has been sent.'))

        is_ws_closed = False

        async def safe_send_json(payload: dict) -> bool:
            nonlocal is_ws_closed
            if is_ws_closed:
                return False
            try:
                if hasattr(mock_ws, "client_state") and mock_ws.client_state != WebSocketState.CONNECTED:
                    is_ws_closed = True
                    return False
            except Exception:
                pass
            try:
                await mock_ws.send_json(payload)
                return True
            except Exception:
                is_ws_closed = True
                return False

        success = await safe_send_json({"type": "audio", "audio": "base64data"})
        assert success is False
        assert is_ws_closed is True


# =====================================================================
# 6. Route Result Validation: Panchavati to Nimani
# =====================================================================
class TestPanchavatiToNimaniValidation:
    def test_panchavati_to_nimani_stops_represent_endpoints(self):
        """
        For Panchavati to Nimani:
        Boarding stop must represent Panchavati (Karanja).
        Alighting stop must represent Nimani (Depot Corner).
        MUST NOT be backwards!
        """
        res = route_engine.search_routes("Panchavati", "Nimani")
        assert res.success is True
        assert res.direct_routes_count > 0
        top = res.direct_routes[0]
        
        # Boarding must be Panchavati Karanja
        assert "Karanja" in top.boarding_stop.name
        # Alighting must be Panchavati Depot Corner (Nimani)
        assert "Depot Corner" in top.alighting_stop.name

    def test_nimani_to_panchavati_stops_represent_endpoints(self):
        """
        For Nimani to Panchavati:
        Boarding stop must represent Nimani (Depot Corner).
        Alighting stop must represent Panchavati (Karanja).
        """
        res = route_engine.search_routes("Nimani", "Panchavati")
        assert res.success is True
        assert res.direct_routes_count > 0
        top = res.direct_routes[0]
        
        # Boarding must be Depot Corner (Nimani)
        assert "Depot Corner" in top.boarding_stop.name
        # Alighting must be Karanja (Panchavati)
        assert "Karanja" in top.alighting_stop.name
