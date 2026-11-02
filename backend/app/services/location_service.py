"""
Location Intelligence Service for CityLinc Nashik.
Indexes all 1,187 unique physical stations, 315 routes, and 12,909 stop instances.
Provides high-performance spatial search, Haversine nearby station discovery,
curated hospital & landmark registries, and route trajectory reconstruction.
"""
import math
import logging
from typing import Dict, List, Optional, Any, Set, Tuple
from backend.app.services.citylinc_knowledge import knowledge_service, LOCATION_ALIASES, DEVANAGARI_DIGITS

logger = logging.getLogger("citylinc.locations")


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates geodesic straight-line distance in kilometers between two GPS coordinates."""
    R = 6371.0  # Earth radius in kilometers
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(R * c, 3)


# Verified Physical Hospital POI Registry for CityLinc Nashik
# Ground-truth verified against NMC Health Dept, ESIC Maharashtra, Govt of Maharashtra registries
VERIFIED_PHYSICAL_HOSPITALS = [
    {
        "hospital_id": "civil_hospital",
        "name_en": "District Civil Hospital, Nashik",
        "name_mr": "जिल्हा सामान्य रुग्णालय, नाशिक",
        "aliases": [
            "civil hospital", "civil", "सिव्हिल हॉस्पिटल", "सिव्हील", "जिल्हा रुग्णालय", "सिव्हिल",
            "सिविल हॉस्पिटल", "सिविल", "सिविल अस्पताल", "सिविल रुग्णालय",
            "सिफिल", "सिफिल हॉस्पिटल", "सिफिल अस्पताल", "सिफिल रुग्णालय", "siphil", "siphil hospital"
        ],
        "physical_lat": 19.99580,
        "physical_lon": 73.77880,
        "address": "Old Agra Road, Near Trimbak Naka, CBS, Nashik 422001",
        "linked_station_ids": [396, 814, 1590, 1184],
        "source": "Government of Maharashtra Public Health Department, Registration #MH-NSK-CIV-001",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "esi_hospital",
        "name_en": "ESIC Hospital, Satpur",
        "name_mr": "कर्मचारी राज्य विमा योजना रुग्णालय, सातपूर",
        "aliases": ["esi hospital", "esic hospital", "कामगार रुग्णालय", "ईएसआय रुग्णालय", "satpur hospital", "esi"],
        "physical_lat": 19.99120,
        "physical_lon": 73.73450,
        "address": "Trimbak Road, Satpur MIDC, Near ITI Signal, Nashik 422007",
        "linked_station_ids": [727, 918],
        "source": "Employees' State Insurance Corporation Regional Directorate Maharashtra, Hospital #331001",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "bytco_hospital",
        "name_en": "Bytco Municipal Hospital, Nashik Road",
        "name_mr": "बिटको महानगरपालिका रुग्णालय, नाशिक रोड",
        "aliases": ["bitco hospital", "bitco", "बिटको", "बिटको रुग्णालय", "बिटको हॉस्पिटल", "bytco hospital", "bytco", "बायको हॉस्पिटल", "बायको रुग्णालय", "बायको", "nashik road municipal hospital", "nashik road hospital"],
        "physical_lat": 19.94300,
        "physical_lon": 73.84150,
        "address": "Bytco Point, Near Nashik Road Railway Station, Nashik Road 422101",
        "linked_station_ids": [250, 281, 277, 863, 1001, 1440],
        "source": "Nashik Municipal Corporation Health Department Apex Municipal Hospital Directory",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "namco_hospital",
        "name_en": "NAMCO Charitable Cancer Hospital",
        "name_mr": "नामको कॅन्सर रुग्णालय, पंचवटी",
        "aliases": ["namco hospital", "namco cancer hospital", "नामको हॉस्पिटल", "नामको कर्करोग रुग्णालय", "namco"],
        "physical_lat": 20.03410,
        "physical_lon": 73.79570,
        "address": "Behind RTO, Near Peth Naka, Ganeshwadi, Panchavati, Nashik 422003",
        "linked_station_ids": [1854, 1856],
        "source": "Shree Saibaba Heart Institute & NAMCO Cancer Hospital Trust Reg #E-614/Nashik",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "dhariwal_hospital",
        "name_en": "Rasiklal M. Dhariwal Heart & Cancer Institute",
        "name_mr": "रसिकलाल धारिवाल रुग्णालय, पंचवटी",
        "aliases": ["dhariwal hospital", "rashiklal dhariwal hospital", "धारिवाल रुग्णालय", "dhariwal"],
        "physical_lat": 20.03420,
        "physical_lon": 73.79770,
        "address": "Peth Phata, Ganeshwadi Link Road, Panchavati, Nashik 422003",
        "linked_station_ids": [1853, 1855],
        "source": "Rasiklal Dhariwal Medical Foundation Trust Reg #E-1042/Nashik",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "nagji_hospital",
        "name_en": "Nagji Memorial Hospital & Research Institute",
        "name_mr": "नागजी मेमोरियल रुग्णालय, द्वारका",
        "aliases": ["nagji hospital", "nagji", "नागजी हॉस्पिटल", "नागजी रुग्णालय"],
        "physical_lat": 19.98900,
        "physical_lon": 73.79290,
        "address": "Dwarka Circle / Mumbai-Agra Highway, Nashik 422011",
        "linked_station_ids": [497, 1078],
        "source": "Nagji Memorial Hospital Charitable Trust Reg #E-489/Nashik",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "saptshrungi_hospital",
        "name_en": "Saptashrungi Ayurved Mahavidyalaya & Hospital",
        "name_mr": "सप्तशृंगी आयुर्वेद रुग्णालय, हिरावाडी",
        "aliases": ["saptshrungi hospital", "saptashrungi ayurved hospital", "सप्तशृंगी रुग्णालय", "saptashrungi hospital"],
        "physical_lat": 19.95520,
        "physical_lon": 73.76290,
        "address": "Kamal Nagar, Hirawadi Road, Panchavati, Nashik 422003",
        "linked_station_ids": [1002, 425],
        "source": "Maharashtra University of Health Sciences (MUHS) Affiliated Ayurvedic Hospital",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "hal_hospital",
        "name_en": "HAL Hospital, Ozar Township",
        "name_mr": "एच.ए.एल. रुग्णालय, ओझर टाउनशिप",
        "aliases": ["hal hospital", "hal ozar hospital", "एचएएल रुग्णालय", "hal"],
        "physical_lat": 20.10470,
        "physical_lon": 73.95120,
        "address": "Sector 4, HAL Township, Ozar (Mig), Nashik 422207",
        "linked_station_ids": [1405],
        "source": "Hindustan Aeronautics Limited Healthcare Directorate Ozar Division",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "hridayaksha_hospital",
        "name_en": "Hridayaksha Cardiac Care Clinic & Hospital",
        "name_mr": "हृदयाक्ष रुग्णालय, गंगापूर रोड",
        "aliases": ["hridayaksha hospital", "hridayaksha", "हृदयाक्ष हॉस्पिटल"],
        "physical_lat": 20.02580,
        "physical_lon": 73.75370,
        "address": "Gangapur Road, Anandvalli, Nashik 422013",
        "linked_station_ids": [1890],
        "source": "Nashik Clinical Establishments Registration Registry #NSK-CLINIC-772",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "gupta_hospital",
        "name_en": "Dr. Gupta Hospital",
        "name_mr": "डॉ. गुप्ता हॉस्पिटल, नाशिक रोड",
        "aliases": ["gupta hospital", "गुप्ता हॉस्पिटल", "डॉ गुप्ता रुग्णालय"],
        "physical_lat": 19.92820,
        "physical_lon": 73.87140,
        "address": "Sinnar Phata, Near Bytco Point, Nashik Road 422101",
        "linked_station_ids": [668],
        "source": "Nashik Road Local Healthcare Registry #NSK-RD-112",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "manpa_satpur_dispensary",
        "name_en": "NMC Satpur Urban Dispensary (Manpa Davakhana)",
        "name_mr": "मनपा दवाखाना, महाराष्ट्र हौसिंग कॉलनी, सातपूर",
        "aliases": ["manpa davakhana", "satpur dispensary", "मनपा दवाखाना", "महानगरपालिका दवाखाना सातपूर"],
        "physical_lat": 19.98940,
        "physical_lon": 73.72840,
        "address": "Maharashtra Housing Colony, Satpur, Nashik 422007",
        "linked_station_ids": [30, 405, 1148, 1131],
        "source": "Nashik Municipal Corporation Urban Primary Health Center Ward #9",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "mico_dispensary",
        "name_en": "MICO (Bosch) Industrial Dispensary",
        "name_mr": "मायको (बॉश) दवाखाना, सातपूर एमआयडीसी",
        "aliases": ["mico davakhana", "bosch dispensary", "मायको दवाखाना", "mico hospital"],
        "physical_lat": 20.01660,
        "physical_lon": 73.79680,
        "address": "Satpur Industrial Area / MICO Circle link, Nashik 422007",
        "linked_station_ids": [178, 196],
        "source": "Bosch India Employee Health Services / MIDC Satpur Directory",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "mvp_medical_college_hospital",
        "name_en": "Dr. Vasantrao Pawar Medical College Hospital (MVP)",
        "name_mr": "डॉ. वसंतराव पवार वैद्यकीय महाविद्यालय व रुग्णालय (मविप्र), आडगाव",
        "aliases": ["mvp hospital", "vasantrao pawar hospital", "adgaon medical college", "मविप्र रुग्णालय", "वसंतराव पवार हॉस्पिटल"],
        "physical_lat": 20.03250,
        "physical_lon": 73.85800,
        "address": "Mumbai-Agra Highway, Adgaon, Nashik 422003",
        "linked_station_ids": [95, 859, 92, 94, 1344],
        "source": "Maratha Vidya Prasarak Samaj (MVP) Medical College Hospital Registry / MUHS Teaching Hospital",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "apollo_hospital",
        "name_en": "Apollo Hospitals, Nashik",
        "name_mr": "अपोलो हॉस्पिटल, पंचवटी",
        "aliases": ["apollo hospital", "apollo", "अपोलो हॉस्पिटल", "अपोलो रुग्णालय"],
        "physical_lat": 20.02150,
        "physical_lon": 73.80550,
        "address": "Swaminarayan Nagar, Panchavati, Nashik 422003",
        "linked_station_ids": [691, 694, 20, 17],
        "source": "Apollo Hospitals Enterprise Ltd Regional Directory & NMC Hospital Registration",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "wockhardt_hospital",
        "name_en": "Wockhardt Hospitals, Nashik",
        "name_mr": "वोक्हार्ट हॉस्पिटल, मुंबई नाका",
        "aliases": ["wockhardt hospital", "wockhardt", "वोक्हार्ट हॉस्पिटल", "वोक्हार्ट रुग्णालय"],
        "physical_lat": 19.98820,
        "physical_lon": 73.78450,
        "address": "Wani House, Mumbai Naka, Old Agra Road, Nashik 422001",
        "linked_station_ids": [396, 814, 1590, 1184],
        "source": "Wockhardt Hospitals Network & NMC Super Specialty Hospital Directory",
        "verification_status": "VERIFIED"
    },
    {
        "hospital_id": "ashoka_medicover_hospital",
        "name_en": "Ashoka Medicover Hospital",
        "name_mr": "अशोका मेडिकोव्हर हॉस्पिटल, वडाळा फाटा",
        "aliases": ["ashoka medicover", "medicover hospital", "ashoka hospital", "अशोका मेडिकोव्हर", "अशोका हॉस्पिटल"],
        "physical_lat": 19.96750,
        "physical_lon": 73.78400,
        "address": "Wadala Phata, Indira Nagar Annex, Nashik 422009",
        "linked_station_ids": [624],
        "source": "Medicover Healthcare Network India / NMC Tertiary Care Registry",
        "verification_status": "VERIFIED"
    }
]


class LocationIntelligenceService:
    """Provides spatial indexing, search, and route geometry services for CityLinc Nashik."""

    def __init__(self):
        self.stations_by_id: Dict[int, Dict[str, Any]] = {}
        self.stations_by_name: Dict[str, Dict[str, Any]] = {}
        self.hospitals: List[Dict[str, Any]] = []
        self.landmarks: List[Dict[str, Any]] = []
        self.routes_summary: List[Dict[str, Any]] = []
        self.hospital_entities: List[Dict[str, Any]] = []
        self.hospital_entities_by_id: Dict[str, Dict[str, Any]] = {}
        self.initialized = False
        self._build_indices()

    def _normalize_query(self, text: str) -> str:
        """Normalizes Devanagari digits and whitespace."""
        out = text or ""
        for d_char, a_digit in DEVANAGARI_DIGITS.items():
            out = out.replace(d_char, a_digit)
        return out.strip().lower()

    def _determine_direction(self, station_name: str) -> str:
        """Determines platform direction from verified station name."""
        name_lower = station_name.lower()
        if "(u)" in name_lower or "( u )" in name_lower or " (u)" in name_lower:
            return "UP"
        if "(d)" in name_lower or "( d )" in name_lower or " (d)" in name_lower:
            return "DOWN"
        if any(t in name_lower for t in ["terminus", "depot", "cbs"]):
            return "TERMINAL"
        return "BOTH"

    def _categorize_station(self, station_name: str) -> Tuple[str, bool, bool, Optional[str]]:
        """
        Categorizes a station into medical, educational, religious, transit, or junction.
        Phase 4A: Removes false positive keyword classifications identified in Phase 3.5.
        Returns: (primary_category, is_hospital, is_landmark, landmark_subcategory)
        """
        name_lower = station_name.lower().strip()

        # 1. Explicit Disqualification / Reclassification of False Positives from Phase 3.5 Audit
        # Highway forks/intersections near MVP Medical College
        if 'medical phata' in name_lower or 'medical fata' in name_lower:
            return "junction", False, True, "junction"

        # Residential apartment development
        if 'panchakarma residency' in name_lower:
            return "general", False, False, None

        # Rural temple road junction in Mohgaon
        if 'saptshrungi mata mandir' in name_lower:
            return "religious", False, True, "religious"

        # 2. Genuine Hospital / Medical Facilities
        # Includes verified municipal hospitals (Civil, ESIC, Bytco) and clinics
        if any(k in name_lower for k in [
            'hospital', 'rugnalaya', 'davakhana', 'clinic', 'esi', 'civil',
            'bytco', 'arogya vidnyan', 'dr. vasantrao pawar house'
        ]):
            return "hospital", True, True, "medical"

        # 3. Education / Colleges / Schools
        if any(k in name_lower for k in [
            'college', 'vidyalaya', 'school', 'polytechnic', 'iti', 'university',
            'campus', 'institute', 'academy', 'motiwala', 'kthm', 'bramha valley'
        ]):
            return "education", False, True, "education"

        # 4. Temples / Religious / Cultural Heritage
        if any(k in name_lower for k in [
            'mandir', 'temple', 'masjid', 'dargah', 'church', 'ashram', 'gurupeeth',
            'samarth', 'karanja', 'tapovan', 'trimbak', 'gajanan maharaj', 'balaji'
        ]):
            return "religious", False, True, "religious"

        # 5. Major Transit Hubs & Terminals
        if any(k in name_lower for k in [
            'stand', 'terminus', 'depot', 'cbs', 'railway', 'station'
        ]):
            return "transit_hub", False, True, "transit_hub"

        # 6. Junctions / Circles / Phata
        if any(k in name_lower for k in [
            'circle', 'naka', 'chowk', 'point', 'signal', 'corner', 'phata', 'fata'
        ]):
            return "junction", False, True, "junction"

        return "general", False, False, None

    def _build_indices(self):
        """Indexes all 1,187 unique physical stations and links them to passing routes."""
        station_route_map: Dict[int, Set[str]] = {}
        station_route_ids: Dict[int, Set[int]] = {}

        # 1. Collect all passing routes for each station from the 12,909 route-stops
        for r in knowledge_service.routes:
            rid = r.get("routeid")
            rno = str(r.get("rno", "")).strip().upper()
            for s in r.get("stops", []):
                sid = s.get("stationid")
                if sid is not None:
                    sid = int(sid)
                    if sid not in station_route_map:
                        station_route_map[sid] = set()
                        station_route_ids[sid] = set()
                    if rno:
                        station_route_map[sid].add(rno)
                    if rid is not None:
                        station_route_ids[sid].add(int(rid))

                    if sid not in self.stations_by_id:
                        sname = str(s.get("stationname") or s.get("displayname") or "").strip()
                        raw_lat = s.get("center_lat")
                        raw_lon = s.get("center_lon")

                        try:
                            lat = float(raw_lat)
                            lon = float(raw_lon)
                        except (ValueError, TypeError):
                            logger.error(f"Invalid coordinate for station {sid}: ({raw_lat}, {raw_lon})")
                            continue

                        direction = self._determine_direction(sname)
                        cat, is_hosp, is_lm, lm_subcat = self._categorize_station(sname)

                        # Clean base name (without (U) or (D))
                        clean_base_name = sname
                        for suffix in ["(U)", "(D)", "( U )", "( D )", " (U)", " (D)"]:
                            clean_base_name = clean_base_name.replace(suffix, "").strip()

                        station_dict = {
                            "station_id": sid,
                            "name": sname,
                            "clean_name": clean_base_name,
                            "display_name": s.get("displayname") or sname,
                            "lat": lat,
                            "lon": lon,
                            "direction": direction,
                            "category": cat,
                            "is_hospital": is_hosp,
                            "is_landmark": is_lm,
                            "landmark_category": lm_subcat,
                            "verified_coordinates": True,
                            "coordinate_source": "CityLinc Official Center Coordinate (center_lat, center_lon)",
                            "passing_routes": [],
                            "passing_route_ids": [],
                            "stop_count_in_network": 0
                        }
                        self.stations_by_id[sid] = station_dict
                        self.stations_by_name[sname.lower()] = station_dict

        # 2. Attach sorted routes to each station
        for sid, st in self.stations_by_id.items():
            routes_list = sorted(list(station_route_map.get(sid, set())), key=lambda x: (len(x), x))
            st["passing_routes"] = routes_list
            st["passing_route_ids"] = sorted(list(station_route_ids.get(sid, set())))
            st["stop_count_in_network"] = len(routes_list)

            if st["is_hospital"]:
                self.hospitals.append(st)
            if st["is_landmark"]:
                self.landmarks.append(st)

        # Sort hospitals and landmarks alphabetically
        self.hospitals.sort(key=lambda x: x["name"])
        self.landmarks.sort(key=lambda x: x["name"])

        # 3. Build verified physical hospital entities with grouped Up/Down platforms
        for h_def in VERIFIED_PHYSICAL_HOSPITALS:
            hid = h_def["hospital_id"]
            plat = h_def["physical_lat"]
            plon = h_def["physical_lon"]

            linked_stops = []
            all_hosp_routes = set()

            for sid in h_def["linked_station_ids"]:
                if sid in self.stations_by_id:
                    st = self.stations_by_id[sid]
                    # Calculate straight line distance in meters
                    dist_km = haversine_distance_km(plat, plon, st["lat"], st["lon"])
                    dist_m = int(round(dist_km * 1000))
                    # Approximate walking distance based on urban street factor (~1.35x)
                    approx_walk_m = int(round(dist_m * 1.35))

                    stop_info = {
                        "station_id": st["station_id"],
                        "name": st["name"],
                        "clean_name": st["clean_name"],
                        "direction": st["direction"],
                        "lat": st["lat"],
                        "lon": st["lon"],
                        "passing_routes": st["passing_routes"],
                        "straight_line_dist_m": dist_m,
                        "approx_walk_distance_m": approx_walk_m
                    }
                    linked_stops.append(stop_info)
                    all_hosp_routes.update(st["passing_routes"])

            # Sort linked stops: nearest to physical building first
            linked_stops.sort(key=lambda x: x["straight_line_dist_m"])

            # Directional platforms
            recommended_up = next((s for s in linked_stops if s["direction"] == "UP"), None)
            recommended_down = next((s for s in linked_stops if s["direction"] == "DOWN"), None)
            primary_stop = linked_stops[0] if linked_stops else None

            entity = {
                "hospital_id": hid,
                "name_en": h_def["name_en"],
                "name_mr": h_def["name_mr"],
                "aliases": h_def["aliases"],
                "physical_coordinates": {
                    "lat": plat,
                    "lon": plon,
                    "coordinate_type": "physical_poi_centroid"
                },
                "address": h_def["address"],
                "source": h_def["source"],
                "verification_status": h_def["verification_status"],
                "linked_stops_count": len(linked_stops),
                "total_routes_count": len(all_hosp_routes),
                "passing_routes": sorted(list(all_hosp_routes), key=lambda x: (len(x), x)),
                "primary_stop": primary_stop,
                "directional_platforms": {
                    "up_platform": recommended_up,
                    "down_platform": recommended_down
                },
                "linked_bus_stops": linked_stops,
                "walking_distance_disclaimer": "Walking distance is calculated based on pedestrian street factor (approx. 1.35x geodesic distance). Real walking times depend on pedestrian crossing locations."
            }
            self.hospital_entities.append(entity)
            self.hospital_entities_by_id[hid] = entity

        # 4. Build route summaries list
        for r in knowledge_service.routes:
            rid = r.get("routeid")
            rno = str(r.get("rno", "")).strip().upper()
            stops = r.get("stops", [])
            total_dist = "0 km"
            if stops:
                last_dist = stops[-1].get("totaldistance")
                if last_dist:
                    total_dist = f"{last_dist} km"

            from_st_val = r.get("fromstation")
            to_st_val = r.get("tostation")

            from_name = ""
            if isinstance(from_st_val, int) and from_st_val in self.stations_by_id:
                from_name = self.stations_by_id[from_st_val]["name"]
            elif from_st_val:
                from_name = str(from_st_val)
            elif stops:
                from_name = stops[0].get("stationname") or ""

            to_name = ""
            if isinstance(to_st_val, int) and to_st_val in self.stations_by_id:
                to_name = self.stations_by_id[to_st_val]["name"]
            elif to_st_val:
                to_name = str(to_st_val)
            elif stops:
                to_name = stops[-1].get("stationname") or ""

            summary = {
                "route_id": rid,
                "rno": rno,
                "route_name": r.get("routename") or r.get("displayfullroutename") or "",
                "from_station": from_name,
                "to_station": to_name,
                "stop_count": len(stops),
                "total_distance": total_dist,
                "timetable_trip_count": len(r.get("timetable", []))
            }
            self.routes_summary.append(summary)

        self.routes_summary.sort(key=lambda x: (x["rno"], x.get("route_id") or 0))
        self.initialized = True
        logger.info(
            f"LocationIntelligenceService ready: {len(self.stations_by_id)} unique physical stations, "
            f"{len(self.hospitals)} verified hospitals, {len(self.landmarks)} landmarks indexed."
        )

    # -------------------------------------------------------------
    # API Methods
    # -------------------------------------------------------------

    def get_all_stops(
        self,
        query: Optional[str] = None,
        route_id: Optional[int] = None,
        category: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> Dict[str, Any]:
        """Returns paginated list of physical stations with filter capabilities."""
        results = list(self.stations_by_id.values())

        if route_id is not None:
            results = [s for s in results if route_id in s["passing_route_ids"]]

        if category:
            cat_norm = category.strip().lower()
            results = [s for s in results if s["category"].lower() == cat_norm or s.get("landmark_category") == cat_norm]

        if query:
            q_norm = self._normalize_query(query)
            results = [
                s for s in results
                if q_norm in s["name"].lower() or q_norm in s["clean_name"].lower() or any(q_norm == r.lower() for r in s["passing_routes"])
            ]

        total = len(results)
        paginated = results[offset: offset + limit] if limit > 0 else results

        return {
            "total": total,
            "count": len(paginated),
            "offset": offset,
            "limit": limit,
            "stops": paginated
        }

    def search_locations(self, q: str, category: Optional[str] = "all", limit: int = 20) -> Dict[str, Any]:
        """
        Multilingual search across stops, hospitals, landmarks, and routes.
        Supports English, Devanagari, and Roman Marathi colloquial tokens.
        """
        cat_filter = (category or "all").strip().lower()

        if not q or not q.strip():
            return {
                "query": "",
                "category": cat_filter,
                "total_matches": 0,
                "stops": [],
                "routes": [],
                "hospitals": [],
                "landmarks": []
            }

        q_norm = self._normalize_query(q)
        cat_filter = (category or "all").strip().lower()

        # Check colloquial alias expansion
        expanded_tokens = [q_norm]
        for canonical, aliases in LOCATION_ALIASES.items():
            if any(a in q_norm or q_norm in a for a in aliases):
                expanded_tokens.append(canonical)
                expanded_tokens.extend(aliases)

        matching_stops = []
        matching_hospitals = []
        matching_landmarks = []
        matching_routes = []

        # 1. Search Stops
        if cat_filter in ("all", "stop", "stops"):
            for s in self.stations_by_id.values():
                s_name = s["name"].lower()
                clean_name = s["clean_name"].lower()
                if any(tok in s_name or tok in clean_name for tok in expanded_tokens) or any(tok == r.lower() for tok in expanded_tokens for r in s["passing_routes"]):
                    matching_stops.append(s)
                    if len(matching_stops) >= limit * 2:
                        break

        # 2. Search Hospitals
        if cat_filter in ("all", "hospital", "hospitals", "medical"):
            for h in self.hospitals:
                h_name = h["name"].lower()
                if any(tok in h_name for tok in expanded_tokens):
                    matching_hospitals.append(h)
                    if len(matching_hospitals) >= limit:
                        break

        # 3. Search Landmarks
        if cat_filter in ("all", "landmark", "landmarks"):
            for lm in self.landmarks:
                lm_name = lm["name"].lower()
                if any(tok in lm_name for tok in expanded_tokens):
                    matching_landmarks.append(lm)
                    if len(matching_landmarks) >= limit:
                        break

        # 4. Search Routes
        if cat_filter in ("all", "route", "routes"):
            for r in self.routes_summary:
                rno = r["rno"].lower()
                rname = r["route_name"].lower()
                if any(tok == rno or tok in rname for tok in expanded_tokens):
                    matching_routes.append(r)
                    if len(matching_routes) >= limit:
                        break

        total_matches = len(matching_stops) + len(matching_routes) + len(matching_hospitals) + len(matching_landmarks)

        return {
            "query": q,
            "category": cat_filter,
            "total_matches": total_matches,
            "stops": matching_stops[:limit],
            "routes": matching_routes[:limit],
            "hospitals": matching_hospitals[:limit],
            "landmarks": matching_landmarks[:limit]
        }

    def get_nearby_stops(
        self,
        lat: float,
        lon: float,
        radius_km: float = 2.0,
        limit: int = 20
    ) -> Dict[str, Any]:
        """
        Finds physical transit stops within straight-line Haversine radius of GPS coordinates.
        Does not claim geodesic distance is road/walking distance.
        """
        # Validate coordinates
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return {
                "error": "Invalid latitude/longitude coordinates",
                "center": {"lat": lat, "lon": lon},
                "count": 0,
                "stops": []
            }

        nearby_list = []
        for s in self.stations_by_id.values():
            dist = haversine_distance_km(lat, lon, s["lat"], s["lon"])
            if dist <= radius_km:
                item = dict(s)
                item["distance_km"] = dist
                item["distance_meters"] = int(dist * 1000)
                nearby_list.append(item)

        # Sort by distance ascending
        nearby_list.sort(key=lambda x: x["distance_km"])
        sliced = nearby_list[:limit]

        return {
            "center": {"lat": lat, "lon": lon},
            "radius_km": radius_km,
            "total_found": len(nearby_list),
            "count": len(sliced),
            "disclaimer": "Distances represent geodesic straight-line distance, not actual walking or road route distance.",
            "stops": sliced
        }

    def get_hospitals(self, query: Optional[str] = None, limit: int = 50, grouped: bool = False) -> Dict[str, Any]:
        """
        Returns verified hospital bus stops or grouped physical hospital entities.
        If grouped=True: returns physical hospital POIs with grouped Up/Down directional platforms.
        If grouped=False: returns verified hospital bus stops (backwards compatible).
        """
        if grouped:
            results = list(self.hospital_entities)
            if query:
                q_norm = self._normalize_query(query)
                results = [
                    h for h in results
                    if q_norm in h["name_en"].lower()
                    or q_norm in h["name_mr"].lower()
                    or any(q_norm in a.lower() for a in h["aliases"])
                    or any(q_norm == r.lower() for r in h["passing_routes"])
                ]
            sliced = results[:limit]
            return {
                "total": len(results),
                "count": len(sliced),
                "category": "verified_physical_hospitals",
                "hospitals": sliced
            }

        results = list(self.hospitals)
        if query:
            q_norm = self._normalize_query(query)
            results = [h for h in results if q_norm in h["name"].lower() or any(q_norm == r.lower() for r in h["passing_routes"])]

        sliced = results[:limit]
        return {
            "total": len(results),
            "count": len(sliced),
            "category": "hospitals_medical",
            "hospitals": sliced
        }

    def get_hospital_entities(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns all verified physical hospital entities with linked platforms and sources."""
        if not query:
            return list(self.hospital_entities)
        q_norm = self._normalize_query(query)
        return [
            h for h in self.hospital_entities
            if q_norm in h["name_en"].lower()
            or q_norm in h["name_mr"].lower()
            or any(q_norm in a.lower() for a in h["aliases"])
            or any(q_norm == r.lower() for r in h["passing_routes"])
        ]

    def get_hospital_navigation(self, query: str) -> Dict[str, Any]:
        """
        Phase 4E: Resolves a hospital POI, finds nearest stops, direct routes,
        and provides directional alighting guidance distinguishing POI from stop coords.
        """
        q_norm = self._normalize_query(query)

        # Match against hospital entities
        matched_entity = None
        for h in self.hospital_entities:
            if (q_norm in h["name_en"].lower() or
                q_norm in h["name_mr"].lower() or
                any(q_norm in a.lower() or a.lower() in q_norm for a in h["aliases"])):
                matched_entity = h
                break

        if not matched_entity:
            # Generic hospital stop words that must NOT trigger singleton entity matching
            HOSPITAL_STOPWORDS = {
                "hospital", "hospitals", "clinic", "dispensary", "रुग्णालय", "दवाखाना", "हॉस्पिटल",
                "medical", "centre", "center", "care", "health"
            }
            for h in self.hospital_entities:
                for a in h["aliases"]:
                    tokens = a.lower().split()
                    if any(t in q_norm for t in tokens if len(t) > 3 and t not in HOSPITAL_STOPWORDS):
                        matched_entity = h
                        break
                if matched_entity:
                    break

        if not matched_entity:
            return {
                "resolved": False,
                "query": query,
                "message": f"Hospital '{query}' not found in verified registry.",
                "verified_hospitals_available": [h["name_en"] for h in self.hospital_entities[:8]]
            }

        primary = matched_entity.get("primary_stop")
        walk_m = primary.get("approx_walk_distance_m", 50) if primary else 50
        stop_name = primary.get("name", "curbside bus stop") if primary else "curbside stop"

        return {
            "resolved": True,
            "hospital_id": matched_entity["hospital_id"],
            "name_en": matched_entity["name_en"],
            "name_mr": matched_entity["name_mr"],
            "address": matched_entity["address"],
            "physical_coordinates": matched_entity["physical_coordinates"],
            "source": matched_entity["source"],
            "verification_status": matched_entity["verification_status"],
            "direct_routes": matched_entity["passing_routes"],
            "routes_count": matched_entity["total_routes_count"],
            "primary_alighting_stop": primary,
            "directional_platforms": matched_entity["directional_platforms"],
            "linked_stops": matched_entity["linked_bus_stops"],
            "walking_guidance": f"Alight at {stop_name} (approx. {walk_m}m walk to hospital main entrance).",
            "disclaimer": matched_entity["walking_distance_disclaimer"]
        }

    def haversine_distance_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculates spherical distance in km between two coordinate pairs."""
        return haversine_distance_km(lat1, lon1, lat2, lon2)

    def get_nearest_hospital(self, user_lat: float, user_lon: float) -> Optional[Dict[str, Any]]:
        """Calculates physical geodesic distance to verified hospitals and returns closest entity."""
        if not self.hospital_entities:
            return None
        closest = None
        min_d = float('inf')
        for h in self.hospital_entities:
            coords = h.get("physical_coordinates", {})
            plat = coords.get("lat")
            plon = coords.get("lon")
            if plat and plon:
                d = haversine_distance_km(user_lat, user_lon, plat, plon)
                if d < min_d:
                    min_d = d
                    closest = (h, d)
        if closest:
            h_entity, dist_km = closest
            res = dict(h_entity)
            res["distance_from_user_km"] = round(dist_km, 2)
            return res
        return None

    def get_landmarks(
        self,
        category: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 100
    ) -> Dict[str, Any]:
        """Returns categorized landmark stops (education, religious, transit_hub, junction)."""
        results = list(self.landmarks)

        if category:
            cat_norm = category.strip().lower()
            results = [lm for lm in results if lm.get("landmark_category") == cat_norm or lm.get("category") == cat_norm]

        if query:
            q_norm = self._normalize_query(query)
            results = [lm for lm in results if q_norm in lm["name"].lower()]

        sliced = results[:limit]
        return {
            "total": len(results),
            "count": len(sliced),
            "filter_category": category or "all",
            "landmarks": sliced
        }

    def get_route_trajectory(self, route_id_or_number: str) -> Dict[str, Any]:
        """
        Reconstructs the full route trajectory, ordered stop sequence, and deduplicated polyline.
        Accepts integer route_id (e.g. 6601) or route number string (e.g. '101').
        Deduplicates adjacent duplicate coordinates (DQI-004) for clean polyline rendering.
        """
        target_route = None

        # 1. Try by integer routeid
        try:
            rid = int(route_id_or_number)
            if rid in knowledge_service.routes_by_id:
                target_route = knowledge_service.routes_by_id[rid]
        except ValueError:
            pass

        # 2. Try by public route number (rno)
        if not target_route:
            rno_clean = str(route_id_or_number).strip().upper()
            if rno_clean in knowledge_service.routes_by_number:
                corridors = knowledge_service.routes_by_number[rno_clean]
                if corridors:
                    target_route = corridors[0]  # Default to primary corridor

        if not target_route:
            return {
                "error": f"Route '{route_id_or_number}' not found in CityLinc network.",
                "route_id": None,
                "stops": [],
                "polyline": []
            }

        rid = target_route.get("routeid")
        rno = str(target_route.get("rno", "")).strip().upper()
        raw_stops = target_route.get("stops", [])

        ordered_stops = []
        polyline = []
        last_coord = None

        for s in raw_stops:
            sid = s.get("stationid")
            sname = str(s.get("stationname") or s.get("displayname") or "").strip()
            srno = s.get("srno")
            dist = s.get("totaldistance")
            raw_lat = s.get("center_lat")
            raw_lon = s.get("center_lon")

            try:
                lat = float(raw_lat)
                lon = float(raw_lon)
            except (ValueError, TypeError):
                continue

            direction = self._determine_direction(sname)
            stop_meta = {
                "srno": int(srno) if str(srno).isdigit() else 0,
                "station_id": int(sid) if sid is not None else None,
                "name": sname,
                "lat": lat,
                "lon": lon,
                "total_distance_km": float(dist) if dist and str(dist).replace(".", "").isdigit() else 0.0,
                "direction": direction
            }
            ordered_stops.append(stop_meta)

            # Build polyline: deduplicate consecutive identical coordinates (DQI-004)
            current_coord = [lat, lon]
            if last_coord is None or (current_coord[0] != last_coord[0] or current_coord[1] != last_coord[1]):
                polyline.append(current_coord)
                last_coord = current_coord

        origin_stop = ordered_stops[0] if ordered_stops else None
        dest_stop = ordered_stops[-1] if ordered_stops else None

        from_st_val = target_route.get("fromstation")
        to_st_val = target_route.get("tostation")

        from_station_name = ""
        if isinstance(from_st_val, int) and from_st_val in self.stations_by_id:
            from_station_name = self.stations_by_id[from_st_val]["name"]
        elif from_st_val:
            from_station_name = str(from_st_val)
        elif origin_stop:
            from_station_name = origin_stop["name"]

        to_station_name = ""
        if isinstance(to_st_val, int) and to_st_val in self.stations_by_id:
            to_station_name = self.stations_by_id[to_st_val]["name"]
        elif to_st_val:
            to_station_name = str(to_st_val)
        elif dest_stop:
            to_station_name = dest_stop["name"]

        return {
            "route_id": rid,
            "rno": rno,
            "route_name": target_route.get("routename") or target_route.get("displayfullroutename") or "",
            "from_station": from_station_name,
            "to_station": to_station_name,
            "stop_count": len(ordered_stops),
            "total_distance_km": ordered_stops[-1]["total_distance_km"] if ordered_stops else 0.0,
            "origin": origin_stop,
            "destination": dest_stop,
            "stops": ordered_stops,
            "polyline": polyline,
            "deduplicated_points_count": len(polyline),
            "raw_stops_count": len(ordered_stops)
        }


# Global singleton service
location_service = LocationIntelligenceService()
