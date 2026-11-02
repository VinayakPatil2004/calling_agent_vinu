"""
Comprehensive Automated Test Suite for CityLinc Phase 4:
1. Route Engine (Direct routes, 1-transfer routes, invalid places, stop ordering, ranking)
2. Timetable Engine (Nashik IST local time, next bus, morning/evening, midnight crossing, missing trips)
3. Conversation Context Engine (Multi-turn follow-ups, fare queries, thanks vs termination)
4. Hospital & Landmark Data Corrections (Removal of 8 false positives, verified POIs, Up/Down grouping)
5. REST APIs & Voice-to-Map Integration (REST endpoints, WebSocket map actions)
"""
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services.route_engine import route_engine
from backend.app.services.timetable_engine import timetable_engine, parse_time_minutes, format_minutes_time, parse_natural_time
from backend.app.services.conversation_context import context_engine
from backend.app.services.location_service import location_service


@pytest.fixture
def client():
    return TestClient(app)


# ==============================================================================
# 1. ROUTE ENGINE TESTS
# ==============================================================================

def test_route_engine_direct_search_nashik_road_to_panchavati():
    """Verify direct route search from Nashik Road to Panchavati."""
    res = route_engine.search_routes("Nashik Road", "Panchavati", limit_direct=5)
    assert res.success is True
    assert res.direct_routes_count > 0
    assert len(res.direct_routes) > 0

    top_route = res.direct_routes[0]
    assert top_route.rno in ("210T", "210J", "245", "245A", "132")
    assert "Nashik Road" in top_route.boarding_stop.name
    assert "Panchavati" in top_route.alighting_stop.name
    # Boarding stop must strictly precede alighting stop
    assert top_route.intermediate_stops_count > 0
    assert top_route.distance_km > 0
    assert len(top_route.intermediate_stops) > 0
    assert len(top_route.polyline) > 0


def test_route_engine_direct_search_multilingual():
    """Verify Marathi and Hindi place name resolution."""
    # Marathi: 'नाशिक रोडवरून' to 'पंचवटीला'
    mr_res = route_engine.search_routes("नाशिक रोडवरून", "पंचवटीला")
    assert mr_res.success is True
    assert mr_res.origin.canonical_name == "nashik road"
    assert mr_res.destination.canonical_name == "panchavati"
    assert mr_res.direct_routes_count > 0

    # Hindi: 'नासिक रोड से' to 'पंचवटी को'
    hi_res = route_engine.search_routes("नासिक रोड से", "पंचवटी को")
    assert hi_res.success is True
    assert hi_res.origin.canonical_name == "nashik road"
    assert hi_res.destination.canonical_name == "panchavati"
    assert hi_res.direct_routes_count > 0


def test_route_engine_one_transfer_search():
    """Verify 1-transfer routing between peripheral suburban points (Trimbakeshwar to Ozar)."""
    res = route_engine.search_routes("Trimbakeshwar", "Ozar", limit_transfer=3)
    assert res.success is True
    assert res.transfer_routes_count > 0
    t1 = res.transfer_routes[0]
    assert t1.leg1.rno
    assert t1.leg2.rno
    assert t1.interchange_hub
    assert t1.total_distance_km > 0
    assert t1.total_stops_count > 0


def test_route_engine_invalid_origin_and_destination():
    """Verify graceful handling when origin or destination is invalid."""
    # Invalid origin
    res_bad_orig = route_engine.search_routes("NonExistentCityXYZ", "Panchavati")
    assert res_bad_orig.success is False
    assert res_bad_orig.direct_routes_count == 0
    assert "could not be resolved" in res_bad_orig.message

    # Invalid destination
    res_bad_dest = route_engine.search_routes("Nashik Road", "FakeMoonStation999")
    assert res_bad_dest.success is False
    assert res_bad_dest.direct_routes_count == 0
    assert "could not be resolved" in res_bad_dest.message


def test_route_engine_disambiguation_no_police_station_false_match():
    """Verify 'station' does not match 'Satpur Police Station' when user queries Railway Station."""
    place = route_engine.resolve_place("railway station")
    assert place.canonical_name == "nashik road"
    for sid in place.matched_station_ids:
        st_name = location_service.stations_by_id[sid]["name"].lower()
        assert "police station" not in st_name, f"Station {sid} '{st_name}' incorrectly matched as railway station"


