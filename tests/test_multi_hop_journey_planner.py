"""
Unit and Integration Test Suite for CityLinc Multi-Hop Journey Planner.
Tests all 20 mandatory test cases specified in CityLinc Route Engine Upgrade requirements:
1. CBS -> Nashik Road
2. CBS -> Nimani
3. Nimani -> Panchavati
4. Datta Mandir -> Nashik Road
5. Datta Mandir -> Shalimar
6. Datta Mandir -> Mumbai Naka
7. Gangapur Road -> KBT Circle
8. Nimani -> CBS
9. Origin & Destination connected by a direct route
10. Journey requiring 1 transfer
11. Journey requiring 2 transfers
12. Journey with an infeasible transfer due to timetable
13. Journey with no valid path
14. Same stop as origin and destination
15. Unknown origin
16. Unknown destination
17. Marathi query
18. Hindi query
19. English query
20. User correcting origin or destination mid-conversation
"""
import pytest
from backend.app.services.route_engine import route_engine, parse_natural_departure_time
from backend.app.services.transit_graph import transit_graph
from backend.app.services.conversation_context import context_engine


class TestCityLincMultiHopJourneyPlanner:
    """Verifies all 20 mandatory journey planning test scenarios across the CityLinc network."""

    def test_01_cbs_to_nashik_road(self):
        """1. CBS -> Nashik Road direct connectivity."""
        res = route_engine.search_routes("CBS", "Nashik Road")
        assert res.success is True
        assert res.direct_routes_count > 0
        top = res.direct_routes[0]
        assert top.rno != ""
        assert top.intermediate_stops_count > 0
        assert top.boarding_stop.name != ""
        assert top.alighting_stop.name != ""
        assert top.distance_km > 0.0

    def test_02_cbs_to_nimani(self):
        """2. CBS -> Nimani direct connectivity."""
        res = route_engine.search_routes("CBS", "Nimani")
        assert res.success is True
        assert res.direct_routes_count > 0
        top = res.direct_routes[0]
        assert top.rno != ""

    def test_03_nimani_to_panchavati(self):
        """3. Nimani -> Panchavati direct connectivity."""
        res = route_engine.search_routes("Nimani", "Panchavati")
        assert res.success is True
        assert res.direct_routes_count > 0

    def test_04_datta_mandir_to_nashik_road(self):
        """4. Datta Mandir -> Nashik Road direct connectivity."""
        res = route_engine.search_routes("Datta Mandir", "Nashik Road")
        assert res.success is True
        assert res.direct_routes_count > 0
        top = res.direct_routes[0]
        assert top.distance_km > 0.0

    def test_05_datta_mandir_to_shalimar(self):
        """5. Datta Mandir -> Shalimar direct connectivity."""
        res = route_engine.search_routes("Datta Mandir", "Shalimar")
        assert res.success is True
        assert res.direct_routes_count > 0
        top = res.direct_routes[0]
        assert top.distance_km > 5.0

    def test_06_datta_mandir_to_mumbai_naka(self):
        """6. Datta Mandir -> Mumbai Naka direct connectivity."""
        res = route_engine.search_routes("Datta Mandir", "Mumbai Naka")
        assert res.success is True
        assert res.direct_routes_count > 0

    def test_07_gangapur_road_to_kbt_circle(self):
        """7. Gangapur Road -> KBT Circle direct connectivity."""
        res = route_engine.search_routes("Gangapur Road", "KBT Circle")
        assert res.success is True
        assert res.direct_routes_count > 0

    def test_08_nimani_to_cbs(self):
        """8. Nimani -> CBS direct connectivity."""
        res = route_engine.search_routes("Nimani", "CBS")
        assert res.success is True
        assert res.direct_routes_count > 0

    def test_09_direct_route_properties(self):
        """9. Origin & Destination connected by a direct route: polyline, distance, stops, fare."""
        res = route_engine.search_routes("CBS", "Nashik Road")
        assert res.success is True
        top = res.direct_routes[0]
        assert len(top.polyline) > 0
        assert len(top.intermediate_stops) > 0
        assert res.fare_estimate is not None
        assert res.fare_estimate["single_fare_inr"] >= 10
        assert top.timetable_trips_count > 0

    def test_10_one_transfer_journey(self):
        """10. Journey requiring 1 transfer (Pathardi Phata -> Dindori)."""
        res = route_engine.search_routes("Pathardi Phata", "Dindori")
        assert res.success is True
        assert res.transfer_routes_count > 0
        tr = res.transfer_routes[0]
        assert tr.leg1.rno != tr.leg2.rno
        assert tr.interchange_hub != ""
        assert tr.total_distance_km > 0.0
        assert tr.total_stops_count > 0
        assert res.fare_estimate is not None

    def test_11_two_transfer_journey(self):
        """11. Journey requiring 2 transfers (KVN Naik College -> Sant Gadge Maharaj Udyan)."""
        res = route_engine.search_routes(
            "KVN Naik College",
            "Sant Gadge Maharaj Udyan",
            include_two_transfers=True
        )
        assert res.success is True
        two_tr = [j for j in res.multi_hop_journeys if j.transfers_count == 2]
        assert len(two_tr) > 0
        top = two_tr[0]
        assert len(top.legs) == 3
        assert len(top.transfers) == 2
        r1, r2, r3 = top.legs[0].rno, top.legs[1].rno, top.legs[2].rno
        assert r1 != r2 and r2 != r3 and r1 != r3

    def test_12_infeasible_transfer_due_to_timetable(self):
        """12. Journey with an infeasible transfer due to timetable (late evening 23:55)."""
        res = route_engine.search_routes(
            "Pathardi Phata",
            "Dindori",
            departure_time="23:55"
        )
        assert res.success is True
        assert res.transfer_routes_count > 0
        # Connecting leg cannot leave on the same day after late night arrival
        assert res.transfer_routes[0].is_timetable_feasible is False

    def test_13_no_valid_path(self):
        """13. Journey with no valid path."""
        res = route_engine.search_routes("NonExistentStationA", "NonExistentStationB")
        assert res.success is False
        assert res.direct_routes_count == 0
        assert res.transfer_routes_count == 0

    def test_14_same_stop_origin_and_destination(self):
        """14. Same stop as origin and destination."""
        res = route_engine.search_routes("CBS", "CBS")
        assert res.success is False
        assert res.direct_routes_count == 0
        assert res.transfer_routes_count == 0
        assert "same location" in res.message.lower() or "एकच" in res.clarification_question

    def test_15_unknown_origin(self):
        """15. Unknown origin."""
        res = route_engine.search_routes("AtlantisUnknown", "CBS")
        assert res.success is False
        assert res.direct_routes_count == 0
        assert len(res.origin.matched_station_ids) == 0
        assert "सापडला नाही" in res.clarification_question

    def test_16_unknown_destination(self):
        """16. Unknown destination."""
        res = route_engine.search_routes("CBS", "GothamUnknown")
        assert res.success is False
        assert res.direct_routes_count == 0
        assert len(res.destination.matched_station_ids) == 0
        assert "सापडले नाही" in res.clarification_question

    def test_17_marathi_query(self):
        """17. Marathi natural language route query."""
        context_engine.clear_context("test_sess_mr")
        res = context_engine.process_turn(
            "test_sess_mr",
            "मला दत्त मंदिरपासून नाशिक रोडला जायचे आहे.",
            language="mr-IN"
        )
        assert res["handled_deterministically"] is True
        assert "थेट बस" in res["response_text"] or "मार्ग" in res["response_text"]
        assert "Datta Mandir" in res["response_text"]

    def test_18_hindi_query(self):
        """18. Hindi natural language route query."""
        context_engine.clear_context("test_sess_hi")
        res = context_engine.process_turn(
            "test_sess_hi",
            "मुझे दत्त मंदिर से नाशिक रोड जाना है।",
            language="hi-IN"
        )
        assert res["handled_deterministically"] is True
        assert "सीधी बस" in res["response_text"] or "रूट" in res["response_text"]
        assert "Datta Mandir" in res["response_text"]

    def test_19_english_query(self):
        """19. English natural language route query."""
        context_engine.clear_context("test_sess_en")
        res = context_engine.process_turn(
            "test_sess_en",
            "I want to travel from Datta Mandir to Nashik Road.",
            language="en-IN"
        )
        assert res["handled_deterministically"] is True
        assert "Route" in res["response_text"]
        assert "Datta Mandir" in res["response_text"]

    def test_20_user_correcting_mid_conversation(self):
        """20. User correcting origin or destination mid-conversation, plus contextual slot filling and follow-ups."""
        sess_id = "test_sess_mid_conv"
        context_engine.clear_context(sess_id)

        # Turn 1: User provides only destination
        t1 = context_engine.process_turn(sess_id, "मला सीबीएसला जायचे आहे", language="mr-IN")
        assert "सीबीएस" in t1["response_text"]
        assert "कोणत्या थांब्यावरून" in t1["response_text"]

        # Turn 2: User provides bare origin stop name
        t2 = context_engine.process_turn(sess_id, "दत्त मंदिर", language="mr-IN")
        assert "थेट बस" in t2["response_text"]
        assert "Datta Mandir" in t2["response_text"]
        assert "Cbs" in t2["response_text"]

        # Turn 3: User corrects destination to Shalimar keeping origin
        t3 = context_engine.process_turn(sess_id, "नाही, सीबीएस नाही, शालिमारला जायचं आहे", language="mr-IN")
        assert "Shalimar" in t3["response_text"]
        assert "Datta Mandir" in t3["response_text"]

        # Turn 4: User asks where to alight
        t4 = context_engine.process_turn(sess_id, "कुठे उतरायचं?", language="mr-IN")
        assert "थांब्यावर उतरावे" in t4["response_text"]

        # Turn 5: User asks how long the journey takes
        t5 = context_engine.process_turn(sess_id, "किती वेळ लागेल?", language="mr-IN")
        assert "मिनिटे" in t5["response_text"]

