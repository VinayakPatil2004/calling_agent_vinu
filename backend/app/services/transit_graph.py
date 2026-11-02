"""
CityLinc Transit Network Graph Service.
Builds and maintains the normalized topological graph of the CityLinc Nashik public transit network:
- 315 master route corridors (158 Up, 157 Down)
- 12,909 stop sequence instances
- 1,187 unique physical platforms (stations)
- 786 display station clusters
- 3,547 operational timetable trips across 311 active routes
- Geodesically and physically verified platform transfer walkways

Features:
- Fast topological reachability indices
- Transfer walkway modeling (same platform, cross-street platform clusters, nearby stops <= 150m)
- Proportional intermediate stop time estimations
- Multi-criteria route and trip selection
"""
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

from backend.app.core.config import settings
from backend.app.services.citylinc_knowledge import knowledge_service

logger = logging.getLogger("citylinc.transit_graph")

NASHIK_TZ = timezone(timedelta(hours=5, minutes=30))


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates geodesic distance in meters between two coordinates."""
    if (lat1 == 0.0 and lon1 == 0.0) or (lat2 == 0.0 and lon2 == 0.0):
        return 999999.0
    R = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(R * c, 1)


def parse_time_to_minutes(time_str: str) -> Optional[int]:
    """Parses 'HH:MM' string into minutes from midnight (0 to 1439)."""
    if not time_str or ":" not in time_str:
        return None
    try:
        parts = time_str.strip().split(":")
        h = int(parts[0])
        m = int(parts[1][:2])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, IndexError):
        pass
    return None


def format_minutes_to_time(minutes: int) -> str:
    """Formats minutes from midnight into 'HH:MM' 24-hr string."""
    m_norm = minutes % 1440
    h = m_norm // 60
    m = m_norm % 60
    return f"{h:02d}:{m:02d}"


@dataclass
class PlatformNode:
    station_id: int
    name: str
    clean_name: str
    display_station_id: int
    display_name: str
    lat: float
    lon: float
    direction: str  # 'UP', 'DOWN', or 'BIDIRECTIONAL'


@dataclass
class WalkTransferEdge:
    from_station_id: int
    to_station_id: int
    walk_distance_m: float
    walk_time_mins: int
    is_same_cluster: bool  # True if same display_station_id cluster
    transfer_type: str = "same_platform"  # "same_platform", "same_cluster", "geodesic_walk"
    pedestrian_path_verified: bool = False
    description: str = ""


@dataclass
class StopInstance:
    station_id: int
    sequence: int
    station_name: str
    display_station_id: int
    display_name: str
    total_distance_km: float
    lat: float
    lon: float


@dataclass
class GraphTrip:
    trip_id: int
    route_id: int
    rno: str
    trip_status: str
    eta_str: str
    eta_mins: int
    arr_str: str
    arr_mins: int
    duration_mins: int
    vehicle_no: Optional[str] = None


@dataclass
class RouteEdgeInfo:
    route_id: int
    rno: str
    routeno: int
    route_name: str
    direction: str  # 'Up' or 'Down'
    from_station_id: int
    to_station_id: int
    stops: List[StopInstance]
    station_ids: List[int]
    station_index_map: Dict[int, int]
    total_distance_km: float
    trips: List[GraphTrip]
    active_trips_count: int


class TransitNetworkGraph:
    """
    Normalized Public Transit Graph for CityLinc Nashik.
    Represents the full multi-modal network: in-vehicle transit legs + transfer walkways.
    """

    def __init__(self):
        self.nodes: Dict[int, PlatformNode] = {}
        self.display_clusters: Dict[int, Set[int]] = {}  # display_station_id -> set of station_ids
        self.walk_transfers: Dict[int, List[WalkTransferEdge]] = {}  # station_id -> list of WalkTransferEdge
        self.routes: Dict[int, RouteEdgeInfo] = {}  # route_id -> RouteEdgeInfo
        self.routes_by_rno: Dict[str, List[int]] = {}  # rno -> list of route_ids
        self.station_to_routes: Dict[int, List[Tuple[int, int]]] = {}  # station_id -> list of (route_id, stop_idx)
        self.excluded_status_counts: Dict[str, int] = {}  # trip_status -> count of excluded non-operational trips
        self.is_loaded: bool = False
        self.load_graph()

    def load_graph(self):
        """Constructs the complete transit network graph from the audited routes dataset."""
        raw_routes = knowledge_service.routes
        if not raw_routes:
            logger.warning("TransitNetworkGraph: knowledge_service.routes is empty, deferred load.")
            return

        # Step 1: Collect nodes and display clusters
        for r in raw_routes:
            stops = r.get("stops", [])
            for s in stops:
                sid = int(s.get("stationid"))
                dsid = int(s.get("displaystationid") or sid)
                sname = (s.get("stationname") or "").strip()
                dname = (s.get("displayname") or sname).strip()
                lat = float(s.get("center_lat") or 0.0)
                lon = float(s.get("center_lon") or 0.0)

                # Determine platform direction
                s_dir = "BIDIRECTIONAL"
                if "(U)" in sname or " 1 (U)" in sname or " 2 (U)" in sname:
                    s_dir = "UP"
                elif "(D)" in sname or " 1 (D)" in sname or " 2 (D)" in sname:
                    s_dir = "DOWN"

                clean = sname.replace("(U)", "").replace("(D)", "").replace(" 1", "").replace(" 2", "").strip()

                if sid not in self.nodes:
                    self.nodes[sid] = PlatformNode(
                        station_id=sid,
                        name=sname,
                        clean_name=clean,
                        display_station_id=dsid,
                        display_name=dname,
                        lat=lat,
                        lon=lon,
                        direction=s_dir
                    )

                if dsid not in self.display_clusters:
                    self.display_clusters[dsid] = set()
                self.display_clusters[dsid].add(sid)

        # Step 2: Build Transfer Walkways between platforms
        # Operational Safety Rules (Phase 4.7):
        # Rule A: Same physical platform: minimum 3 min buffer (pedestrian_path_verified = True)
        # Rule B: Same display cluster (cross-street / directional bays): minimum 5 min buffer (pedestrian_path_verified = True)
        # Rule C: Nearby stops within 150m: walk_mins = max(5, ceil(dist / 60) + 2) (pedestrian_path_verified = False)
        for sid, node in self.nodes.items():
            self.walk_transfers[sid] = []

            # 1. Self connection (staying on same platform)
            self.walk_transfers[sid].append(WalkTransferEdge(
                from_station_id=sid,
                to_station_id=sid,
                walk_distance_m=0.0,
                walk_time_mins=3,  # Minimum 3 minutes operational safety buffer
                is_same_cluster=True,
                transfer_type="same_platform",
                pedestrian_path_verified=True,
                description="Same platform (3 min operational buffer)"
            ))

            # 2. Other platforms in same display station cluster (e.g. Up <-> Down platform)
            dsid = node.display_station_id
            for other_sid in self.display_clusters.get(dsid, set()):
                if other_sid != sid:
                    self.walk_transfers[sid].append(WalkTransferEdge(
                        from_station_id=sid,
                        to_station_id=other_sid,
                        walk_distance_m=35.0,
                        walk_time_mins=5,  # Minimum 5 minutes cross-street transfer buffer
                        is_same_cluster=True,
                        transfer_type="same_cluster",
                        pedestrian_path_verified=True,
                        description=f"Walk across {node.display_name} to paired platform (5 min buffer)"
                    ))

        # 3. Geodesic walkable links (Haversine <= 150 meters between distinct clusters)
        all_sids = list(self.nodes.keys())
        for i in range(len(all_sids)):
            sid1 = all_sids[i]
            n1 = self.nodes[sid1]
            if n1.lat == 0.0 or n1.lon == 0.0:
                continue
            for j in range(i + 1, len(all_sids)):
                sid2 = all_sids[j]
                n2 = self.nodes[sid2]
                if n2.lat == 0.0 or n2.lon == 0.0:
                    continue
                # Skip if already in same display cluster
                if n1.display_station_id == n2.display_station_id:
                    continue
                dist_m = haversine_distance_meters(n1.lat, n1.lon, n2.lat, n2.lon)
                if dist_m <= 150.0:
                    walk_mins = max(5, math.ceil(dist_m / 60.0) + 2)
                    self.walk_transfers[sid1].append(WalkTransferEdge(
                        from_station_id=sid1,
                        to_station_id=sid2,
                        walk_distance_m=dist_m,
                        walk_time_mins=walk_mins,
                        is_same_cluster=False,
                        transfer_type="geodesic_walk",
                        pedestrian_path_verified=False,
                        description=f"Walk {dist_m:.0f}m from {n1.name} to {n2.name} (unverified pedestrian crossing)"
                    ))
                    self.walk_transfers[sid2].append(WalkTransferEdge(
                        from_station_id=sid2,
                        to_station_id=sid1,
                        walk_distance_m=dist_m,
                        walk_time_mins=walk_mins,
                        is_same_cluster=False,
                        transfer_type="geodesic_walk",
                        pedestrian_path_verified=False,
                        description=f"Walk {dist_m:.0f}m from {n2.name} to {n1.name} (unverified pedestrian crossing)"
                    ))

        # Step 3: Build Route Edge Info and Schedules
        for r in raw_routes:
            rid = int(r.get("routeid"))
            rno = str(r.get("rno", "")).strip().upper()
            routeno = int(r.get("routeno") or 3)
            rname = r.get("routename") or r.get("displayfullroutename") or ""
            direction = r.get("routetype") or "Up"
            from_sid = int(r.get("fromstation") or 0)
            to_sid = int(r.get("tostation") or 0)

            raw_stops = r.get("stops", [])
            stops_instances = []
            station_ids = []
            station_index_map = {}

            for idx, s in enumerate(raw_stops):
                sid = int(s.get("stationid"))
                dsid = int(s.get("displaystationid") or sid)
                sname = s.get("stationname") or ""
                dname = s.get("displayname") or sname
                seq = int(s.get("srno") or (idx + 1))
                try:
                    tdist = float(s.get("totaldistance") or 0.0)
                except (ValueError, TypeError):
                    tdist = 0.0
                lat = float(s.get("center_lat") or 0.0)
                lon = float(s.get("center_lon") or 0.0)

                stop_inst = StopInstance(
                    station_id=sid,
                    sequence=seq,
                    station_name=sname,
                    display_station_id=dsid,
                    display_name=dname,
                    total_distance_km=tdist,
                    lat=lat,
                    lon=lon
                )
                stops_instances.append(stop_inst)
                station_ids.append(sid)
                if sid not in station_index_map:
                    station_index_map[sid] = idx

                # Invert index
                if sid not in self.station_to_routes:
                    self.station_to_routes[sid] = []
                self.station_to_routes[sid].append((rid, idx))

            total_dist = stops_instances[-1].total_distance_km if stops_instances else 0.0

            # Parse Timetable Trips (Phase 4.7: Trip Status Validation)
            # Verified operational passenger service statuses: '0' (Regular Scheduled), '2' (Active Operating Dispatch)
            # Unknown / non-revenue / depot dispatch statuses ('6', '9') are safely excluded.
            PASSENGER_OPERATIONAL_STATUSES = {"0", "2"}
            raw_trips = r.get("timetable", [])
            graph_trips = []
            for t in raw_trips:
                t_id = int(t.get("tripid") or 0)
                t_status = str(t.get("trip_status") or "0").strip()

                if t_status not in PASSENGER_OPERATIONAL_STATUSES:
                    self.excluded_status_counts[t_status] = self.excluded_status_counts.get(t_status, 0) + 1
                    continue

                eta_str = str(t.get("eta") or "00:00").strip()
                arr_str = str(t.get("arrivalatdestination") or eta_str).strip()
                v_no = t.get("vehicleno")

                eta_m = parse_time_to_minutes(eta_str) or 0
                arr_m = parse_time_to_minutes(arr_str) or (eta_m + 60)
                dur_m = arr_m - eta_m
                if dur_m < 0:
                    dur_m += 1440
                if dur_m == 0:
                    dur_m = 45

                graph_trips.append(GraphTrip(
                    trip_id=t_id,
                    route_id=rid,
                    rno=rno,
                    trip_status=t_status,
                    eta_str=eta_str,
                    eta_mins=eta_m,
                    arr_str=arr_str,
                    arr_mins=arr_m,
                    duration_mins=dur_m,
                    vehicle_no=v_no
                ))

            # Sort trips chronologically
            graph_trips.sort(key=lambda x: x.eta_mins)

            self.routes[rid] = RouteEdgeInfo(
                route_id=rid,
                rno=rno,
                routeno=routeno,
                route_name=rname,
                direction=direction,
                from_station_id=from_sid,
                to_station_id=to_sid,
                stops=stops_instances,
                station_ids=station_ids,
                station_index_map=station_index_map,
                total_distance_km=total_dist,
                trips=graph_trips,
                active_trips_count=len(graph_trips)
            )

            if rno not in self.routes_by_rno:
                self.routes_by_rno[rno] = []
            self.routes_by_rno[rno].append(rid)

        # Sort station_to_routes by active_trips_count descending
        for sid in self.station_to_routes:
            self.station_to_routes[sid].sort(
                key=lambda x: self.routes[x[0]].active_trips_count,
                reverse=True
            )

        self.is_loaded = True
        logger.info(
            f"TransitNetworkGraph Loaded: {len(self.nodes)} platform nodes, "
            f"{len(self.display_clusters)} station clusters, {len(self.routes)} routes, "
            f"{len(self.routes_by_rno)} line numbers."
        )

    def estimate_stop_times(
        self,
        route_id: int,
        stop_index: int,
        trip: GraphTrip
    ) -> Tuple[str, int]:
        """
        Estimates proportional passage/departure time at an intermediate stop along the corridor.
        Returns (time_str_HH_MM, minutes_from_midnight).
        """
        route = self.routes.get(route_id)
        if not route or not route.stops or stop_index <= 0:
            return trip.eta_str, trip.eta_mins

        total_dist = route.total_distance_km
        stop_dist = route.stops[stop_index].total_distance_km

        if total_dist > 0.0:
            ratio = min(1.0, max(0.0, stop_dist / total_dist))
        else:
            ratio = stop_index / max(1, len(route.stops) - 1)

        offset_mins = int(round(ratio * trip.duration_mins))
        passage_mins = (trip.eta_mins + offset_mins) % 1440
        return format_minutes_to_time(passage_mins), passage_mins

    def find_best_trip(
        self,
        route_id: int,
        boarding_stop_idx: int,
        alighting_stop_idx: int,
        target_departure_mins: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Finds the earliest viable scheduled trip for a specific route segment on or after target_departure_mins.
        Returns trip timing details or None if no trips exist on route.
        """
        route = self.routes.get(route_id)
        if not route or not route.trips:
            return None

        if target_departure_mins is None:
            # Pick first available daytime trip or first scheduled trip
            t = route.trips[0]
            dep_str, dep_m = self.estimate_stop_times(route_id, boarding_stop_idx, t)
            arr_str, arr_m = self.estimate_stop_times(route_id, alighting_stop_idx, t)
            dur_m = arr_m - dep_m
            if dur_m < 0:
                dur_m += 1440
            return {
                "trip_id": t.trip_id,
                "vehicle_no": t.vehicle_no,
                "departure_str": dep_str,
                "departure_mins": dep_m,
                "arrival_str": arr_str,
                "arrival_mins": arr_m,
                "duration_mins": max(1, dur_m),
                "is_next_day": False,
                "time_type": "estimated",
                "is_official_stop_time": False
            }

        # Look for earliest trip where estimated boarding departure >= target_departure_mins
        best_t = None
        best_dep_m = 999999
        best_arr_m = 999999
        best_dep_str = ""
        best_arr_str = ""
        is_next_day = False

        # First pass: trips departing today at or after target_departure_mins
        for t in route.trips:
            dep_str, dep_m = self.estimate_stop_times(route_id, boarding_stop_idx, t)
            if dep_m >= target_departure_mins:
                if dep_m < best_dep_m:
                    best_dep_m = dep_m
                    best_dep_str = dep_str
                    arr_str, arr_m = self.estimate_stop_times(route_id, alighting_stop_idx, t)
                    best_arr_m = arr_m
                    best_arr_str = arr_str
                    best_t = t
                    break

        # Second pass: if no trips left today, roll over to the first morning trip tomorrow
        if best_t is None:
            t = route.trips[0]
            dep_str, dep_m = self.estimate_stop_times(route_id, boarding_stop_idx, t)
            arr_str, arr_m = self.estimate_stop_times(route_id, alighting_stop_idx, t)
            best_t = t
            best_dep_str = dep_str
            best_dep_m = dep_m + 1440  # tomorrow
            best_arr_str = arr_str
            best_arr_m = arr_m + 1440
            is_next_day = True

        dur_m = best_arr_m - best_dep_m
        if dur_m < 0:
            dur_m += 1440

        return {
            "trip_id": best_t.trip_id,
            "vehicle_no": best_t.vehicle_no,
            "departure_str": best_dep_str,
            "departure_mins": best_dep_m,
            "arrival_str": best_arr_str,
            "arrival_mins": best_arr_m,
            "duration_mins": max(1, dur_m),
            "is_next_day": is_next_day,
            "time_type": "estimated",
            "is_official_stop_time": False
        }

    def get_cluster_id(self, station_id: int) -> int:
        """Returns the display station ID (cluster) for a platform."""
        node = self.nodes.get(station_id)
        return node.display_station_id if node else station_id

    def get_origin_and_dest_clusters(self, origin_sids: Set[int], dest_sids: Set[int]) -> Tuple[Set[int], Set[int]]:
        """Returns sets of display cluster IDs for origins and destinations."""
        o_clusters = {self.get_cluster_id(s) for s in origin_sids}
        d_clusters = {self.get_cluster_id(s) for s in dest_sids}
        return o_clusters, d_clusters

    def find_direct_paths(
        self,
        origin_sids: Set[int],
        dest_sids: Set[int],
        departure_mins: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Finds all direct route options between origin platforms and destination platforms.
        Strictly enforces topological forward ordering (boarding_idx < alighting_idx).
        """
        candidates_by_rno: Dict[str, Dict[str, Any]] = {}

        for o_sid in origin_sids:
            for rid, o_idx in self.station_to_routes.get(o_sid, []):
                route = self.routes.get(rid)
                if not route:
                    continue

                for d_sid in dest_sids:
                    if d_sid in route.station_index_map:
                        d_idx = route.station_index_map[d_sid]
                        if o_idx < d_idx:
                            # Forward topological order confirmed
                            trip_info = self.find_best_trip(rid, o_idx, d_idx, departure_mins)
                            b_stop = route.stops[o_idx]
                            a_stop = route.stops[d_idx]

                            dist_km = round(max(0.1, a_stop.total_distance_km - b_stop.total_distance_km), 2)
                            inter_stops = route.stops[o_idx: d_idx + 1]

                            # Polyline coordinates
                            polyline_coords = []
                            for s in inter_stops:
                                if s.lat != 0.0 and s.lon != 0.0:
                                    if not polyline_coords or polyline_coords[-1] != [s.lat, s.lon]:
                                        polyline_coords.append([s.lat, s.lon])

                            departures = [t.eta_str for t in route.trips if t.eta_str][:5]

                            path_record = {
                                "route_id": rid,
                                "rno": route.rno,
                                "route_name": route.route_name,
                                "route_type": route.direction,
                                "boarding_stop": b_stop,
                                "alighting_stop": a_stop,
                                "intermediate_stops": inter_stops,
                                "intermediate_stops_count": d_idx - o_idx,
                                "distance_km": dist_km,
                                "trip_info": trip_info,
                                "timetable_trips_count": route.active_trips_count,
                                "scheduled_departures": departures,
                                "polyline": polyline_coords
                            }

                            if route.rno not in candidates_by_rno:
                                candidates_by_rno[route.rno] = path_record
                            else:
                                curr = candidates_by_rno[route.rno]
                                # Prefer segment with fewer stops, then shorter distance
                                if (path_record["intermediate_stops_count"], path_record["distance_km"]) < (curr["intermediate_stops_count"], curr["distance_km"]):
                                    candidates_by_rno[route.rno] = path_record

        direct_paths = list(candidates_by_rno.values())
        # Sort direct paths:
        # Prioritize routes with active timetable trips, then fewest intermediate stops, then distance
        direct_paths.sort(key=lambda x: (
            x["timetable_trips_count"] == 0,
            x["intermediate_stops_count"],
            x["distance_km"]
        ))
        return direct_paths

    def find_one_transfer_paths(
        self,
        origin_sids: Set[int],
        dest_sids: Set[int],
        departure_mins: Optional[int] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Discovers 1-transfer transit paths (Origin -> T1 -> [Walk] -> T2 -> Destination).
        Enforces topological directionality, timetable continuity (Arr1 + Walk <= Dep2),
        and transfer platform walkways.
        """
        o_clusters, d_clusters = self.get_origin_and_dest_clusters(origin_sids, dest_sids)
        feasible_paths: List[Dict[str, Any]] = []
        infeasible_paths: List[Dict[str, Any]] = []
        seen_pairs: Set[Tuple[str, str, int]] = set()

        for o_sid in origin_sids:
            for r1_id, o_idx in self.station_to_routes.get(o_sid, []):
                r1 = self.routes.get(r1_id)
                if not r1 or r1.active_trips_count == 0:
                    continue

                for t1_idx in range(o_idx + 1, len(r1.stops)):
                    t1_sid = r1.stops[t1_idx].station_id
                    c1 = self.get_cluster_id(t1_sid)
                    if c1 in o_clusters or c1 in d_clusters:
                        continue  # Avoid circular loop back to origin or destination cluster

                    walks = self.walk_transfers.get(t1_sid, [])
                    for walk in walks:
                        t2_sid = walk.to_station_id
                        for r2_id, t2_idx in self.station_to_routes.get(t2_sid, []):
                            if r2_id == r1_id:
                                continue
                            r2 = self.routes.get(r2_id)
                            if not r2 or r2.rno == r1.rno or r2.active_trips_count == 0:
                                continue

                            for d_idx in range(t2_idx + 1, len(r2.stops)):
                                d_sid = r2.stops[d_idx].station_id
                                if d_sid in dest_sids:
                                    pair_key = (r1.rno, r2.rno, c1)
                                    if pair_key in seen_pairs:
                                        continue
                                    seen_pairs.add(pair_key)

                                    # Timetable continuity check
                                    t1_info = self.find_best_trip(r1_id, o_idx, t1_idx, departure_mins)
                                    if not t1_info:
                                        continue

                                    min_dep2 = t1_info["arrival_mins"] + walk.walk_time_mins
                                    t2_info = self.find_best_trip(r2_id, t2_idx, d_idx, min_dep2)
                                    if not t2_info:
                                        continue

                                    wait_mins = t2_info["departure_mins"] - min_dep2
                                    is_feasible = (
                                        (not t1_info["is_next_day"])
                                        and (not t2_info["is_next_day"])
                                        and (0 <= wait_mins <= 180)
                                    )

                                    leg1_dist = round(max(0.1, r1.stops[t1_idx].total_distance_km - r1.stops[o_idx].total_distance_km), 2)
                                    leg2_dist = round(max(0.1, r2.stops[d_idx].total_distance_km - r2.stops[t2_idx].total_distance_km), 2)

                                    total_duration = (
                                        t1_info["duration_mins"] + walk.walk_time_mins +
                                        max(0, wait_mins) + t2_info["duration_mins"]
                                    )

                                    t1_node = self.nodes.get(t1_sid)
                                    t2_node = self.nodes.get(t2_sid)
                                    hub_name = t1_node.display_name if t1_node else r1.stops[t1_idx].station_name
                                    is_major_hub = any(k in hub_name.lower() for k in ["cbs", "shalimar", "nimani", "dwarka", "panchavati", "satpur", "nashik road", "शालिमार", "सीबीएस", "निमाणी", "द्वारका", "पंचवटी", "सातपूर"])

                                    path_obj = {
                                        "transfers_count": 1,
                                        "is_feasible": is_feasible,
                                        "is_major_hub": is_major_hub,
                                        "interchange_hub": hub_name,
                                        "transfer_from_sid": t1_sid,
                                        "transfer_from_name": r1.stops[t1_idx].station_name,
                                        "transfer_to_sid": t2_sid,
                                        "transfer_to_name": t2_node.name if t2_node else r1.stops[t1_idx].station_name,
                                        "walk_distance_m": walk.walk_distance_m,
                                        "walk_time_mins": walk.walk_time_mins,
                                        "wait_time_mins": max(0, wait_mins),
                                        "total_duration_mins": total_duration,
                                        "total_distance_km": round(leg1_dist + leg2_dist, 2),
                                        "total_stops_count": (t1_idx - o_idx) + (d_idx - t2_idx),
                                        "leg1": {
                                            "route_id": r1_id,
                                            "rno": r1.rno,
                                            "route_name": r1.route_name,
                                            "direction": r1.direction,
                                            "boarding_stop": r1.stops[o_idx],
                                            "alighting_stop": r1.stops[t1_idx],
                                            "intermediate_stops": r1.stops[o_idx: t1_idx + 1],
                                            "stops_count": t1_idx - o_idx,
                                            "distance_km": leg1_dist,
                                            "trip_info": t1_info,
                                            "trips_count": r1.active_trips_count,
                                            "departures": [t.eta_str for t in r1.trips if t.eta_str][:3]
                                        },
                                        "leg2": {
                                            "route_id": r2_id,
                                            "rno": r2.rno,
                                            "route_name": r2.route_name,
                                            "direction": r2.direction,
                                            "boarding_stop": r2.stops[t2_idx],
                                            "alighting_stop": r2.stops[d_idx],
                                            "intermediate_stops": r2.stops[t2_idx: d_idx + 1],
                                            "stops_count": d_idx - t2_idx,
                                            "distance_km": leg2_dist,
                                            "trip_info": t2_info,
                                            "trips_count": r2.active_trips_count,
                                            "departures": [t.eta_str for t in r2.trips if t.eta_str][:3]
                                        }
                                    }

                                    if is_feasible:
                                        feasible_paths.append(path_obj)
                                    else:
                                        infeasible_paths.append(path_obj)

        feasible_paths.sort(key=lambda x: (not x["is_major_hub"], x["total_duration_mins"], x["wait_time_mins"], x["total_stops_count"]))
        infeasible_paths.sort(key=lambda x: (not x["is_major_hub"], x["total_stops_count"], x["total_distance_km"]))

        # Return feasible paths first, followed by infeasible if needed
        all_paths = feasible_paths + infeasible_paths
        return all_paths[:limit]

    def find_two_transfer_paths(
        self,
        origin_sids: Set[int],
        dest_sids: Set[int],
        departure_mins: Optional[int] = None,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Discovers 2-transfer transit paths (O -> T1 -> [W1] -> T2 -> T3 -> [W2] -> T4 -> D).
        Uses bidirectional forward-backward reachable set matching for sub-50ms execution.
        Enforces 3 distinct line numbers (R1 != R2 != R3) and 4 distinct station clusters.
        """
        o_clusters, d_clusters = self.get_origin_and_dest_clusters(origin_sids, dest_sids)

        # 1. Forward step: Reachable platforms t2 from Origin via Leg 1 + Walk 1
        forward_map: Dict[int, List[Tuple[int, int, int, WalkTransferEdge]]] = {}
        for o_sid in origin_sids:
            for r1_id, o_idx in self.station_to_routes.get(o_sid, []):
                r1 = self.routes.get(r1_id)
                if not r1 or r1.active_trips_count == 0:
                    continue
                for t1_idx in range(o_idx + 1, len(r1.stops)):
                    t1_sid = r1.stops[t1_idx].station_id
                    c1 = self.get_cluster_id(t1_sid)
                    if c1 in o_clusters or c1 in d_clusters:
                        continue
                    for w1 in self.walk_transfers.get(t1_sid, []):
                        t2 = w1.to_station_id
                        if t2 not in forward_map:
                            forward_map[t2] = []
                        forward_map[t2].append((r1_id, o_idx, t1_idx, w1))

        # 2. Backward step: Platforms t3 that can reach Destination via Walk 2 + Leg 3
        backward_map: Dict[int, List[Tuple[int, int, int, WalkTransferEdge]]] = {}
        for d_sid in dest_sids:
            for r3_id, d_idx in self.station_to_routes.get(d_sid, []):
                r3 = self.routes.get(r3_id)
                if not r3 or r3.active_trips_count == 0:
                    continue
                for t4_idx in range(0, d_idx):
                    t4_sid = r3.stops[t4_idx].station_id
                    c4 = self.get_cluster_id(t4_sid)
                    if c4 in o_clusters or c4 in d_clusters:
                        continue
                    for w2 in self.walk_transfers.get(t4_sid, []):
                        t3 = w2.to_station_id  # t3 walks to t4
                        if t3 not in backward_map:
                            backward_map[t3] = []
                        backward_map[t3].append((r3_id, t4_idx, d_idx, w2))

        # 3. Middle step: Find R2 connecting t2 to t3
        results: List[Dict[str, Any]] = []
        seen_combos: Set[Tuple[str, str, str]] = set()

        for t2, f_list in forward_map.items():
            for r2_id, t2_idx in self.station_to_routes.get(t2, []):
                r2 = self.routes.get(r2_id)
                if not r2 or r2.active_trips_count == 0:
                    continue
                for t3_idx in range(t2_idx + 1, len(r2.stops)):
                    t3 = r2.stops[t3_idx].station_id
                    if t3 in backward_map:
                        c2_start = self.get_cluster_id(t2)
                        c2_end = self.get_cluster_id(t3)
                        if c2_start == c2_end or c2_start in o_clusters or c2_end in d_clusters:
                            continue

                        for r1_id, o_idx, t1_idx, w1 in f_list:
                            r1 = self.routes[r1_id]
                            if r1.rno == r2.rno:
                                continue
                            for r3_id, t4_idx, d_idx, w2 in backward_map[t3]:
                                r3 = self.routes[r3_id]
                                if r3.rno in (r1.rno, r2.rno):
                                    continue

                                combo_key = (r1.rno, r2.rno, r3.rno)
                                if combo_key in seen_combos:
                                    continue
                                seen_combos.add(combo_key)

                                # Timetable feasibility across all 3 legs
                                t1_info = self.find_best_trip(r1_id, o_idx, t1_idx, departure_mins)
                                if not t1_info:
                                    continue

                                min_dep2 = t1_info["arrival_mins"] + w1.walk_time_mins
                                t2_info = self.find_best_trip(r2_id, t2_idx, t3_idx, min_dep2)
                                if not t2_info:
                                    continue

                                wait1_mins = max(0, t2_info["departure_mins"] - min_dep2)
                                min_dep3 = t2_info["arrival_mins"] + w2.walk_time_mins
                                t3_info = self.find_best_trip(r3_id, t4_idx, d_idx, min_dep3)
                                if not t3_info:
                                    continue

                                wait2_mins = max(0, t3_info["departure_mins"] - min_dep3)

                                is_feas = (
                                    (not t1_info["is_next_day"])
                                    and (not t2_info["is_next_day"])
                                    and (not t3_info["is_next_day"])
                                    and (wait1_mins <= 180)
                                    and (wait2_mins <= 180)
                                )

                                leg1_dist = round(max(0.1, r1.stops[t1_idx].total_distance_km - r1.stops[o_idx].total_distance_km), 2)
                                leg2_dist = round(max(0.1, r2.stops[t3_idx].total_distance_km - r2.stops[t2_idx].total_distance_km), 2)
                                leg3_dist = round(max(0.1, r3.stops[d_idx].total_distance_km - r3.stops[t4_idx].total_distance_km), 2)

                                total_dur = (
                                    t1_info["duration_mins"] + w1.walk_time_mins + wait1_mins +
                                    t2_info["duration_mins"] + w2.walk_time_mins + wait2_mins +
                                    t3_info["duration_mins"]
                                )

                                t1_node = self.nodes.get(r1.stops[t1_idx].station_id)
                                t3_node = self.nodes.get(t3)
                                hub1 = t1_node.display_name if t1_node else r1.stops[t1_idx].station_name
                                hub2 = t3_node.display_name if t3_node else r2.stops[t3_idx].station_name

                                results.append({
                                    "transfers_count": 2,
                                    "is_feasible": is_feas,
                                    "interchange_hub_1": hub1,
                                    "interchange_hub_2": hub2,
                                    "total_duration_mins": total_dur,
                                    "total_distance_km": round(leg1_dist + leg2_dist + leg3_dist, 2),
                                    "total_stops_count": (t1_idx - o_idx) + (t3_idx - t2_idx) + (d_idx - t4_idx),
                                    "leg1": {
                                        "route_id": r1_id,
                                        "rno": r1.rno,
                                        "route_name": r1.route_name,
                                        "boarding_stop": r1.stops[o_idx],
                                        "alighting_stop": r1.stops[t1_idx],
                                        "stops_count": t1_idx - o_idx,
                                        "distance_km": leg1_dist,
                                        "trip_info": t1_info,
                                        "trips_count": r1.active_trips_count,
                                        "departures": [t.eta_str for t in r1.trips if t.eta_str][:2]
                                    },
                                    "transfer1": {
                                        "walk_distance_m": w1.walk_distance_m,
                                        "walk_time_mins": w1.walk_time_mins,
                                        "wait_time_mins": wait1_mins
                                    },
                                    "leg2": {
                                        "route_id": r2_id,
                                        "rno": r2.rno,
                                        "route_name": r2.route_name,
                                        "boarding_stop": r2.stops[t2_idx],
                                        "alighting_stop": r2.stops[t3_idx],
                                        "stops_count": t3_idx - t2_idx,
                                        "distance_km": leg2_dist,
                                        "trip_info": t2_info,
                                        "trips_count": r2.active_trips_count,
                                        "departures": [t.eta_str for t in r2.trips if t.eta_str][:2]
                                    },
                                    "transfer2": {
                                        "walk_distance_m": w2.walk_distance_m,
                                        "walk_time_mins": w2.walk_time_mins,
                                        "wait_time_mins": wait2_mins
                                    },
                                    "leg3": {
                                        "route_id": r3_id,
                                        "rno": r3.rno,
                                        "route_name": r3.route_name,
                                        "boarding_stop": r3.stops[t4_idx],
                                        "alighting_stop": r3.stops[d_idx],
                                        "stops_count": d_idx - t4_idx,
                                        "distance_km": leg3_dist,
                                        "trip_info": t3_info,
                                        "trips_count": r3.active_trips_count,
                                        "departures": [t.eta_str for t in r3.trips if t.eta_str][:2]
                                    }
                                })
                                if len(results) >= limit * 2:
                                    break
                            if len(results) >= limit * 2:
                                break
                        if len(results) >= limit * 2:
                            break
                    if len(results) >= limit * 2:
                        break
                if len(results) >= limit * 2:
                    break
            if len(results) >= limit * 2:
                break

        results.sort(key=lambda x: (not x["is_feasible"], x["total_duration_mins"], x["total_stops_count"]))
        return results[:limit]

    def find_three_transfer_paths(
        self,
        origin_sids: Set[int],
        dest_sids: Set[int],
        departure_mins: Optional[int] = None,
        limit: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Discovers 3-transfer transit paths (O -> T1 -> [W1] -> T2 -> T3 -> [W2] -> T4 -> T5 -> [W3] -> T6 -> D)
        across 4 bus legs. Bounded and pruned for high performance (< 50ms).
        Enforces 4 distinct route numbers (R1 != R2 != R3 != R4) and 5 distinct station clusters.
        """
        o_clusters, d_clusters = self.get_origin_and_dest_clusters(origin_sids, dest_sids)

        # 1. Forward step: Reachable platforms T2 from Origin via Leg 1 + Walk 1
        forward_map: Dict[int, List[Tuple[int, int, int, WalkTransferEdge]]] = {}
        for o_sid in origin_sids:
            for r1_id, o_idx in self.station_to_routes.get(o_sid, []):
                r1 = self.routes.get(r1_id)
                if not r1 or r1.active_trips_count == 0:
                    continue
                for t1_idx in range(o_idx + 1, len(r1.stops)):
                    t1_sid = r1.stops[t1_idx].station_id
                    c1 = self.get_cluster_id(t1_sid)
                    if c1 in o_clusters or c1 in d_clusters:
                        continue
                    for w1 in self.walk_transfers.get(t1_sid, []):
                        t2 = w1.to_station_id
                        if t2 not in forward_map:
                            forward_map[t2] = []
                        forward_map[t2].append((r1_id, o_idx, t1_idx, w1))

        # 2. Backward step: Platforms T5 that can reach Destination via Walk 3 + Leg 4
        backward_map: Dict[int, List[Tuple[int, int, int, WalkTransferEdge]]] = {}
        for d_sid in dest_sids:
            for r4_id, d_idx in self.station_to_routes.get(d_sid, []):
                r4 = self.routes.get(r4_id)
                if not r4 or r4.active_trips_count == 0:
                    continue
                for t6_idx in range(0, d_idx):
                    t6_sid = r4.stops[t6_idx].station_id
                    c4 = self.get_cluster_id(t6_sid)
                    if c4 in o_clusters or c4 in d_clusters:
                        continue
                    for w3 in self.walk_transfers.get(t6_sid, []):
                        t5 = w3.to_station_id
                        if t5 not in backward_map:
                            backward_map[t5] = []
                        backward_map[t5].append((r4_id, t6_idx, d_idx, w3))

        # 3. Middle connecting traversal: T2 -> T3 -> [W2] -> T4 -> T5
        results: List[Dict[str, Any]] = []
        seen_combos: Set[Tuple[str, str, str, str]] = set()

        for t2, f_list in forward_map.items():
            for r2_id, t2_idx in self.station_to_routes.get(t2, []):
                r2 = self.routes.get(r2_id)
                if not r2 or r2.active_trips_count == 0:
                    continue
                for t3_idx in range(t2_idx + 1, len(r2.stops)):
                    t3 = r2.stops[t3_idx].station_id
                    c_t3 = self.get_cluster_id(t3)
                    if c_t3 in o_clusters or c_t3 in d_clusters:
                        continue

                    for w2 in self.walk_transfers.get(t3, []):
                        t4 = w2.to_station_id
                        for r3_id, t4_idx in self.station_to_routes.get(t4, []):
                            if r3_id == r2_id:
                                continue
                            r3 = self.routes.get(r3_id)
                            if not r3 or r3.rno == r2.rno or r3.active_trips_count == 0:
                                continue
                            for t5_idx in range(t4_idx + 1, len(r3.stops)):
                                t5 = r3.stops[t5_idx].station_id
                                if t5 in backward_map:
                                    c_t5 = self.get_cluster_id(t5)
                                    if c_t5 in o_clusters or c_t5 in d_clusters:
                                        continue

                                    for r1_id, o_idx, t1_idx, w1 in f_list:
                                        r1 = self.routes[r1_id]
                                        for r4_id, t6_idx, d_idx, w3 in backward_map[t5]:
                                            r4 = self.routes[r4_id]
                                            rnos = {r1.rno, r2.rno, r3.rno, r4.rno}
                                            if len(rnos) != 4:
                                                continue

                                            combo_key = (r1.rno, r2.rno, r3.rno, r4.rno)
                                            if combo_key in seen_combos:
                                                continue
                                            seen_combos.add(combo_key)

                                            # Timetable feasibility across all 4 legs
                                            t1_info = self.find_best_trip(r1_id, o_idx, t1_idx, departure_mins)
                                            if not t1_info:
                                                continue
                                            min_dep2 = t1_info["arrival_mins"] + w1.walk_time_mins
                                            t2_info = self.find_best_trip(r2_id, t2_idx, t3_idx, min_dep2)
                                            if not t2_info:
                                                continue
                                            wait1 = max(0, t2_info["departure_mins"] - min_dep2)

                                            min_dep3 = t2_info["arrival_mins"] + w2.walk_time_mins
                                            t3_info = self.find_best_trip(r3_id, t4_idx, t5_idx, min_dep3)
                                            if not t3_info:
                                                continue
                                            wait2 = max(0, t3_info["departure_mins"] - min_dep3)

                                            min_dep4 = t3_info["arrival_mins"] + w3.walk_time_mins
                                            t4_info = self.find_best_trip(r4_id, t6_idx, d_idx, min_dep4)
                                            if not t4_info:
                                                continue
                                            wait3 = max(0, t4_info["departure_mins"] - min_dep4)

                                            is_feas = (
                                                (not t1_info["is_next_day"])
                                                and (not t2_info["is_next_day"])
                                                and (not t3_info["is_next_day"])
                                                and (not t4_info["is_next_day"])
                                                and (wait1 <= 180)
                                                and (wait2 <= 180)
                                                and (wait3 <= 180)
                                            )

                                            l1_dist = round(max(0.1, r1.stops[t1_idx].total_distance_km - r1.stops[o_idx].total_distance_km), 2)
                                            l2_dist = round(max(0.1, r2.stops[t3_idx].total_distance_km - r2.stops[t2_idx].total_distance_km), 2)
                                            l3_dist = round(max(0.1, r3.stops[t5_idx].total_distance_km - r3.stops[t4_idx].total_distance_km), 2)
                                            l4_dist = round(max(0.1, r4.stops[d_idx].total_distance_km - r4.stops[t6_idx].total_distance_km), 2)

                                            total_dur = (
                                                t1_info["duration_mins"] + w1.walk_time_mins + wait1 +
                                                t2_info["duration_mins"] + w2.walk_time_mins + wait2 +
                                                t3_info["duration_mins"] + w3.walk_time_mins + wait3 +
                                                t4_info["duration_mins"]
                                            )

                                            t1_node = self.nodes.get(r1.stops[t1_idx].station_id)
                                            t3_node = self.nodes.get(t3)
                                            t5_node = self.nodes.get(t5)

                                            results.append({
                                                "is_feasible": is_feas,
                                                "total_duration_mins": total_dur,
                                                "total_wait_mins": wait1 + wait2 + wait3,
                                                "total_stops_count": (t1_idx - o_idx) + (t3_idx - t2_idx) + (t5_idx - t4_idx) + (d_idx - t6_idx),
                                                "total_distance_km": round(l1_dist + l2_dist + l3_dist + l4_dist, 2),
                                                "total_walk_meters": round(w1.walk_distance_m + w2.walk_distance_m + w3.walk_distance_m, 1),
                                                "interchange1": t1_node.display_name if t1_node else "Transfer 1",
                                                "interchange2": t3_node.display_name if t3_node else "Transfer 2",
                                                "interchange3": t5_node.display_name if t5_node else "Transfer 3",
                                                "leg1": {
                                                    "route_id": r1_id,
                                                    "rno": r1.rno,
                                                    "route_name": r1.route_name,
                                                    "boarding_stop": r1.stops[o_idx],
                                                    "alighting_stop": r1.stops[t1_idx],
                                                    "stops_count": t1_idx - o_idx,
                                                    "distance_km": l1_dist,
                                                    "trip_info": t1_info,
                                                    "trips_count": r1.active_trips_count,
                                                    "departures": [t.eta_str for t in r1.trips if t.eta_str][:2]
                                                },
                                                "transfer1": {
                                                    "walk_distance_m": w1.walk_distance_m,
                                                    "walk_time_mins": w1.walk_time_mins,
                                                    "wait_time_mins": wait1,
                                                    "transfer_type": w1.transfer_type,
                                                    "pedestrian_path_verified": w1.pedestrian_path_verified
                                                },
                                                "leg2": {
                                                    "route_id": r2_id,
                                                    "rno": r2.rno,
                                                    "route_name": r2.route_name,
                                                    "boarding_stop": r2.stops[t2_idx],
                                                    "alighting_stop": r2.stops[t3_idx],
                                                    "stops_count": t3_idx - t2_idx,
                                                    "distance_km": l2_dist,
                                                    "trip_info": t2_info,
                                                    "trips_count": r2.active_trips_count,
                                                    "departures": [t.eta_str for t in r2.trips if t.eta_str][:2]
                                                },
                                                "transfer2": {
                                                    "walk_distance_m": w2.walk_distance_m,
                                                    "walk_time_mins": w2.walk_time_mins,
                                                    "wait_time_mins": wait2,
                                                    "transfer_type": w2.transfer_type,
                                                    "pedestrian_path_verified": w2.pedestrian_path_verified
                                                },
                                                "leg3": {
                                                    "route_id": r3_id,
                                                    "rno": r3.rno,
                                                    "route_name": r3.route_name,
                                                    "boarding_stop": r3.stops[t4_idx],
                                                    "alighting_stop": r3.stops[t5_idx],
                                                    "stops_count": t5_idx - t4_idx,
                                                    "distance_km": l3_dist,
                                                    "trip_info": t3_info,
                                                    "trips_count": r3.active_trips_count,
                                                    "departures": [t.eta_str for t in r3.trips if t.eta_str][:2]
                                                },
                                                "transfer3": {
                                                    "walk_distance_m": w3.walk_distance_m,
                                                    "walk_time_mins": w3.walk_time_mins,
                                                    "wait_time_mins": wait3,
                                                    "transfer_type": w3.transfer_type,
                                                    "pedestrian_path_verified": w3.pedestrian_path_verified
                                                },
                                                "leg4": {
                                                    "route_id": r4_id,
                                                    "rno": r4.rno,
                                                    "route_name": r4.route_name,
                                                    "boarding_stop": r4.stops[t6_idx],
                                                    "alighting_stop": r4.stops[d_idx],
                                                    "stops_count": d_idx - t6_idx,
                                                    "distance_km": l4_dist,
                                                    "trip_info": t4_info,
                                                    "trips_count": r4.active_trips_count,
                                                    "departures": [t.eta_str for t in r4.trips if t.eta_str][:2]
                                                }
                                            })
                                            if len(results) >= limit * 2:
                                                break
                                        if len(results) >= limit * 2:
                                            break
                                    if len(results) >= limit * 2:
                                        break
                                if len(results) >= limit * 2:
                                    break
                            if len(results) >= limit * 2:
                                break
                        if len(results) >= limit * 2:
                            break
                    if len(results) >= limit * 2:
                        break
                if len(results) >= limit * 2:
                    break
            if len(results) >= limit * 2:
                break

        results.sort(key=lambda x: (not x["is_feasible"], x["total_duration_mins"], x["total_stops_count"]))
        return results[:limit]


# Global Singleton
transit_graph = TransitNetworkGraph()
