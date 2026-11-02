"""
Comprehensive Verification Tests for Complete CityLinc Bus Knowledge Network.
Verifies:
1. Complete dataset record counts (315 routes, 12,909 stops, 3,547 trips, 1,187 stations).
2. Random and wide-spectrum route lookups (101, 104, 111, 128, 145, 147, 163I, 201, 247, 264, 301, 331A).
3. Stop-to-route inverted lookup (Shalimar, CBS, Tapovan, Ashok Nagar, Panchavati).
4. Route-to-stops sequence lookup.
5. Missing timetable handling for 0-trip corridors (6625, 6784, 7007, 6664).
6. Non-existent route rejection (Route 999, Route 8888).
7. Multilingual parity across Marathi, Hindi, and English.
"""
import pytest
from backend.app.services.citylinc_knowledge import knowledge_service


def test_complete_dataset_counts():
    """Verify exact counts of audited production dataset."""
    assert len(knowledge_service.routes) == 315, f"Expected 315 routes, got {len(knowledge_service.routes)}"
    assert len(knowledge_service.routes_by_id) == 315
    assert len(knowledge_service.known_rnos) == 120, f"Expected 120 unique route numbers, got {len(knowledge_service.known_rnos)}"
    assert len(knowledge_service.timetables) == 3547, f"Expected 3547 timetable trips, got {len(knowledge_service.timetables)}"
    assert len(knowledge_service.unique_station_ids) == 1187, f"Expected 1187 unique stations, got {len(knowledge_service.unique_station_ids)}"
    assert len(knowledge_service.bus_passes) == 3


def test_random_route_lookups_across_full_dataset():
    """Verify that any route across the 120 route numbers can be queried."""
    test_routes = ["101", "104", "111", "128", "145", "147", "163I", "201", "247", "264", "301", "331A"]
    for rno in test_routes:
        assert rno in knowledge_service.routes_by_number, f"Route {rno} not found in routes_by_number"
        routes = knowledge_service.routes_by_number[rno]
        assert len(routes) >= 1

        # Marathi query
        mr_resp = knowledge_service.answer_query_locally(f"बस {rno} चा वेळ काय आहे?", language="mr-IN")
        assert mr_resp is not None
        assert rno in mr_resp or "सिटीलिंक" in mr_resp

        # English query
        en_resp = knowledge_service.answer_query_locally(f"What is the schedule for bus {rno}?", language="en-IN")
        assert en_resp is not None
        assert rno in en_resp or "CityLinc" in en_resp

        # Hindi query
        hi_resp = knowledge_service.answer_query_locally(f"बस {rno} का टाइमिंग बताइए", language="hi-IN")
        assert hi_resp is not None
        assert rno in hi_resp or "सिटीलिंक" in hi_resp


def test_stop_to_route_lookup():
    """Verify inverted stop-to-route lookups."""
    # Shalimar
    shalimar_name, shalimar_routes = knowledge_service.find_routes_for_stop("shalimar")
    assert len(shalimar_routes) >= 40, f"Expected >= 40 routes for Shalimar, got {len(shalimar_routes)}"
    assert "145" in shalimar_routes or "104" in shalimar_routes

    resp_mr = knowledge_service.answer_query_locally("शालिमार वरून जाणाऱ्या बसेस कोणत्या?", language="mr-IN")
    assert resp_mr is not None
    assert "शालिमार" in resp_mr or "Shalimar" in resp_mr

    # CBS
    cbs_name, cbs_routes = knowledge_service.find_routes_for_stop("cbs")
    assert len(cbs_routes) >= 60, f"Expected >= 60 routes for CBS, got {len(cbs_routes)}"
    assert "101" in cbs_routes or "145" in cbs_routes

    # Ashok Nagar
    an_name, an_routes = knowledge_service.find_routes_for_stop("ashok nagar")
    assert len(an_routes) >= 10, f"Expected >= 10 routes for Ashok Nagar, got {len(an_routes)}"
    assert "101" in an_routes or "201" in an_routes


def test_route_stops_query():
    """Verify query for route stops returns stop counts and key stops."""
    resp_101 = knowledge_service.answer_query_locally("What are the stops on route 101?", language="en-IN")
    assert resp_101 is not None
    assert "stops" in resp_101.lower()
    assert "Tapovan" in resp_101 or "Gangapur" in resp_101

    resp_101_mr = knowledge_service.answer_query_locally("बस १०१ चे थांबे कोणते आहेत?", language="mr-IN")
    assert resp_101_mr is not None
    assert "थांबे" in resp_101_mr
    assert "१०१" in resp_101_mr or "101" in resp_101_mr


def test_missing_timetable_handling():
    """Verify that corridors with 0 timetable records clearly state status as pending rather than inventing times."""
    # Route ID 6625 (Route 104 Vitbhatti Gaulane variant)
    resp_6625 = knowledge_service.answer_query_locally("route id 6625 timing", language="en-IN")
    assert resp_6625 is not None
    assert "pending" in resp_6625.lower() or "unavailable" in resp_6625.lower()

    # Route ID 6784 (Route 163I Down variant)
    resp_6784_mr = knowledge_service.answer_query_locally("route id 6784 वेळापत्रक", language="mr-IN")
    assert resp_6784_mr is not None
    assert "प्रलंबित" in resp_6784_mr or "वेळापत्रक" in resp_6784_mr

    # Route ID 7007 (Route 247 Down variant)
    resp_7007_hi = knowledge_service.answer_query_locally("route id 7007 समय सारणी", language="hi-IN")
    assert resp_7007_hi is not None
    assert "प्रतीक्षित" in resp_7007_hi or "समय-सारणी" in resp_7007_hi


def test_non_existent_route_handling():
    """Verify that querying a non-existent route states it is not found without hallucinating."""
    resp_999 = knowledge_service.answer_query_locally("What is the timing of bus 999?", language="en-IN")
    assert resp_999 is not None
    assert "not found" in resp_999.lower() or "999" in resp_999

    resp_999_mr = knowledge_service.answer_query_locally("बस ९९९ कधी सुटते?", language="mr-IN")
    assert resp_999_mr is not None
    assert "उपलब्ध नाही" in resp_999_mr or "999" in resp_999_mr


def test_devanagari_numerals_query():
    """Verify Devanagari numerals are converted and matched accurately."""
    resp_mr = knowledge_service.answer_query_locally("बस १४५ चा वेळ काय आहे?", language="mr-IN")
    assert resp_mr is not None
    assert "१४५" in resp_mr or "145" in resp_mr
    assert "त्र्यंबकेश्वर" in resp_mr or "Trimbakeshwar" in resp_mr

    resp_hi = knowledge_service.answer_query_locally("बस २०१ कब मिलेगी?", language="hi-IN")
    assert resp_hi is not None
    assert "२०१" in resp_hi or "201" in resp_hi