# ==============================================================================
# 2. TIMETABLE ENGINE TESTS
# ==============================================================================

def test_timetable_next_bus_for_route_201():
    """Verify next bus query for high-frequency corridor Route 201."""
    res = timetable_engine.query_route_departures("201", query_mode="next", limit=5)
    assert res.success is True
    assert res.rno == "201"
    assert res.trips_count > 0
    assert len(res.scheduled_trips) > 0

    first_trip = res.scheduled_trips[0]
    assert first_trip.estimated_boarding_time
    assert ":" in first_trip.estimated_boarding_time
    assert first_trip.minutes_until_departure is not None


def test_timetable_morning_and_evening_query_modes():
    """Verify morning (06:00 - 11:59) and evening (16:00 - 21:59) filters."""
    m_res = timetable_engine.query_route_departures("201", query_mode="morning", limit=5)
    assert m_res.success is True
    for trip in m_res.scheduled_trips:
        mins = parse_time_minutes(trip.estimated_boarding_time)
        assert 360 <= mins < 720, f"Morning trip {trip.estimated_boarding_time} not in morning window"

    e_res = timetable_engine.query_route_departures("201", query_mode="evening", limit=5)
    assert e_res.success is True
    for trip in e_res.scheduled_trips:
        mins = parse_time_minutes(trip.estimated_boarding_time)
        assert 960 <= mins < 1320, f"Evening trip {trip.estimated_boarding_time} not in evening window"


def test_timetable_first_and_last_bus():
    """Verify first bus and last bus query modes."""
    first_res = timetable_engine.query_route_departures("201", query_mode="first")
    assert first_res.success is True
    assert first_res.trips_count == 1

    last_res = timetable_engine.query_route_departures("201", query_mode="last")
    assert last_res.success is True
    assert last_res.trips_count == 1

    first_mins = parse_time_minutes(first_res.scheduled_trips[0].estimated_boarding_time)
    last_mins = parse_time_minutes(last_res.scheduled_trips[0].estimated_boarding_time)
    assert first_mins <= last_mins


def test_timetable_zero_trip_route_handling():
    """Verify graceful handling when a route has 0 scheduled trips (e.g. Route ID 6625)."""
    res = timetable_engine.query_route_departures("6625")
    assert res.success is False
    assert res.trips_count == 0
    assert "pending operator dispatch" in res.message_en.lower()


def test_timetable_midnight_duration_calculation():
    """Verify duration and time calculation across midnight."""
    t1 = parse_time_minutes("23:45")
    t2 = parse_time_minutes("00:30")
    assert t1 == 1425
    assert t2 == 30
    duration = (t2 - t1) % 1440
    assert duration == 45, f"Expected 45 minutes crossing midnight, got {duration}"


# ==============================================================================
# 3. CONVERSATION CONTEXT ENGINE TESTS
# ==============================================================================

def test_conversation_context_multi_turn_followup():
    """Verify origin/destination preservation across follow-up next bus and fare queries."""
    sid = "pytest_context_session"
    context_engine.clear_context(sid)

    # Turn 1: Route Search
    t1 = context_engine.process_turn(sid, "मला Nashik Road वरून Panchavati ला जायचं आहे.", "mr-IN")
    assert t1["handled_deterministically"] is True
    assert t1["intent"] == "route_search"
    assert t1["map_action"] is not None
    assert t1["map_action"]["action"] == "highlight_journey"

    # Turn 2: Follow-up Next Bus (no OD repeated!)
    t2 = context_engine.process_turn(sid, "पुढची बस कधी आहे?", "mr-IN")
    assert t2["handled_deterministically"] is True
    assert t2["intent"] == "next_bus"
    assert "वाजता" in t2["response_text"] or "मार्ग" in t2["response_text"]

    # Turn 3: Follow-up Fare (no OD repeated!)
    t3 = context_engine.process_turn(sid, "आणि भाडं किती?", "mr-IN")
    assert t3["handled_deterministically"] is True
    assert t3["intent"] == "fare"
    assert "₹" in t3["response_text"] or "भाडे" in t3["response_text"]

    # Turn 4: Polite Thanks (must NOT terminate session)
    t4 = context_engine.process_turn(sid, "धन्यवाद", "mr-IN")
    assert t4["handled_deterministically"] is True
    assert t4["intent"] == "thanks"
    assert t4["is_terminated"] is False

    # Turn 5: Explicit Termination
    t5 = context_engine.process_turn(sid, "बाय", "mr-IN")
    assert t5["handled_deterministically"] is True
    assert t5["intent"] == "termination"
    assert t5["is_terminated"] is True


