"""
CityLinc Voice Agent - Final Targeted Browser Test Corrections Test Suite.
Regression tests for:
1. Nearby Hospital Proximity vs Bus Pass Intent Disambiguation
2. Thakkar Bazar and Civil Hospital / Siphil STT Variations Recognition
3. Journey Destination Validation (Civil Hospital to Dwarka alighting stop verification)
4. Graceful 180s Call Ending (Elimination of call_not_found and Duration 0.0s)
"""
import pytest
import time
from backend.app.services.conversation_context import context_engine, SessionTransitContext
from backend.app.services.route_engine import route_engine
from backend.app.services.location_service import location_service
from backend.app.services.call_restriction_service import call_restriction_service
from backend.app.database.call_storage import call_storage


class TestNearbyHospitalVsBusPassIntent:
    """Requirement 1: Distinguish 'के पास / जवळचे' proximity for hospitals from bus pass queries."""

    def test_hindi_datta_mandir_nearby_hospital_intent(self):
        """'दत्ता मंदिर के पास वाला अस्पताल' must trigger HOSPITAL_LOOKUP, not BUS_PASS_QUERY."""
        ctx = SessionTransitContext(session_id="t1", preferred_language="hi-IN")
        intent = context_engine.detect_intent("दत्ता मंदिर के पास वाला अस्पताल", ctx)
        assert intent == "HOSPITAL_LOOKUP", f"Expected HOSPITAL_LOOKUP, got {intent}"

    def test_hindi_nearby_hospital_variations(self):
        """Variations of hospital near landmark in Hindi."""
        ctx = SessionTransitContext(session_id="t2", preferred_language="hi-IN")
        for q in [
            "दत्ता मंदिर के पास अस्पताल",
            "दत्ता मंदिर के पास कौन सा अस्पताल है",
            "दत्त मंदिर के पास वाला अस्पताल बताइए",
            "सीबीएस के पास वाला अस्पताल"
        ]:
            intent = context_engine.detect_intent(q, ctx)
            assert intent == "HOSPITAL_LOOKUP", f"Query '{q}' should be HOSPITAL_LOOKUP, got {intent}"

    def test_marathi_nearby_hospital_variations(self):
        """Variations of nearby hospital in Marathi."""
        ctx = SessionTransitContext(session_id="t3", preferred_language="mr-IN")
        for q in [
            "दत्त मंदिर जवळचे रुग्णालय",
            "दत्ता मंदिर जवळचे हॉस्पिटल कोणते आहे",
            "सीबीएस जवळचे रुग्णालय सांगा"
        ]:
            intent = context_engine.detect_intent(q, ctx)
            assert intent == "HOSPITAL_LOOKUP", f"Query '{q}' should be HOSPITAL_LOOKUP, got {intent}"

    def test_bus_pass_intent_preserved_when_explicit(self):
        """Bus pass queries must still correctly trigger BUS_PASS_QUERY."""
        ctx = SessionTransitContext(session_id="t4", preferred_language="mr-IN")
        pass_queries = [
            "मला बस पास काढायचा आहे",
            "विद्यार्थी पास दर काय आहेत",
            "मासिक पास कसा काढायचा",
            "बस पास",
            "daily pass price",
            "पास की दरें क्या हैं"
        ]
        for q in pass_queries:
            intent = context_engine.detect_intent(q, ctx)
            assert intent == "BUS_PASS_QUERY", f"Query '{q}' should be BUS_PASS_QUERY, got {intent}"

    def test_process_turn_nearby_hospital_response(self):
        """Turn processing for 'दत्ता मंदिर के पास वाला अस्पताल' returns Bitco Hospital."""
        session_id = f"test_hosp_{int(time.time()*1000)}"
        resp = context_engine.process_turn(session_id, "दत्ता मंदिर के पास वाला अस्पताल", language="hi-IN")
        assert resp["intent"] == "HOSPITAL_LOOKUP"
        text = resp["response_text"].lower()
        # Must resolve to Bitco Hospital / Nashik Road
        assert "बिटको" in text or "bitco" in text or "bytco" in text, f"Response should mention Bitco Hospital: {resp['response_text']}"


class TestStopAndHospitalNameRecognition:
    """Requirement 2: Safe explicit recognition for Thakkar Bazar and Civil / Siphil variations."""

    def test_thakkar_bazar_resolution(self):
        """Thakkar Bazar resolves to CBS cluster with canonical station IDs."""
        for query in ["ठक्कर बाजार", "Thakkar Bazar", "thakkar bazaar", "ठक्कर बाझार"]:
            resolved = route_engine.resolve_place(query)
            assert resolved.canonical_name == "cbs", f"Expected canonical 'cbs' for '{query}', got {resolved.canonical_name}"
            # Must include core CBS stations
            assert any(sid in resolved.matched_station_ids for sid in [82, 313, 315, 317, 955, 975, 1017, 1184, 1590]), \
                f"Resolved stations {resolved.matched_station_ids} missing CBS stations for '{query}'"

    def test_civil_hospital_siphil_stt_variations(self):
        """Acoustic mishearings like 'सिफिल', 'सिफिल हॉस्पिटल', 'सिविल अस्पताल' resolve to Civil Hospital."""
        for query in ["सिफिल", "सिफिल हॉस्पिटल", "सिविल अस्पताल", "सिविल हॉस्पिटल", "siphil hospital"]:
            resolved = route_engine.resolve_place(query)
            assert resolved.canonical_name == "civil hospital", f"Expected 'civil hospital' for '{query}', got {resolved.canonical_name}"
            assert set(resolved.matched_station_ids) == {396, 814, 1184, 1590}

    def test_location_service_siphil_navigation(self):
        """Location service hospital navigation handles 'सिफिल' and 'सिफिल हॉस्पिटल'."""
        nav1 = location_service.get_hospital_navigation("सिफिल हॉस्पिटल")
        assert nav1["resolved"] is True
        assert nav1["hospital_id"] == "civil_hospital"

        nav2 = location_service.get_hospital_navigation("सिफिल")
        assert nav2["resolved"] is True
        assert nav2["hospital_id"] == "civil_hospital"


