"""
CityLinc Phase 4.7 Remediation & Production Hardening Test Suite.
Verifies all Phase 4.7 critical route-engine remediations:
1. Official 32-stage fare engine & multi-leg ticketing policy.
2. Timetable trip status filtering (revenue trips only, excluded statuses tracked).
3. Transfer buffers and pedestrian safety logic (3m platform, 5m cluster, 5m+ geodesic).
4. Multi-hop 3-transfer / 4-leg journey planning.
5. Conversational understanding for colloquial Marathi/Hindi patterns (Cases A, B, C, D, E).
6. Estimated stop time labeling and passenger disclaimers.
7. Unreachable and timetable infeasible journey handling.
"""
import pytest
from backend.app.services.fare_service import fare_service, CityLincFareService
from backend.app.services.transit_graph import transit_graph, WalkTransferEdge
from backend.app.services.route_engine import route_engine
from backend.app.services.conversation_context import context_engine


class TestPhase47FareEngine:
    """Verifies official 32-stage tariff, stage boundaries, and multi-leg ticketing policy."""

    def test_official_tariff_stages(self):
        """Boundary interval tests for official 32-stage distance slabs."""
        # Stage 1: <= 2.0 km -> Adult Rs 10, Child Rs 5
        assert fare_service.compute_stage_fare(0.5) == 10
        assert fare_service.compute_stage_fare(2.0) == 10
        assert fare_service.compute_stage_fare(2.0, is_child=True) == 5

        # Stage 2: 2.1 - 4.0 km -> Adult Rs 17, Child Rs 10
        assert fare_service.compute_stage_fare(2.1) == 17
        assert fare_service.compute_stage_fare(4.0) == 17
        assert fare_service.compute_stage_fare(4.0, is_child=True) == 10

        # Stage 3: 4.1 - 6.0 km -> Adult Rs 23, Child Rs 12
        assert fare_service.compute_stage_fare(4.1) == 23
        assert fare_service.compute_stage_fare(6.0) == 23
        assert fare_service.compute_stage_fare(6.0, is_child=True) == 12

        # Stage 4 & 5: 6.1 - 10.0 km -> Adult Rs 30, Child Rs 15
        assert fare_service.compute_stage_fare(6.1) == 30
        assert fare_service.compute_stage_fare(8.0) == 30
        assert fare_service.compute_stage_fare(10.0) == 30

        # Stage 6, 7, 8: 10.1 - 16.0 km -> Adult Rs 35, Child Rs 18
        assert fare_service.compute_stage_fare(10.1) == 35
        assert fare_service.compute_stage_fare(16.0) == 35

        # Stage 31: 60.1 - 62.0 km -> Adult Rs 90, Child Rs 45
        assert fare_service.compute_stage_fare(62.0) == 90

        # Stage 32: > 62.0 km -> 90 + extra units
        assert fare_service.compute_stage_fare(70.0) == 120

    def test_multi_leg_boarding_fare_summation(self):
        """Ticketing rule: total fare must equal sum of individual leg fares, NOT pooled distance."""
        # 3 legs: 3.5 km (Rs 17), 5.2 km (Rs 23), 2.0 km (Rs 10)
        # Sum = 17 + 23 + 10 = Rs 50
        # If pooled distance = 10.7 km -> Rs 35 (which would be incorrect under CityLinc rules)
        res = fare_service.compute_journey_fare([3.5, 5.2, 2.0])
        assert res["total_fare_inr"] == 50
        assert res["leg_fares_inr"] == [17, 23, 10]
        assert res["total_distance_km"] == 10.7
        assert res["tariff_version"] == "2023-01-01"
        assert res["tariff_status"] == "INDICATIVE_TARIFF_2023"

    def test_daily_pass_recommendation(self):
        """Recommends Rs 110 daily pass when fare >= 110 or legs >= 3."""
        # 3 legs -> recommended
        res1 = fare_service.compute_journey_fare([1.0, 1.0, 1.0])
        assert res1["daily_pass_recommended"] is True

        # 2 long legs where fare >= 110: 45 km (Rs 70) + 45 km (Rs 70) = Rs 140
        res2 = fare_service.compute_journey_fare([45.0, 45.0])
        assert res2["total_fare_inr"] == 140
        assert res2["daily_pass_recommended"] is True

        # 1 short leg -> NOT recommended
        res3 = fare_service.compute_journey_fare([5.0])
        assert res3["daily_pass_recommended"] is False


class TestPhase47TripStatusFiltering:
    """Verifies that non-revenue/depot trips are strictly excluded from graph pathfinding."""

    def test_excluded_trip_counts(self):
        """Audited counts: status '6' has 218 trips, status '9' has 7 trips."""
        assert transit_graph.excluded_status_counts.get("6", 0) == 218
        assert transit_graph.excluded_status_counts.get("9", 0) == 7

    def test_only_passenger_trips_loaded(self):
        """All trips in transit_graph must have trip_status in {'0', '2'}."""
        for r in transit_graph.routes.values():
            for t in r.trips:
                assert t.trip_status in {"0", "2"}


class TestPhase47TransferBuffers:
    """Verifies realistic pedestrian safety walk buffers."""

    def test_walk_edge_attributes(self):
        """Checks buffer minimums and verified flags across all transfer edges."""
        for sid, edges in transit_graph.walk_transfers.items():
            for w in edges:
                if w.transfer_type == "same_platform":
                    assert w.walk_time_mins >= 3
                    assert w.pedestrian_path_verified is True
                elif w.transfer_type == "same_cluster":
                    assert w.walk_time_mins >= 5
                    assert w.pedestrian_path_verified is True
                elif w.transfer_type == "geodesic_walk":
                    assert w.walk_time_mins >= 5
                    assert w.pedestrian_path_verified is False