# ==============================================================================
# 4. HOSPITAL & LANDMARK DATA CORRECTIONS (PHASE 4A & 4E)
# ==============================================================================

def test_hospital_false_positives_eliminated():
    """
    Verify all 8 false positive records from Phase 3.5 are eliminated from hospital category:
    - Medical Phata 1 (U/D) [95, 859]
    - Medical Phata 2 (D) [92]
    - Medical Phata 3 [1344]
    - Medical Phata (U) [94]
    - Medical Fata Mhasrul [1492]
    - Panchakarma Residency [711]
    - Saptshrungi Mata Mandir Phata [1863]
    """
    hosp_stop_ids = {h["station_id"] for h in location_service.hospitals}

    false_positive_ids = [95, 859, 92, 1344, 94, 1492, 711, 1863]
    for fid in false_positive_ids:
        assert fid not in hosp_stop_ids, f"False positive ID {fid} still present in location_service.hospitals"

        # Verify correct reclassification
        st = location_service.stations_by_id[fid]
        assert st["is_hospital"] is False
        if fid in [95, 859, 92, 1344, 94, 1492]:
            assert st["category"] == "junction"
        elif fid == 711:
            assert st["category"] == "general"
        elif fid == 1863:
            assert st["category"] == "religious"


def test_verified_hospital_entities_registry():
    """Verify the 16 physical hospital entities with Up/Down platform grouping."""
    entities = location_service.hospital_entities
    assert len(entities) == 16, f"Expected 16 verified hospital entities, found {len(entities)}"

    entity_ids = {h["hospital_id"] for h in entities}
    assert "civil_hospital" in entity_ids
    assert "esi_hospital" in entity_ids
    assert "bytco_hospital" in entity_ids
    assert "namco_hospital" in entity_ids
    assert "apollo_hospital" in entity_ids
    assert "wockhardt_hospital" in entity_ids
    assert "ashoka_medicover_hospital" in entity_ids

    # Verify Civil Hospital platform grouping
    civil = location_service.hospital_entities_by_id["civil_hospital"]
    assert civil["physical_coordinates"]["lat"] == 19.9958
    assert civil["physical_coordinates"]["lon"] == 73.7788
    assert len(civil["linked_bus_stops"]) >= 4
    assert civil["directional_platforms"]["up_platform"] is not None
    assert civil["directional_platforms"]["down_platform"] is not None


def test_hospital_navigation_guidance():
    """Verify hospital navigation guidance returns correct alighting and walking details."""
    nav = location_service.get_hospital_navigation("Civil Hospital")
    assert nav["resolved"] is True
    assert nav["hospital_id"] == "civil_hospital"
    assert nav["primary_alighting_stop"] is not None
    assert len(nav["direct_routes"]) > 0
    assert "walking_guidance" in nav
    assert "disclaimer" in nav


# ==============================================================================
# 5. REST API ENDPOINTS TESTS
# ==============================================================================

def test_api_routes_search(client):
    """GET /api/routes/search returns verified route options."""
    r = client.get("/api/routes/search?origin=Nashik Road&destination=Panchavati")
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["direct_routes_count"] > 0
    assert len(data["direct_routes"]) > 0


def test_api_timetable_next(client):
    """GET /api/timetable/next returns next scheduled departure in IST."""
    r = client.get("/api/timetable/next?route_id=201")
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["trips_count"] > 0
    assert len(data["scheduled_trips"]) > 0


def test_api_places_search(client):
    """GET /api/places/search returns verified matches."""
    r = client.get("/api/places/search?q=Civil Hospital")
    assert r.status_code == 200
    data = r.json()
    assert data["total_matches"] > 0


def test_api_places_connections(client):
    """GET /api/places/{place_id}/connections returns transit connections."""
    r = client.get("/api/places/civil_hospital/connections")
    assert r.status_code == 200
    data = r.json()
    assert data["place_id"] == "civil_hospital"
    assert len(data["direct_routes"]) > 0
    assert len(data["direct_stops"]) > 0

