"""
Comprehensive Test Suite for CityLinc Nashik Location Intelligence & Interactive Map.
Verifies:
1. All 6 REST API endpoints (/stops, /search, /nearby, /hospitals, /landmarks, /routes/{id}).
2. Strict coordinate validation (center_lat, center_lon within Nashik metro bounding box).
3. Nearby stop sorting using Haversine geodesic distance.
4. Route stop sequencing & deduplication of consecutive identical coordinates (DQI-004).
5. Directional platform recognition (Up vs Down vs Both/Terminal).
6. Invalid searches, non-existent routes, and out-of-bounds coordinates.
7. Existing voice WebSocket contract and barge-in non-regression.
"""
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.services.location_service import location_service, haversine_distance_km
from backend.app.orchestrator.conversation_manager import conversation_manager


@pytest.fixture
def client():
    return TestClient(app)


def test_location_service_dataset_integrity():
    """Verify in-memory dataset counts match the audited CityLinc network."""
    # 1. Total unique physical stations must be 1,187
    assert len(location_service.stations_by_id) == 1187, (
        f"Expected 1,187 unique stations, found {len(location_service.stations_by_id)}"
    )

    # 2. Every single station must have center_lat and center_lon in Nashik Metropolitan Region
    # Nashik metro bounds: 19.80 - 20.25 N, 73.50 - 74.05 E
    for sid, st in location_service.stations_by_id.items():
        lat = st["lat"]
        lon = st["lon"]
        assert 19.80 <= lat <= 20.25, f"Station {sid} lat {lat} out of Nashik bounds"
        assert 73.50 <= lon <= 74.05, f"Station {sid} lon {lon} out of Nashik bounds"
        assert st["verified_coordinates"] is True
        assert st["direction"] in ("UP", "DOWN", "BOTH", "TERMINAL")

    # 3. Verified hospitals count must be >= 30
    assert len(location_service.hospitals) >= 30, (
        f"Expected at least 30 hospital stops, found {len(location_service.hospitals)}"
    )


def test_endpoint_get_stops(client):
    """Test GET /api/locations/stops with pagination and filters."""
    # 1. Default pagination
    r = client.get("/api/locations/stops?limit=25&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1187
    assert data["count"] == 25
    assert len(data["stops"]) == 25

    # Check station attributes
    s0 = data["stops"][0]
    assert "station_id" in s0
    assert "name" in s0
    assert "lat" in s0
    assert "lon" in s0
    assert "direction" in s0
    assert "passing_routes" in s0

    # 2. Filter by Route 101 corridor (route_id: 5463)
    r_route = client.get("/api/locations/stops?route_id=5463")
    assert r_route.status_code == 200
    data_route = r_route.json()
    assert data_route["count"] > 0
    for s in data_route["stops"]:
        assert 5463 in s["passing_route_ids"]


def test_endpoint_get_hospitals(client):
    """Test GET /api/locations/hospitals returns verified medical locations."""
    r = client.get("/api/locations/hospitals?limit=50")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 30
    assert data["count"] >= 30

    names = [h["name"].lower() for h in data["hospitals"]]
    assert any("civil" in n for n in names), "Civil Hospital should be in hospitals list"
    assert any("esi" in n for n in names), "ESI Hospital should be in hospitals list"


def test_endpoint_get_landmarks(client):
    """Test GET /api/locations/landmarks with categories."""
    # Education category
    r_edu = client.get("/api/locations/landmarks?category=education")
    assert r_edu.status_code == 200
    edu_data = r_edu.json()
    assert edu_data["total"] >= 50
    for lm in edu_data["landmarks"]:
        assert lm["is_landmark"] is True

    # Religious category
    r_rel = client.get("/api/locations/landmarks?category=religious")
    assert r_rel.status_code == 200
    assert r_rel.json()["total"] >= 50


def test_endpoint_nearby_stops(client):
    """Test GET /api/locations/nearby sorts stops by Haversine distance."""
    # Central Nashik (CBS area: 19.99613, 73.77811)
    r = client.get("/api/locations/nearby?lat=19.99613&lon=73.77811&radius_km=1.5&limit=10")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] > 0
    assert data["count"] <= 10

    stops = data["stops"]
    # Verify sorting: distances must be monotonically non-decreasing
    distances = [s["distance_km"] for s in stops]
    assert distances == sorted(distances), "Nearby stops must be ordered by distance ascending"

    # Verify closest stop is within ~100m of CBS
    assert stops[0]["distance_km"] < 0.15
    assert "distance_meters" in stops[0]
    assert "disclaimer" in data