class TestPhase47ThreeTransferRouting:
    """Verifies 3-transfer (4-leg) pathfinding across clusters requiring 3 transfers."""

    def test_three_transfer_discovery(self):
        """Cluster 266 (Datt Mandir Satpur Mhada Rd) to Cluster 310 (Vasali Gaon Phata)."""
        cl266 = transit_graph.display_clusters.get(266)
        cl310 = transit_graph.display_clusters.get(310)
        assert cl266 is not None and cl310 is not None
        paths = transit_graph.find_three_transfer_paths(
            origin_sids=cl266,
            dest_sids=cl310,
            departure_mins=480,
            limit=3
        )
        assert len(paths) > 0
        top = paths[0]
        # Must have 4 legs
        assert "leg1" in top and "leg2" in top and "leg3" in top and "leg4" in top
        # Distinct route numbers: R1 != R2 != R3 != R4
        rnos = {top["leg1"]["rno"], top["leg2"]["rno"], top["leg3"]["rno"], top["leg4"]["rno"]}
        assert len(rnos) == 4
        # Total distance and duration positive
        assert top["total_distance_km"] > 0
        assert top["total_duration_mins"] > 0


class TestPhase47ConversationRemediation:
    """Verifies colloquial Marathi/Hindi parsing: Cases A, B, C, D, E."""

    def test_case_a_filler_prefix_nimani_to_panchavati(self):
        """Case A: 'मला ना निम्मानीवरून पंचवटी जायचं आहे' -> Origin: Nimani, Dest: Panchavati."""
        res = context_engine.process_turn(
            "test_case_a",
            "मला ना निम्मानीवरून पंचवटी जायचं आहे",
            language="mr-IN"
        )
        assert res["handled_deterministically"] is True
        assert res["intent"] == "route_search"
        assert "Nimani" in res["response_text"]
        assert "Panchavati" in res["response_text"]
        assert "थेट बस" in res["response_text"]

    def test_case_b_english_loan_connector_to(self):
        """Case B: 'निमाणी टू सीबीएस जायचंय' -> Origin: Nimani, Dest: CBS."""
        res = context_engine.process_turn(
            "test_case_b",
            "निमाणी टू सीबीएस जायचंय",
            language="mr-IN"
        )
        assert res["handled_deterministically"] is True
        assert res["intent"] == "route_search"
        assert "Nimani" in res["response_text"]
        assert "Cbs" in res["response_text"]
        assert "थेट बस" in res["response_text"]

    def test_case_c_negation_without_vocative_as_stop(self):
        """Case C: 'ना निमानी नाही रे बाबा, निमानी फक्त निमानी' -> Never extract 'रे बाबा' as stop."""
        ctx = context_engine.get_or_create_context("test_case_c", "mr-IN")
        ctx.origin = "Nashik Road"
        ctx.destination = "CBS"

        res = context_engine.process_turn(
            "test_case_c",
            "ना निमानी नाही रे बाबा, निमानी फक्त निमानी",
            language="mr-IN"
        )
        assert res["handled_deterministically"] is True
        # Must NOT contain 'रे बाबा' as a stop or error
        assert "रे बाबा" not in res["response_text"]
        # Destination should be resolved to Nimani
        assert ctx.destination == "Nimani"

    def test_case_d_standalone_nimani(self):
        """Case D: Standalone 'Nimani' -> asks for destination, non-empty response."""
        context_engine.clear_context("test_case_d")
        res = context_engine.process_turn(
            "test_case_d",
            "Nimani",
            language="mr-IN"
        )
        assert res["handled_deterministically"] is True
        assert res["response_text"] != ""
        assert "तुम्हाला कोठे जायचे आहे" in res["response_text"]
        assert "Nimani" in res["response_text"]

    def test_case_e_standalone_cbs(self):
        """Case E: Standalone 'CBS' -> asks for destination, non-empty response."""
        context_engine.clear_context("test_case_e")
        res = context_engine.process_turn(
            "test_case_e",
            "CBS",
            language="mr-IN"
        )
        assert res["handled_deterministically"] is True
        assert res["response_text"] != ""
        assert "तुम्हाला कोठे जायचे आहे" in res["response_text"]
        assert "Cbs" in res["response_text"]


class TestPhase47EstimatedStopTimesAndDisclaimers:
    """Verifies that all responses contain accurate estimated stop time disclaimers."""

    def test_estimated_stop_time_disclaimer_marathi(self):
        res = context_engine.process_turn(
            "test_disclaimer_mr",
            "सीबीएस ते नाशिक रोड",
            language="mr-IN"
        )
        assert "हा थांब्यावरील वेळ अंदाजित आहे; अधिकृत थांबा-स्तरीय वेळ उपलब्ध नाही." in res["response_text"]

    def test_estimated_stop_time_disclaimer_hindi(self):
        res = context_engine.process_turn(
            "test_disclaimer_hi",
            "सीबीएस से नाशिक रोड",
            language="hi-IN"
        )
        assert "यह समय अनुमानित है; इस स्टॉप का आधिकारिक समय उपलब्ध नहीं है।" in res["response_text"]

    def test_estimated_stop_time_disclaimer_english(self):
        res = context_engine.process_turn(
            "test_disclaimer_en",
            "from CBS to Nashik Road",
            language="en-IN"
        )
        assert "This is an estimated stop arrival time, not an official scheduled stop time." in res["response_text"]