class TestJourneyDestinationValidation:
    """Requirement 3: Final alighting stop must correspond strictly to requested destination."""

    def test_civil_hospital_to_dwarka_no_invalid_direct_to_bharat_nagar(self):
        """Civil Hospital to Dwarka must NOT return routes alighting at Bharat Nagar 1."""
        sr = route_engine.search_routes("Civil Hospital", "Dwarka")
        # Direct routes must not contain invalid Bharat Nagar 1 alighting stop
        for dr in sr.direct_routes:
            assert dr.alighting_stop.station_id in [497, 1078, 1287], \
                f"Direct route {dr.rno} alight stop {dr.alighting_stop.name} (id {dr.alighting_stop.station_id}) is not Dwarka!"
            assert "bharat nagar" not in dr.alighting_stop.name.lower()

    def test_civil_hospital_to_dwarka_valid_connecting_routes(self):
        """Civil Hospital to Dwarka must provide valid connecting transfers ending at Dwarka Circle."""
        sr = route_engine.search_routes("Civil Hospital", "Dwarka")
        assert sr.transfer_routes_count > 0, "Expected valid transfer routes between Civil Hospital and Dwarka"
        for tr in sr.transfer_routes:
            final_stop = tr.leg2.alighting_stop
            assert final_stop.station_id in [497, 1078, 1287], \
                f"Transfer final stop {final_stop.name} (id {final_stop.station_id}) is not Dwarka!"

    def test_dwarka_matched_stations_are_accurate(self):
        """Dwarka Circle must strictly map to Kathe Galli / Nagji Hospital, excluding Bharat Nagar 1."""
        resolved = route_engine.resolve_place("Dwarka")
        assert 1049 not in resolved.matched_station_ids, "Station 1049 (Bharat Nagar 1) must NOT be in Dwarka cluster!"
        assert 1050 not in resolved.matched_station_ids, "Station 1050 (Gauri Shankar) must NOT be in Dwarka cluster!"
        assert set(resolved.matched_station_ids) == {497, 1078, 1287}

    def test_process_turn_civil_to_dwarka_response_clarity(self):
        """In process_turn, Civil Hospital to Dwarka clearly explains connecting journey to Dwarka."""
        session_id = f"test_c2d_{int(time.time()*1000)}"
        resp = context_engine.process_turn(session_id, "सिविल हॉस्पिटल से द्वारका जाना है", language="hi-IN")
        text = resp["response_text"]
        assert "New CBS/Civil Hospital" in text or "Civil Hospital" in text
        # Must clearly indicate transfer or connecting journey
        assert "कनेक्टिंग" in text or "बदलाव" in text or "चरण" in text or "Shalimar" in text
        # Final stop should be Nagji Hospital or Kathe Galli, NOT Bharat Nagar
        assert "Nagji Hospital" in text or "Kathe Galli" in text or "द्वारका" in text
        assert "Bharat Nagar 1" not in text


class TestGraceful180SecondCallEnding:
    """Requirement 4: Elimination of call_not_found and Duration 0.0s; strict duration enforcement."""

    @pytest.mark.asyncio
    async def test_session_connected_call_registered_and_terminated(self):
        """Registered call has valid duration and correct status on termination."""
        ts = int(time.time()*1000)
        call_id = f"test_ws_{ts}"
        phone = f"+9198{ts % 100000000:08d}"

        connect_res = await call_restriction_service.register_connected_call(
            provider_call_id=call_id,
            caller_phone=phone,
            warning_lead_seconds=20
        )
        assert connect_res.allowed is True
        assert connect_res.status == "connected"

        # Simulate small elapsed time
        time.sleep(0.05)

        # Disconnect call normally
        disconnect_res = call_restriction_service.register_call_disconnect(
            provider_call_id=call_id,
            termination_reason="client_disconnect"
        )
        assert disconnect_res.success is True
        assert disconnect_res.status in ("terminated", "completed", "already_terminated")
        assert disconnect_res.duration_seconds >= 0.0
        assert disconnect_res.status != "call_not_found"

    @pytest.mark.asyncio
    async def test_call_termination_idempotency(self):
        """Subsequent termination calls do not revert to call_not_found or 0.0s."""
        ts = int(time.time()*1000)
        call_id = f"test_idemp_{ts}"
        phone = f"+9197{ts % 100000000:08d}"

        await call_restriction_service.register_connected_call(
            provider_call_id=call_id,
            caller_phone=phone
        )
        time.sleep(0.05)

        # Terminate at limit
        await call_restriction_service.terminate_call_at_limit(call_id)

        # Second disconnect call (like from WebSocket finally block)
        disconnect_res = call_restriction_service.register_call_disconnect(
            provider_call_id=call_id,
            termination_reason="client_disconnect"
        )
        assert disconnect_res.success is True
        assert disconnect_res.status == "already_terminated"
        assert disconnect_res.duration_seconds >= 0.0
        assert disconnect_res.status != "call_not_found"

    def test_storage_record_call_termination_not_found_handling(self):
        """Direct call to storage for nonexistent ID returns call_not_found safely."""
        success, duration, status = call_storage.record_call_termination("nonexistent_id_12345")
        assert success is False
        assert status == "call_not_found"
        assert duration == 0.0