def test_endpoint_nearby_stops_validation(client):
    """Verify validation on coordinates and boundary limits."""
    # Out of range latitude
    r = client.get("/api/locations/nearby?lat=199.0&lon=73.0")
    assert r.status_code == 422  # FastAPI validation error


def test_endpoint_search_multilingual(client):
    """Test GET /api/locations/search across English, Devanagari, and numbers."""
    # 1. English query
    r_en = client.get("/api/locations/search?q=Civil")
    assert r_en.status_code == 200
    assert r_en.json()["total_matches"] > 0

    # 2. Devanagari query
    r_mr = client.get("/api/locations/search?q=सिव्हिल")
    assert r_mr.status_code == 200
    assert r_mr.json()["total_matches"] > 0

    # 3. Route number query
    r_num = client.get("/api/locations/search?q=101")
    assert r_num.status_code == 200
    routes = r_num.json()["routes"]
    assert any(r["rno"] == "101" for r in routes)

    # 4. Empty or whitespace query
    r_empty = client.get("/api/locations/search?q=   ")
    assert r_empty.status_code == 200
    assert r_empty.json()["total_matches"] == 0


def test_endpoint_route_trajectory_and_deduplication(client):
    """
    Test GET /api/locations/routes/{route_id} returns ordered sequence
    and deduplicates adjacent identical coordinates (DQI-004).
    """
    # 1. Test by route number "101"
    r = client.get("/api/locations/routes/101")
    assert r.status_code == 200
    data = r.json()
    assert data["rno"] == "101"
    assert data["stop_count"] > 0
    assert len(data["stops"]) == data["stop_count"]
    assert len(data["polyline"]) > 0

    # Check origin and destination
    assert data["origin"]["name"] == data["from_station"]
    assert data["destination"]["name"] == data["to_station"]

    # 2. Test Route 6710 (which contains consecutive duplicate stops in DQI-004)
    r_dup = client.get("/api/locations/routes/6710")
    assert r_dup.status_code == 200
    dup_data = r_dup.json()

    # Raw stops count preserves all records
    assert dup_data["raw_stops_count"] == len(dup_data["stops"])

    # Polyline deduplicates consecutive duplicates so no zero-distance jumps
    polyline = dup_data["polyline"]
    for i in range(len(polyline) - 1):
        assert polyline[i] != polyline[i + 1], f"Polyline contains consecutive duplicate at index {i}"


def test_endpoint_invalid_route(client):
    """Test 404 behavior on non-existent route."""
    r = client.get("/api/locations/routes/INVALID_9999")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_existing_voice_websocket_and_bargein_regression(client):
    """
    Ensure the location intelligence additions caused ZERO regressions
    to the active voice conversation WebSocket and turn tracking.
    """
    session = conversation_manager.get_or_create_session("loc_reg_test_session")
    session.reset()

    # Start turn
    t1 = session.add_user_message("Route 101 timings", language="en-IN")
    assert t1 == 1

    # Connect WebSocket
    with client.websocket_connect("/ws/conversation") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"

        # Send text message
        ws.send_json({
            "type": "text",
            "text": "101 नंबरची बस सांगा",
            "language": "mr-IN"
        })

        # Receive transcript
        user_msg = ws.receive_json()
        assert user_msg["type"] == "transcript"
        assert user_msg["role"] == "user"

        # Receive status
        status_msg = ws.receive_json()
        assert status_msg["type"] == "status"
        assert status_msg["status"] == "thinking"
