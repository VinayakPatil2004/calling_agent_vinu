"""
CityLinc Origin-Destination Route Engine.
Provides deterministic, high-accuracy transit pathfinding across the audited CityLinc Nashik bus network:
- 315 route records
- 12,909 stop sequence instances
- 1,187 unique physical stations

Features:
- Trilingual place resolution (Marathi, Hindi, English, Devanagari and Roman phonetic scripts)
- Automatic station and directional platform candidate expansion
- Strict topological directionality (ensures boarding stop precedes alighting stop on route)
- Intermediate stop sequence extraction with distances
- Transfer route calculation (1-transfer) via major Nashik transit hubs (CBS, Shalimar, Nimani, Dwarka)
- Route ranking prioritizing scheduled trips, fewest intermediate stops, and direct connectivity
"""
import re
import logging
from typing import Dict, List, Optional, Any, Set, Tuple
from pydantic import BaseModel, Field

from backend.app.services.citylinc_knowledge import knowledge_service, LOCATION_ALIASES, DEVANAGARI_DIGITS
from backend.app.services.location_service import location_service
from backend.app.services.transit_graph import transit_graph, format_minutes_to_time, parse_time_to_minutes
from backend.app.services.fare_service import fare_service

logger = logging.getLogger("citylinc.route_engine")


# Common suffixes in Marathi and Hindi to strip from place tokens
DEV_AFFIXES = [
    "वरून", "पासून", "पासुन", "हून", "कडून", "कडुन", "ला", "कडे", "पर्यंत", "स", "ना", "चे", "च्या",
    "से", "को", "तक", "के लिए"
]

# Generic transit tokens that must NOT trigger singleton fallback station matching
GENERIC_TRANSIT_TOKENS = {
    "circle", "stand", "stop", "phata", "fata", "road", "corner", "chowk", "chauk",
    "nagar", "depot", "station", "bridge", "point", "gate",
    "सर्कल", "रोड", "स्टॉप", "थांबा", "फाटा", "नाका", "चौक", "नगर", "डेपो", "स्टेशन", "पॉइंट"
}

# Major Transit Interchanges in Nashik for 1-Transfer routing
MAJOR_INTERCHANGES = [
    {
        "hub_name": "CBS (Central Bus Stand)",
        "hub_mr": "सीबीएस (मध्यवर्ती बस स्थानक)",
        "hub_tokens": ["cbs", "सीबीएस", "central bus stand", "मध्यवर्ती"],
        "station_ids": [975, 317, 1590, 1184, 313, 1017]
    },
    {
        "hub_name": "Shalimar",
        "hub_mr": "शालिमार चौक",
        "hub_tokens": ["shalimar", "शालिमार", "शालीमार"],
        "station_ids": [1406, 1407, 706, 532, 1099, 1051, 898, 384]
    },
    {
        "hub_name": "Nimani Bus Stand",
        "hub_mr": "निमाणी बस स्थानक",
        "hub_tokens": ["nimani", "निमाणी"],
        "station_ids": [691, 694, 704, 707, 956, 1194]
    },
    {
        "hub_name": "Dwarka Circle",
        "hub_mr": "द्वारका चौक",
        "hub_tokens": ["dwarka", "द्वारका"],
        "station_ids": [497, 1078, 1049, 1050]
    },
    {
        "hub_name": "Panchavati Karanja",
        "hub_mr": "पंचवटी कारंजा",
        "hub_tokens": ["panchavati karanja", "पंचवटी कारंजा"],
        "station_ids": [707, 704, 691, 694]
    },
    {
        "hub_name": "Satpur Bus Stand",
        "hub_mr": "सातपूर बस स्थानक",
        "hub_tokens": ["satpur bus stand", "सातपूर"],
        "station_ids": [410, 929, 753, 916]
    },
    {
        "hub_name": "Nashik Road Depot",
        "hub_mr": "नाशिक रोड डेपो",
        "hub_tokens": ["nashik road depot", "नाशिक रोड"],
        "station_ids": [1440, 250, 281, 277, 863, 1001]
    }
]


def parse_natural_departure_time(text: str) -> Optional[int]:
    """
    Extracts departure time from natural language input (Marathi, Hindi, English).
    Returns minutes from midnight (0 to 1439) or None if no time was specified.
    """
    if not text:
        return None
    t = text.lower()
    for d_char, a_digit in DEVANAGARI_DIGITS.items():
        t = t.replace(d_char, a_digit)

    is_pm = any(p in t for p in ("दुपारी", "संध्याकाळी", "रात्री", "दोपहर", "शाम", "रात", "pm", "p.m."))
    is_am = any(p in t for p in ("सकाळी", "सुबह", "am", "a.m."))

    # Pattern A: H:MM (e.g. 08:30, 22:15, 6:30)
    m_std = re.search(r'(\d{1,2}):(\d{2})', t)
    if m_std:
        h = int(m_std.group(1))
        m = int(m_std.group(2))
        if is_pm and h < 12:
            h += 12
        elif is_am and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m

    # Pattern B: H वाजता / H बजे / H am/pm
    m_hr = re.search(r'(\d{1,2})\s*(?:वाजता|बजे|am|pm|o\x27clock)', t)
    if not m_hr:
        m_hr = re.search(r'(?:at|around)\s*(\d{1,2})', t)
    if m_hr:
        h = int(m_hr.group(1))
        if is_pm and h < 12:
            h += 12
        elif is_am and h == 12:
            h = 0
        if 0 <= h <= 23:
            return h * 60

    return None


class StopInfo(BaseModel):
    station_id: int
    name: str
    display_name: str
    sequence: int
    distance_from_origin_km: float
    lat: float
    lon: float
    direction: str


class DirectRouteOption(BaseModel):
    route_id: int
    rno: str
    route_name: str
    route_type: str
    boarding_stop: StopInfo
    alighting_stop: StopInfo
    intermediate_stops_count: int
    intermediate_stops: List[StopInfo]
    distance_km: float
    timetable_trips_count: int
    scheduled_departures: List[str]
    polyline: List[List[float]]
    departure_time: Optional[str] = None
    arrival_time: Optional[str] = None
    duration_mins: Optional[int] = None


class TransferLeg(BaseModel):
    leg_number: int
    route_id: int
    rno: str
    route_name: str
    boarding_stop: StopInfo
    alighting_stop: StopInfo
    stops_count: int
    distance_km: float
    timetable_trips_count: int
    scheduled_departures: List[str]
    departure_time: Optional[str] = None
    arrival_time: Optional[str] = None
    duration_mins: Optional[int] = None


class TransferOption(BaseModel):
    interchange_hub: str
    interchange_hub_mr: str
    transfer_stop_name: str
    leg1: TransferLeg
    leg2: TransferLeg
    total_distance_km: float
    total_stops_count: int
    is_timetable_feasible: bool = True
    departure_time: Optional[str] = None
    arrival_time: Optional[str] = None
    total_duration_mins: Optional[int] = None
    wait_time_mins: Optional[int] = None
    walk_time_mins: Optional[int] = None


class JourneyLeg(BaseModel):
    leg_number: int
    route_id: int
    rno: str
    route_name: str
    route_type: str
    boarding_stop: StopInfo
    alighting_stop: StopInfo
    intermediate_stops_count: int
    intermediate_stops: List[StopInfo]
    distance_km: float
    trip_id: Optional[int] = None
    vehicle_no: Optional[str] = None
    departure_time: Optional[str] = None
    departure_mins: Optional[int] = None
    arrival_time: Optional[str] = None
    arrival_mins: Optional[int] = None
    duration_mins: int
    timetable_trips_count: int
    scheduled_departures: List[str]
    polyline: List[List[float]] = []


class TransferPoint(BaseModel):
    from_leg_number: int
    to_leg_number: int
    interchange_hub: str
    interchange_hub_mr: str
    transfer_from_stop: StopInfo
    transfer_to_stop: StopInfo
    walk_distance_m: float
    walk_time_mins: int
    wait_time_mins: int
    description: str


class PlannedJourney(BaseModel):
    journey_id: str
    transfers_count: int
    legs_count: int
    departure_time: str
    arrival_time: str
    total_duration_mins: int
    total_distance_km: float
    total_stops_count: int
    total_fare_inr: int
    legs: List[JourneyLeg]
    transfers: List[TransferPoint] = []
    is_timetable_feasible: bool
    summary_text_mr: str
    summary_text_hi: str
    summary_text_en: str


class ResolvedPlace(BaseModel):
    query_text: str
    canonical_name: str
    display_name: str
    matched_station_ids: List[int]
    sample_stops: List[str]
    is_ambiguous: bool


class JourneySearchResult(BaseModel):
    success: bool
    origin: ResolvedPlace
    destination: ResolvedPlace
    direct_routes_count: int
    direct_routes: List[DirectRouteOption]
    transfer_routes_count: int
    transfer_routes: List[TransferOption]
    multi_hop_journeys: List[PlannedJourney] = []
    fare_estimate: Optional[Dict[str, Any]] = None
    clarification_question: Optional[str] = None
    message: str
    departure_time_used: Optional[str] = None


class TransitRouteEngine:
    """Deterministic Origin-Destination Pathfinding and Transit Journey Engine for CityLinc Nashik."""

    def __init__(self):
        self._build_stop_indices()

    def _build_stop_indices(self):
        """Builds fast inverted indices of station tokens to station IDs and routes."""
        self.station_token_map: Dict[str, Set[int]] = {}
        for sid, st in location_service.stations_by_id.items():
            sname = st["name"].lower()
            clean_name = st["clean_name"].lower()
            # Tokenize words
            tokens = set(re.findall(r'[a-zA-Z0-9\u0900-\u097F]+', sname + " " + clean_name))
            for tok in tokens:
                if len(tok) >= 3:
                    if tok not in self.station_token_map:
                        self.station_token_map[tok] = set()
                    self.station_token_map[tok].add(sid)

    def normalize_text(self, text: str) -> str:
        """Converts Devanagari numerals and trims affixes."""
        out = (text or "").strip()
        for d_char, a_digit in DEVANAGARI_DIGITS.items():
            out = out.replace(d_char, a_digit)
        return out.lower().strip()

    def clean_place_token(self, place_text: str) -> str:
        """Strips Marathi/Hindi grammatical postpositions and whitespace."""
        p = self.normalize_text(place_text)
        # Strip common English prefixes
        for pfx in ["from ", "to ", "at ", "near ", "for "]:
            if p.startswith(pfx):
                p = p[len(pfx):].strip()
        # Strip Devanagari affixes
        for affix in DEV_AFFIXES:
            if p.endswith(affix) and len(p) > len(affix) + 2:
                p = p[:-len(affix)].strip()
                break
        return p.strip()

    def resolve_place(self, place_query: str) -> ResolvedPlace:
        """
        Resolves colloquial place names in Marathi, Hindi, or English to verified station IDs.
        Handles aliases (e.g., 'सीबीएस', 'नासिक रोड', 'गंगापूर', 'तपोवन').
        Prevents generic false matches (such as matching 'police station' when user says 'station').
        """
        clean_q = self.clean_place_token(place_query)
        matched_sids: Set[int] = set()
        canonical_name = clean_q
        display_name = clean_q.title()

        # 1. Check Canonical LOCATION_ALIASES
        # Pass 1: Exact match
        matched_alias_canonical = None
        for canonical, aliases in LOCATION_ALIASES.items():
            for a in aliases:
                if clean_q == a:
                    matched_alias_canonical = canonical
                    break
            if matched_alias_canonical:
                break

        # Pass 2: Substring containment (if no exact match and query is not a lone generic token)
        if not matched_alias_canonical and clean_q.lower() not in GENERIC_TRANSIT_TOKENS:
            for canonical, aliases in LOCATION_ALIASES.items():
                for a in aliases:
                    if a == "station" and clean_q in ("station", "रेल्वे स्टेशन"):
                        matched_alias_canonical = "nashik road"
                        break
                    if clean_q in a or a in clean_q:
                        if len(clean_q) >= 3:
                            matched_alias_canonical = canonical
                            break
                if matched_alias_canonical:
                    break

        if matched_alias_canonical:
            # Special verified mapping for colloquial landmarks without direct eponym stops
            if matched_alias_canonical == "kbt circle" or clean_q in ("kbt circle", "kbt", "k b t circle", "k.b.t. circle", "केबीटी सर्कल", "केबीटी"):
                matched_sids = {122, 1108, 582, 1109}
                canonical_name = "kbt circle"
                display_name = "KBT Circle (Boarding: Thatte Nagar / Vidya Vikas Circle)"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Thatte Nagar (D)", "Thatte Nagar (U)", "Vidya Vikas Circle (D)", "Vidya Vikas Circle (U)"],
                    is_ambiguous=False
                )

            # Special verified mapping for Nimani Bus Stand (Panchavati Depot / Karanja terminus)
            if matched_alias_canonical == "nimani" or clean_q in ("nimani", "निमाणी", "nimani bus stand", "निमाणी बस स्थानक"):
                matched_sids = {691, 694, 704, 707, 956, 1194}
                canonical_name = "nimani"
                display_name = "Nimani Bus Stand (Panchavati Depot / Karanja)"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Panchavati Depot Corner (U)", "Panchavati Depot Corner (D)", "Panchavati Karanja 1 (U)", "Panchavati Karanja (D)"],
                    is_ambiguous=False
                )

            # Special verified mapping for Panchavati (Panchavati Karanja / Depot Corner terminus)
            if matched_alias_canonical == "panchavati" or clean_q in (
                "panchavati", "पंचवटी", "पंचवडी", "panchavadi", "पंचवडीला", "पंचवटीला",
                "पंचवटी कारंजा", "panchavati karanja", "पंचवडी कारंजा"
            ):
                matched_sids = {691, 694, 704, 707, 956, 1194}
                canonical_name = "panchavati"
                display_name = "Panchavati"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Panchavati Karanja (D)", "Panchavati Karanja 1 (U)", "Panchavati Depot Corner (U)", "Panchavati Depot Corner (D)"],
                    is_ambiguous=False
                )

            # Special verified mapping for Nashik Road (Depot / Railway Station cluster)
            if matched_alias_canonical == "nashik road" or clean_q in (
                "nashik road", "नाशिक रोड", "नासिक रोड", "हाशिकरोड", "हाशिक रोड", "हाशिकरोडला",
                "नाशिकरोड", "nashikroad", "railway station", "रेल्वे स्टेशन", "nashik road railway station"
            ):
                matched_sids = {529, 530, 787, 1440, 1683}
                canonical_name = "nashik road"
                display_name = "Nashik Road"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Nashik Road Depot", "Datta Mandir (Nashik Road) (D)", "Datta Mandir (Nashik Road) (U)", "Nashik Road Railway Station"],
                    is_ambiguous=False
                )

            # Special verified mapping for Civil Hospital
            if matched_alias_canonical == "civil hospital" or clean_q in (
                "civil hospital", "सिव्हिल हॉस्पिटल", "सिव्हिल", "civil", "सिविल हॉस्पिटल",
                "सिविल", "सिविल अस्पताल", "सिव्हिल रुग्णालय", "सिविल रुग्णालय",
                "सिफिल", "सिफिल हॉस्पिटल", "सिफिल अस्पताल", "सिफिल रुग्णालय",
                "siphil", "siphil hospital"
            ):
                matched_sids = {396, 814, 1184, 1590}
                canonical_name = "civil hospital"
                display_name = "Civil Hospital"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["New CBS/Civil Hospital(D)", "Civil Hospital (D)", "Civil Hospital (U)", "New CBS/Civil Hospital(U)"],
                    is_ambiguous=False
                )

            # Special verified mapping for Thakkar Bazar / CBS
            if matched_alias_canonical == "thakkar bazar" or clean_q in (
                "thakkar bazar", "thakkar bazaar", "thakkar bazzar", "thakar bazar",
                "ठक्कर बाजार", "ठक्कर बाझार", "ठाकर बाजार",
                "ठक्कर बाजार बस स्थानक", "ठक्कर बाजार बस स्टँड", "thakkar bazar bus stand",
                "ठक्कर बाजारला", "ठक्कर बाजारातून"
            ):
                matched_sids = {82, 313, 315, 317, 955, 975, 1017, 1184, 1590}
                canonical_name = "cbs"
                display_name = "Thakkar Bazar / CBS"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["CBS 1 (U)", "CBS 2 (U)", "CBS 3 (U)", "CBS/Mela Stand (U)", "New CBS/Civil Hospital(U)"],
                    is_ambiguous=False
                )

            # Special verified mapping for Anandavalli
            if matched_alias_canonical == "anandavalli" or clean_q in ("anandavalli", "aanandavalli", "आनंदवल्ली", "आनंदवली"):
                matched_sids = {427, 1105}
                canonical_name = "anandavalli"
                display_name = "Anandavalli"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Aanandavalli (D)", "Aanandavalli (U)"],
                    is_ambiguous=False
                )

            # Special verified mapping for City Centre Mall (CCM)
            if matched_alias_canonical == "ccm" or clean_q in ("ccm", "city centre mall", "सिटी सेंटर मॉल", "सीसीएम", "city center mall"):
                matched_sids = {392, 448, 449}
                canonical_name = "ccm"
                display_name = "City Centre Mall (CCM / Sambhaji Chowk)"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["City Centre Mall", "Chhatrapati Sambhaji Maharaj Chowk (U)", "Chhatrapati Sambhaji Maharaj Chowk (D)"],
                    is_ambiguous=False
                )

            # Special verified mapping for ABT / ABB Circle
            if matched_alias_canonical == "abt circle" or clean_q in ("abt circle", "abt", "एबीटी सर्कल", "एबीटी", "abb circle", "abb", "एबीबी सर्कल", "एबीबी"):
                matched_sids = {195, 192, 676, 1008}
                canonical_name = "abt circle"
                display_name = "ABT Circle (Boarding: Mahatmanagar / Parijat Nagar)"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Mahatmanagar (U)", "Mahatmanagar (D)", "Parijat Nagar (U)", "Parijat Nagar (D)"],
                    is_ambiguous=False
                )

            # Special verified mapping for Dwarka Circle
            if matched_alias_canonical == "dwarka" or clean_q in ("dwarka", "dwarka circle", "द्वारका", "द्वारका सर्कल"):
                matched_sids = {497, 1078, 1287}
                canonical_name = "dwarka"
                display_name = "Dwarka Circle (Boarding: Kathe Galli / Nagji Hospital)"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Kathe Galli Signal", "Nagji Hospital (U)", "Nagji Hospital (D)"],
                    is_ambiguous=False
                )

            # Special verified mapping for Datta Mandir (Nashik Road primary junction)
            if matched_alias_canonical == "datta mandir" or clean_q in ("datta mandir", "datt mandir", "दत्त मंदिर", "दत्त मंदीर", "दत्तमंदिर", "दत्ता मंदिर", "दत्ता मंदीर", "दत्तामंदिर", "datta mandir nashik road", "datt mandir nashik road", "दत्त मंदिर नाशिक रोड", "दत्ता मंदिर नाशिक रोड"):
                matched_sids = {529, 530}
                canonical_name = "datta mandir"
                display_name = "Datta Mandir (Nashik Road)"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Datta Mandir (Nashik Road) (D)", "Datta Mandir (Nashik Road) (U)"],
                    is_ambiguous=False
                )

            # Special verified mapping for Mumbai Naka (Mahamarg Bus Stand)
            if matched_alias_canonical == "mumbai naka" or clean_q in ("mumbai naka", "मुंबई नाका", "mumbainaka", "मुंबई नाक्याला", "मुंबई नाक्या", "मुंबई नाका बस स्टॉप", "mahamarg", "महामार्ग"):
                matched_sids = {202, 887, 950, 951}
                canonical_name = "mumbai naka"
                display_name = "Mumbai Naka (Mahamarg Bus Stand)"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Mahamarg (D)", "Mahamarg (U)", "Mahamarg 1 (U)", "Mahamarg 2 (D)"],
                    is_ambiguous=False
                )

            # Special verified mapping for Shalimar Chowk
            if matched_alias_canonical == "shalimar" or clean_q in ("shalimar", "शालिमार", "शालीमार", "shalimar chowk", "शालिमार चौक", "शालीमार चौक"):
                matched_sids = {384, 532, 706, 898, 1051, 1099, 1406, 1407}
                canonical_name = "shalimar"
                display_name = "Shalimar Chowk"
                return ResolvedPlace(
                    query_text=place_query,
                    canonical_name=canonical_name,
                    display_name=display_name,
                    matched_station_ids=sorted(list(matched_sids)),
                    sample_stops=["Shalimar (D)", "Shalimar (U)", "Shalimar 1 (U)"],
                    is_ambiguous=False
                )

            canonical_name = matched_alias_canonical
            display_name = matched_alias_canonical.title()
            aliases = LOCATION_ALIASES[matched_alias_canonical]

            exact_phrase_sids: Set[int] = set()
            secondary_sids: Set[int] = set()

            for sid, st in location_service.stations_by_id.items():
                sname = st["name"].lower()
                # Special disambiguation: If user asks for Nashik Road / Railway Station, exclude police stations!
                if matched_alias_canonical == "nashik road" and "police station" in sname:
                    continue

                # Strict prioritization: If user explicitly mentions the canonical or alias token in station name
                if matched_alias_canonical in sname:
                    exact_phrase_sids.add(sid)
                else:
                    for a in aliases:
                        if a == "station":
                            continue  # avoid bare station false positive
                        if a in sname:
                            secondary_sids.add(sid)
                            break

            matched_sids = exact_phrase_sids if exact_phrase_sids else secondary_sids

        # 2. Direct Station Name Match
        if not matched_sids:
            # Exclude lone generic transit tokens from substring match (e.g. 'circle', 'stand')
            if clean_q not in GENERIC_TRANSIT_TOKENS:
                for sid, st in location_service.stations_by_id.items():
                    sname = st["name"].lower()
                    clean_name = st["clean_name"].lower()
                    if clean_q == sname or clean_q == clean_name:
                        matched_sids.add(sid)
                    elif len(clean_q) >= 4 and (clean_q in sname or clean_q in clean_name):
                        matched_sids.add(sid)

        # 3. Fallback to token index (strictly excluding lone generic transit tokens)
        if not matched_sids:
            tokens = re.findall(r'[a-zA-Z0-9\u0900-\u097F]+', clean_q)
            valid_tokens = [tok for tok in tokens if len(tok) >= 4 and tok.lower() not in GENERIC_TRANSIT_TOKENS]
            for tok in valid_tokens:
                if tok in self.station_token_map:
                    matched_sids.update(self.station_token_map[tok])

        sample_stop_names = []
        for sid in list(matched_sids)[:4]:
            if sid in location_service.stations_by_id:
                sample_stop_names.append(location_service.stations_by_id[sid]["name"])

        is_ambig = len(matched_sids) > 15
        return ResolvedPlace(
            query_text=place_query,
            canonical_name=canonical_name,
            display_name=display_name,
            matched_station_ids=sorted(list(matched_sids)),
            sample_stops=sample_stop_names,
            is_ambiguous=is_ambig
        )

    def search_routes(
        self,
        origin_query: str,
        destination_query: str,
        departure_time: Optional[str] = None,
        limit_direct: Optional[int] = None,
        limit_transfer: int = 5,
        limit_two_transfer: int = 3,
        include_two_transfers: bool = False
    ) -> JourneySearchResult:
        """
        Executes complete Origin-Destination pathfinding across the audited CityLinc transit network:
        - Direct routes (0 transfers)
        - 1-transfer routes across all verified shared/walkable station clusters
        - 2-transfer routes across multi-hop transit paths when required
        - Strict topological directionality and timetable feasibility checks
        """
        origin_resolved = self.resolve_place(origin_query)
        dest_resolved = self.resolve_place(destination_query)

        if not origin_resolved.matched_station_ids:
            return JourneySearchResult(
                success=False,
                origin=origin_resolved,
                destination=dest_resolved,
                direct_routes_count=0,
                direct_routes=[],
                transfer_routes_count=0,
                transfer_routes=[],
                multi_hop_journeys=[],
                clarification_question=f"मला '{origin_query}' हा थांबा सापडला नाही. कृपया थांब्याचे नाव पुन्हा सांगा (उदा. CBS, Nashik Road, Panchavati, Satpur).",
                message=f"Origin '{origin_query}' could not be resolved to any CityLinc bus stop."
            )

        if not dest_resolved.matched_station_ids:
            return JourneySearchResult(
                success=False,
                origin=origin_resolved,
                destination=dest_resolved,
                direct_routes_count=0,
                direct_routes=[],
                transfer_routes_count=0,
                transfer_routes=[],
                multi_hop_journeys=[],
                clarification_question=f"मला '{destination_query}' हे ठिकाण सापडले नाही आणि कोणताही मार्ग आढळला नाही. कृपया योग्य गंतव्य सांगा.",
                message=f"Destination '{destination_query}' could not be resolved to any CityLinc bus stop."
            )

        origin_sids = set(origin_resolved.matched_station_ids)
        dest_sids = set(dest_resolved.matched_station_ids)

        o_clusters, d_clusters = transit_graph.get_origin_and_dest_clusters(origin_sids, dest_sids)

        # Check for identical origin and destination (Test Case 14)
        if (
            origin_resolved.canonical_name == dest_resolved.canonical_name
            or origin_query.strip().lower() == destination_query.strip().lower()
            or (o_clusters == d_clusters and len(o_clusters) == 1)
        ):
            return JourneySearchResult(
                success=False,
                origin=origin_resolved,
                destination=dest_resolved,
                direct_routes_count=0,
                direct_routes=[],
                transfer_routes_count=0,
                transfer_routes=[],
                multi_hop_journeys=[],
                clarification_question="तुम्ही आधीच त्या ठिकाणी आहात. कृपया वेगळे गंतव्य ठिकाण सांगा.",
                message=f"Origin and destination are the same location ('{origin_resolved.display_name}'). Please choose a different destination."
            )

        # Determine target departure time in minutes from midnight
        departure_mins = None
        if departure_time:
            departure_mins = parse_time_to_minutes(departure_time)
            if departure_mins is None:
                departure_mins = parse_natural_departure_time(departure_time)

        if departure_mins is None:
            departure_mins = (
                parse_natural_departure_time(origin_query)
                or parse_natural_departure_time(destination_query)
            )

        # Default to 08:00 AM (480 mins) if no explicit departure time requested
        dep_mins_used = departure_mins if departure_mins is not None else 480
        dep_str_used = format_minutes_to_time(dep_mins_used)

        # -------------------------------------------------------------
        # 1. Direct Routes Search
        # -------------------------------------------------------------
        raw_directs = transit_graph.find_direct_paths(origin_sids, dest_sids, departure_mins=dep_mins_used)
        direct_options: List[DirectRouteOption] = []
        planned_journeys: List[PlannedJourney] = []

        for d in raw_directs:
            b_node = d["boarding_stop"]
            a_node = d["alighting_stop"]
            # Strict Journey Destination Validation: ensure alighting stop reaches requested destination
            if a_node.station_id not in dest_sids:
                continue
            t_info = d["trip_info"]

            b_stop = StopInfo(
                station_id=b_node.station_id,
                name=b_node.station_name,
                display_name=b_node.display_name,
                sequence=b_node.sequence,
                distance_from_origin_km=b_node.total_distance_km,
                lat=b_node.lat,
                lon=b_node.lon,
                direction=location_service._determine_direction(b_node.station_name)
            )

            a_stop = StopInfo(
                station_id=a_node.station_id,
                name=a_node.station_name,
                display_name=a_node.display_name,
                sequence=a_node.sequence,
                distance_from_origin_km=a_node.total_distance_km,
                lat=a_node.lat,
                lon=a_node.lon,
                direction=location_service._determine_direction(a_node.station_name)
            )

            inter_stops = [
                StopInfo(
                    station_id=s.station_id,
                    name=s.station_name,
                    display_name=s.display_name,
                    sequence=s.sequence,
                    distance_from_origin_km=s.total_distance_km,
                    lat=s.lat,
                    lon=s.lon,
                    direction=location_service._determine_direction(s.station_name)
                ) for s in d["intermediate_stops"]
            ]

            dep_time = t_info["departure_str"] if t_info else None
            arr_time = t_info["arrival_str"] if t_info else None
            dur_mins = t_info["duration_mins"] if t_info else 30

            dr_opt = DirectRouteOption(
                route_id=d["route_id"],
                rno=d["rno"],
                route_name=d["route_name"],
                route_type=d["route_type"],
                boarding_stop=b_stop,
                alighting_stop=a_stop,
                intermediate_stops_count=d["intermediate_stops_count"],
                intermediate_stops=inter_stops,
                distance_km=d["distance_km"],
                timetable_trips_count=d["timetable_trips_count"],
                scheduled_departures=d["scheduled_departures"],
                polyline=d["polyline"],
                departure_time=dep_time,
                arrival_time=arr_time,
                duration_mins=dur_mins
            )
            direct_options.append(dr_opt)

            # Build PlannedJourney for direct route
            leg = JourneyLeg(
                leg_number=1,
                route_id=d["route_id"],
                rno=d["rno"],
                route_name=d["route_name"],
                route_type=d["route_type"],
                boarding_stop=b_stop,
                alighting_stop=a_stop,
                intermediate_stops_count=d["intermediate_stops_count"],
                intermediate_stops=inter_stops,
                distance_km=d["distance_km"],
                trip_id=t_info["trip_id"] if t_info else None,
                vehicle_no=t_info["vehicle_no"] if t_info else None,
                departure_time=dep_time,
                departure_mins=t_info["departure_mins"] if t_info else None,
                arrival_time=arr_time,
                arrival_mins=t_info["arrival_mins"] if t_info else None,
                duration_mins=dur_mins,
                timetable_trips_count=d["timetable_trips_count"],
                scheduled_departures=d["scheduled_departures"],
                polyline=d["polyline"]
            )

            fare_val = self._compute_fare(d["distance_km"])
            planned_journeys.append(PlannedJourney(
                journey_id=f"direct_{d['rno']}_{d['route_id']}",
                transfers_count=0,
                legs_count=1,
                departure_time=dep_time or dep_str_used,
                arrival_time=arr_time or dep_str_used,
                total_duration_mins=dur_mins,
                total_distance_km=d["distance_km"],
                total_stops_count=d["intermediate_stops_count"],
                total_fare_inr=fare_val,
                legs=[leg],
                transfers=[],
                is_timetable_feasible=True if (t_info and not t_info.get("is_next_day")) else (t_info is not None),
                summary_text_mr=f"थेट बस मार्ग {d['rno']} ({d['route_name']}): '{b_stop.name}' वरून चढावे आणि '{a_stop.name}' वर उतरावे.",
                summary_text_hi=f"सीधी बस रूट {d['rno']} ({d['route_name']}): '{b_stop.name}' से बैठें और '{a_stop.name}' पर उतरें।",
                summary_text_en=f"Direct Route {d['rno']} ({d['route_name']}): Board at '{b_stop.name}' and alight at '{a_stop.name}'."
            ))

        pruned_direct = direct_options[:limit_direct] if limit_direct is not None else direct_options

        # -------------------------------------------------------------
        # 2. 1-Transfer Routes Search
        # -------------------------------------------------------------
        transfer_options: List[TransferOption] = []
        raw_transfers = transit_graph.find_one_transfer_paths(
            origin_sids,
            dest_sids,
            departure_mins=dep_mins_used,
            limit=limit_transfer
        )

        for tr in raw_transfers:
            l1 = tr["leg1"]
            l2 = tr["leg2"]

            b1 = l1["boarding_stop"]
            a1 = l1["alighting_stop"]
            b2 = l2["boarding_stop"]
            a2 = l2["alighting_stop"]

            # Strict Journey Destination Validation: ensure final alighting stop reaches requested destination
            if a2.station_id not in dest_sids:
                continue

            t_leg1 = TransferLeg(
                leg_number=1,
                route_id=l1["route_id"],
                rno=l1["rno"],
                route_name=l1["route_name"],
                boarding_stop=StopInfo(
                    station_id=b1.station_id,
                    name=b1.station_name,
                    display_name=b1.display_name,
                    sequence=b1.sequence,
                    distance_from_origin_km=b1.total_distance_km,
                    lat=b1.lat,
                    lon=b1.lon,
                    direction=location_service._determine_direction(b1.station_name)
                ),
                alighting_stop=StopInfo(
                    station_id=a1.station_id,
                    name=a1.station_name,
                    display_name=a1.display_name,
                    sequence=a1.sequence,
                    distance_from_origin_km=a1.total_distance_km,
                    lat=a1.lat,
                    lon=a1.lon,
                    direction=location_service._determine_direction(a1.station_name)
                ),
                stops_count=l1["stops_count"],
                distance_km=l1["distance_km"],
                timetable_trips_count=l1["trips_count"],
                scheduled_departures=l1["departures"],
                departure_time=l1["trip_info"]["departure_str"] if l1["trip_info"] else None,
                arrival_time=l1["trip_info"]["arrival_str"] if l1["trip_info"] else None,
                duration_mins=l1["trip_info"]["duration_mins"] if l1["trip_info"] else 20
            )

            t_leg2 = TransferLeg(
                leg_number=2,
                route_id=l2["route_id"],
                rno=l2["rno"],
                route_name=l2["route_name"],
                boarding_stop=StopInfo(
                    station_id=b2.station_id,
                    name=b2.station_name,
                    display_name=b2.display_name,
                    sequence=b2.sequence,
                    distance_from_origin_km=b2.total_distance_km,
                    lat=b2.lat,
                    lon=b2.lon,
                    direction=location_service._determine_direction(b2.station_name)
                ),
                alighting_stop=StopInfo(
                    station_id=a2.station_id,
                    name=a2.station_name,
                    display_name=a2.display_name,
                    sequence=a2.sequence,
                    distance_from_origin_km=a2.total_distance_km,
                    lat=a2.lat,
                    lon=a2.lon,
                    direction=location_service._determine_direction(a2.station_name)
                ),
                stops_count=l2["stops_count"],
                distance_km=l2["distance_km"],
                timetable_trips_count=l2["trips_count"],
                scheduled_departures=l2["departures"],
                departure_time=l2["trip_info"]["departure_str"] if l2["trip_info"] else None,
                arrival_time=l2["trip_info"]["arrival_str"] if l2["trip_info"] else None,
                duration_mins=l2["trip_info"]["duration_mins"] if l2["trip_info"] else 20
            )

            tr_opt = TransferOption(
                interchange_hub=tr["interchange_hub"],
                interchange_hub_mr=tr["interchange_hub"],
                transfer_stop_name=tr["transfer_from_name"],
                leg1=t_leg1,
                leg2=t_leg2,
                total_distance_km=tr["total_distance_km"],
                total_stops_count=tr["total_stops_count"],
                is_timetable_feasible=tr["is_feasible"],
                departure_time=t_leg1.departure_time,
                arrival_time=t_leg2.arrival_time,
                total_duration_mins=tr["total_duration_mins"],
                wait_time_mins=tr["wait_time_mins"],
                walk_time_mins=tr["walk_time_mins"]
            )
            transfer_options.append(tr_opt)

            # Build PlannedJourney for 1-transfer
            j_leg1 = JourneyLeg(
                leg_number=1,
                route_id=l1["route_id"],
                rno=l1["rno"],
                route_name=l1["route_name"],
                route_type=l1["direction"],
                boarding_stop=t_leg1.boarding_stop,
                alighting_stop=t_leg1.alighting_stop,
                intermediate_stops_count=l1["stops_count"],
                intermediate_stops=[],
                distance_km=l1["distance_km"],
                trip_id=l1["trip_info"]["trip_id"] if l1["trip_info"] else None,
                vehicle_no=l1["trip_info"]["vehicle_no"] if l1["trip_info"] else None,
                departure_time=t_leg1.departure_time,
                departure_mins=l1["trip_info"]["departure_mins"] if l1["trip_info"] else None,
                arrival_time=t_leg1.arrival_time,
                arrival_mins=l1["trip_info"]["arrival_mins"] if l1["trip_info"] else None,
                duration_mins=t_leg1.duration_mins,
                timetable_trips_count=l1["trips_count"],
                scheduled_departures=l1["departures"]
            )

            j_leg2 = JourneyLeg(
                leg_number=2,
                route_id=l2["route_id"],
                rno=l2["rno"],
                route_name=l2["route_name"],
                route_type=l2["direction"],
                boarding_stop=t_leg2.boarding_stop,
                alighting_stop=t_leg2.alighting_stop,
                intermediate_stops_count=l2["stops_count"],
                intermediate_stops=[],
                distance_km=l2["distance_km"],
                trip_id=l2["trip_info"]["trip_id"] if l2["trip_info"] else None,
                vehicle_no=l2["trip_info"]["vehicle_no"] if l2["trip_info"] else None,
                departure_time=t_leg2.departure_time,
                departure_mins=l2["trip_info"]["departure_mins"] if l2["trip_info"] else None,
                arrival_time=t_leg2.arrival_time,
                arrival_mins=l2["trip_info"]["arrival_mins"] if l2["trip_info"] else None,
                duration_mins=t_leg2.duration_mins,
                timetable_trips_count=l2["trips_count"],
                scheduled_departures=l2["departures"]
            )

            tp = TransferPoint(
                from_leg_number=1,
                to_leg_number=2,
                interchange_hub=tr["interchange_hub"],
                interchange_hub_mr=tr["interchange_hub"],
                transfer_from_stop=t_leg1.alighting_stop,
                transfer_to_stop=t_leg2.boarding_stop,
                walk_distance_m=tr["walk_distance_m"],
                walk_time_mins=tr["walk_time_mins"],
                wait_time_mins=tr["wait_time_mins"],
                description=f"Walk {tr['walk_distance_m']:.0f}m ({tr['walk_time_mins']} mins) at {tr['interchange_hub']}"
            )

            fare1 = self._compute_fare(l1["distance_km"])
            fare2 = self._compute_fare(l2["distance_km"])

            planned_journeys.append(PlannedJourney(
                journey_id=f"transfer_1_{l1['rno']}_{l2['rno']}",
                transfers_count=1,
                legs_count=2,
                departure_time=t_leg1.departure_time or dep_str_used,
                arrival_time=t_leg2.arrival_time or dep_str_used,
                total_duration_mins=tr["total_duration_mins"],
                total_distance_km=tr["total_distance_km"],
                total_stops_count=tr["total_stops_count"],
                total_fare_inr=fare1 + fare2,
                legs=[j_leg1, j_leg2],
                transfers=[tp],
                is_timetable_feasible=tr["is_feasible"],
                summary_text_mr=f"{tr['interchange_hub']} येथे १ बस बदला: मार्ग {l1['rno']} ने '{tr['transfer_from_name']}' ला जा, व तेथून मार्ग {l2['rno']} ने जा.",
                summary_text_hi=f"{tr['interchange_hub']} पर 1 बस बदलें: रूट {l1['rno']} से '{tr['transfer_from_name']}' जाएं, फिर रूट {l2['rno']} लें।",
                summary_text_en=f"1 transfer at {tr['interchange_hub']}: take Route {l1['rno']} to '{tr['transfer_from_name']}', then transfer to Route {l2['rno']}."
            ))

        # -------------------------------------------------------------
        # 3. 2-Transfer Routes Search (Multi-Hop)
        # -------------------------------------------------------------
        if (len(pruned_direct) == 0 and len(transfer_options) == 0) or include_two_transfers:
            raw_two_transfers = transit_graph.find_two_transfer_paths(
                origin_sids,
                dest_sids,
                departure_mins=dep_mins_used,
                limit=limit_two_transfer
            )

            for tr2 in raw_two_transfers:
                l1 = tr2["leg1"]
                l2 = tr2["leg2"]
                l3 = tr2["leg3"]

                # Legs
                j1 = JourneyLeg(
                    leg_number=1,
                    route_id=l1["route_id"],
                    rno=l1["rno"],
                    route_name=l1["route_name"],
                    route_type="Standard",
                    boarding_stop=StopInfo(
                        station_id=l1["boarding_stop"].station_id,
                        name=l1["boarding_stop"].station_name,
                        display_name=l1["boarding_stop"].display_name,
                        sequence=l1["boarding_stop"].sequence,
                        distance_from_origin_km=l1["boarding_stop"].total_distance_km,
                        lat=l1["boarding_stop"].lat,
                        lon=l1["boarding_stop"].lon,
                        direction=location_service._determine_direction(l1["boarding_stop"].station_name)
                    ),
                    alighting_stop=StopInfo(
                        station_id=l1["alighting_stop"].station_id,
                        name=l1["alighting_stop"].station_name,
                        display_name=l1["alighting_stop"].display_name,
                        sequence=l1["alighting_stop"].sequence,
                        distance_from_origin_km=l1["alighting_stop"].total_distance_km,
                        lat=l1["alighting_stop"].lat,
                        lon=l1["alighting_stop"].lon,
                        direction=location_service._determine_direction(l1["alighting_stop"].station_name)
                    ),
                    intermediate_stops_count=l1["stops_count"],
                    intermediate_stops=[],
                    distance_km=l1["distance_km"],
                    trip_id=l1["trip_info"]["trip_id"] if l1["trip_info"] else None,
                    vehicle_no=l1["trip_info"]["vehicle_no"] if l1["trip_info"] else None,
                    departure_time=l1["trip_info"]["departure_str"] if l1["trip_info"] else None,
                    departure_mins=l1["trip_info"]["departure_mins"] if l1["trip_info"] else None,
                    arrival_time=l1["trip_info"]["arrival_str"] if l1["trip_info"] else None,
                    arrival_mins=l1["trip_info"]["arrival_mins"] if l1["trip_info"] else None,
                    duration_mins=l1["trip_info"]["duration_mins"] if l1["trip_info"] else 20,
                    timetable_trips_count=l1["trips_count"],
                    scheduled_departures=l1["departures"]
                )

                j2 = JourneyLeg(
                    leg_number=2,
                    route_id=l2["route_id"],
                    rno=l2["rno"],
                    route_name=l2["route_name"],
                    route_type="Standard",
                    boarding_stop=StopInfo(
                        station_id=l2["boarding_stop"].station_id,
                        name=l2["boarding_stop"].station_name,
                        display_name=l2["boarding_stop"].display_name,
                        sequence=l2["boarding_stop"].sequence,
                        distance_from_origin_km=l2["boarding_stop"].total_distance_km,
                        lat=l2["boarding_stop"].lat,
                        lon=l2["boarding_stop"].lon,
                        direction=location_service._determine_direction(l2["boarding_stop"].station_name)
                    ),
                    alighting_stop=StopInfo(
                        station_id=l2["alighting_stop"].station_id,
                        name=l2["alighting_stop"].station_name,
                        display_name=l2["alighting_stop"].display_name,
                        sequence=l2["alighting_stop"].sequence,
                        distance_from_origin_km=l2["alighting_stop"].total_distance_km,
                        lat=l2["alighting_stop"].lat,
                        lon=l2["alighting_stop"].lon,
                        direction=location_service._determine_direction(l2["alighting_stop"].station_name)
                    ),
                    intermediate_stops_count=l2["stops_count"],
                    intermediate_stops=[],
                    distance_km=l2["distance_km"],
                    trip_id=l2["trip_info"]["trip_id"] if l2["trip_info"] else None,
                    vehicle_no=l2["trip_info"]["vehicle_no"] if l2["trip_info"] else None,
                    departure_time=l2["trip_info"]["departure_str"] if l2["trip_info"] else None,
                    departure_mins=l2["trip_info"]["departure_mins"] if l2["trip_info"] else None,
                    arrival_time=l2["trip_info"]["arrival_str"] if l2["trip_info"] else None,
                    arrival_mins=l2["trip_info"]["arrival_mins"] if l2["trip_info"] else None,
                    duration_mins=l2["trip_info"]["duration_mins"] if l2["trip_info"] else 20,
                    timetable_trips_count=l2["trips_count"],
                    scheduled_departures=l2["departures"]
                )

                j3 = JourneyLeg(
                    leg_number=3,
                    route_id=l3["route_id"],
                    rno=l3["rno"],
                    route_name=l3["route_name"],
                    route_type="Standard",
                    boarding_stop=StopInfo(
                        station_id=l3["boarding_stop"].station_id,
                        name=l3["boarding_stop"].station_name,
                        display_name=l3["boarding_stop"].display_name,
                        sequence=l3["boarding_stop"].sequence,
                        distance_from_origin_km=l3["boarding_stop"].total_distance_km,
                        lat=l3["boarding_stop"].lat,
                        lon=l3["boarding_stop"].lon,
                        direction=location_service._determine_direction(l3["boarding_stop"].station_name)
                    ),
                    alighting_stop=StopInfo(
                        station_id=l3["alighting_stop"].station_id,
                        name=l3["alighting_stop"].station_name,
                        display_name=l3["alighting_stop"].display_name,
                        sequence=l3["alighting_stop"].sequence,
                        distance_from_origin_km=l3["alighting_stop"].total_distance_km,
                        lat=l3["alighting_stop"].lat,
                        lon=l3["alighting_stop"].lon,
                        direction=location_service._determine_direction(l3["alighting_stop"].station_name)
                    ),
                    intermediate_stops_count=l3["stops_count"],
                    intermediate_stops=[],
                    distance_km=l3["distance_km"],
                    trip_id=l3["trip_info"]["trip_id"] if l3["trip_info"] else None,
                    vehicle_no=l3["trip_info"]["vehicle_no"] if l3["trip_info"] else None,
                    departure_time=l3["trip_info"]["departure_str"] if l3["trip_info"] else None,
                    departure_mins=l3["trip_info"]["departure_mins"] if l3["trip_info"] else None,
                    arrival_time=l3["trip_info"]["arrival_str"] if l3["trip_info"] else None,
                    arrival_mins=l3["trip_info"]["arrival_mins"] if l3["trip_info"] else None,
                    duration_mins=l3["trip_info"]["duration_mins"] if l3["trip_info"] else 20,
                    timetable_trips_count=l3["trips_count"],
                    scheduled_departures=l3["departures"]
                )

                tp1 = TransferPoint(
                    from_leg_number=1,
                    to_leg_number=2,
                    interchange_hub=tr2["interchange_hub_1"],
                    interchange_hub_mr=tr2["interchange_hub_1"],
                    transfer_from_stop=j1.alighting_stop,
                    transfer_to_stop=j2.boarding_stop,
                    walk_distance_m=tr2["transfer1"]["walk_distance_m"],
                    walk_time_mins=tr2["transfer1"]["walk_time_mins"],
                    wait_time_mins=tr2["transfer1"]["wait_time_mins"],
                    description=f"Transfer at {tr2['interchange_hub_1']}"
                )

                tp2 = TransferPoint(
                    from_leg_number=2,
                    to_leg_number=3,
                    interchange_hub=tr2["interchange_hub_2"],
                    interchange_hub_mr=tr2["interchange_hub_2"],
                    transfer_from_stop=j2.alighting_stop,
                    transfer_to_stop=j3.boarding_stop,
                    walk_distance_m=tr2["transfer2"]["walk_distance_m"],
                    walk_time_mins=tr2["transfer2"]["walk_time_mins"],
                    wait_time_mins=tr2["transfer2"]["wait_time_mins"],
                    description=f"Transfer at {tr2['interchange_hub_2']}"
                )

                f1 = self._compute_fare(l1["distance_km"])
                f2 = self._compute_fare(l2["distance_km"])
                f3 = self._compute_fare(l3["distance_km"])

                planned_journeys.append(PlannedJourney(
                    journey_id=f"transfer_2_{l1['rno']}_{l2['rno']}_{l3['rno']}",
                    transfers_count=2,
                    legs_count=3,
                    departure_time=j1.departure_time or dep_str_used,
                    arrival_time=j3.arrival_time or dep_str_used,
                    total_duration_mins=tr2["total_duration_mins"],
                    total_distance_km=tr2["total_distance_km"],
                    total_stops_count=tr2["total_stops_count"],
                    total_fare_inr=f1 + f2 + f3,
                    legs=[j1, j2, j3],
                    transfers=[tp1, tp2],
                    is_timetable_feasible=tr2["is_feasible"],
                    summary_text_mr=f"२ बस बदला: {tr2['interchange_hub_1']} (मार्ग {l1['rno']} -> {l2['rno']}) आणि {tr2['interchange_hub_2']} (मार्ग {l2['rno']} -> {l3['rno']}).",
                    summary_text_hi=f"2 बस बदलें: {tr2['interchange_hub_1']} (रूट {l1['rno']} -> {l2['rno']}) और {tr2['interchange_hub_2']} (रूट {l2['rno']} -> {l3['rno']})।",
                    summary_text_en=f"2 transfers: at {tr2['interchange_hub_1']} (Route {l1['rno']} -> {l2['rno']}) and {tr2['interchange_hub_2']} (Route {l2['rno']} -> {l3['rno']})."
                ))

        # -------------------------------------------------------------
        # 3B. 3-Transfer Routes Search (Multi-Hop) when needed (Phase 4.8)
        # -------------------------------------------------------------
        has_feasible_journey = any(pj.is_timetable_feasible for pj in planned_journeys)
        if (len(planned_journeys) == 0 or not has_feasible_journey):
            raw_three_transfers = transit_graph.find_three_transfer_paths(
                origin_sids,
                dest_sids,
                departure_mins=dep_mins_used,
                limit=3
            )
            for tr3 in raw_three_transfers:
                l1 = tr3["leg1"]
                l2 = tr3["leg2"]
                l3 = tr3["leg3"]
                l4 = tr3["leg4"]

                j1 = JourneyLeg(
                    leg_number=1,
                    route_id=l1["route_id"],
                    rno=l1["rno"],
                    route_name=l1["route_name"],
                    route_type="Standard",
                    boarding_stop=StopInfo(
                        station_id=l1["boarding_stop"].station_id,
                        name=l1["boarding_stop"].station_name,
                        display_name=l1["boarding_stop"].display_name,
                        sequence=l1["boarding_stop"].sequence,
                        distance_from_origin_km=l1["boarding_stop"].total_distance_km,
                        lat=l1["boarding_stop"].lat,
                        lon=l1["boarding_stop"].lon,
                        direction=location_service._determine_direction(l1["boarding_stop"].station_name)
                    ),
                    alighting_stop=StopInfo(
                        station_id=l1["alighting_stop"].station_id,
                        name=l1["alighting_stop"].station_name,
                        display_name=l1["alighting_stop"].display_name,
                        sequence=l1["alighting_stop"].sequence,
                        distance_from_origin_km=l1["alighting_stop"].total_distance_km,
                        lat=l1["alighting_stop"].lat,
                        lon=l1["alighting_stop"].lon,
                        direction=location_service._determine_direction(l1["alighting_stop"].station_name)
                    ),
                    intermediate_stops_count=l1["stops_count"],
                    intermediate_stops=[],
                    distance_km=l1["distance_km"],
                    trip_id=l1["trip_info"]["trip_id"] if l1["trip_info"] else None,
                    vehicle_no=l1["trip_info"]["vehicle_no"] if l1["trip_info"] else None,
                    departure_time=l1["trip_info"]["departure_str"] if l1["trip_info"] else None,
                    departure_mins=l1["trip_info"]["departure_mins"] if l1["trip_info"] else None,
                    arrival_time=l1["trip_info"]["arrival_str"] if l1["trip_info"] else None,
                    arrival_mins=l1["trip_info"]["arrival_mins"] if l1["trip_info"] else None,
                    duration_mins=l1["trip_info"]["duration_mins"] if l1["trip_info"] else 20,
                    timetable_trips_count=l1["trips_count"],
                    scheduled_departures=l1["departures"]
                )

                j2 = JourneyLeg(
                    leg_number=2,
                    route_id=l2["route_id"],
                    rno=l2["rno"],
                    route_name=l2["route_name"],
                    route_type="Standard",
                    boarding_stop=StopInfo(
                        station_id=l2["boarding_stop"].station_id,
                        name=l2["boarding_stop"].station_name,
                        display_name=l2["boarding_stop"].display_name,
                        sequence=l2["boarding_stop"].sequence,
                        distance_from_origin_km=l2["boarding_stop"].total_distance_km,
                        lat=l2["boarding_stop"].lat,
                        lon=l2["boarding_stop"].lon,
                        direction=location_service._determine_direction(l2["boarding_stop"].station_name)
                    ),
                    alighting_stop=StopInfo(
                        station_id=l2["alighting_stop"].station_id,
                        name=l2["alighting_stop"].station_name,
                        display_name=l2["alighting_stop"].display_name,
                        sequence=l2["alighting_stop"].sequence,
                        distance_from_origin_km=l2["alighting_stop"].total_distance_km,
                        lat=l2["alighting_stop"].lat,
                        lon=l2["alighting_stop"].lon,
                        direction=location_service._determine_direction(l2["alighting_stop"].station_name)
                    ),
                    intermediate_stops_count=l2["stops_count"],
                    intermediate_stops=[],
                    distance_km=l2["distance_km"],
                    trip_id=l2["trip_info"]["trip_id"] if l2["trip_info"] else None,
                    vehicle_no=l2["trip_info"]["vehicle_no"] if l2["trip_info"] else None,
                    departure_time=l2["trip_info"]["departure_str"] if l2["trip_info"] else None,
                    departure_mins=l2["trip_info"]["departure_mins"] if l2["trip_info"] else None,
                    arrival_time=l2["trip_info"]["arrival_str"] if l2["trip_info"] else None,
                    arrival_mins=l2["trip_info"]["arrival_mins"] if l2["trip_info"] else None,
                    duration_mins=l2["trip_info"]["duration_mins"] if l2["trip_info"] else 20,
                    timetable_trips_count=l2["trips_count"],
                    scheduled_departures=l2["departures"]
                )

                j3 = JourneyLeg(
                    leg_number=3,
                    route_id=l3["route_id"],
                    rno=l3["rno"],
                    route_name=l3["route_name"],
                    route_type="Standard",
                    boarding_stop=StopInfo(
                        station_id=l3["boarding_stop"].station_id,
                        name=l3["boarding_stop"].station_name,
                        display_name=l3["boarding_stop"].display_name,
                        sequence=l3["boarding_stop"].sequence,
                        distance_from_origin_km=l3["boarding_stop"].total_distance_km,
                        lat=l3["boarding_stop"].lat,
                        lon=l3["boarding_stop"].lon,
                        direction=location_service._determine_direction(l3["boarding_stop"].station_name)
                    ),
                    alighting_stop=StopInfo(
                        station_id=l3["alighting_stop"].station_id,
                        name=l3["alighting_stop"].station_name,
                        display_name=l3["alighting_stop"].display_name,
                        sequence=l3["alighting_stop"].sequence,
                        distance_from_origin_km=l3["alighting_stop"].total_distance_km,
                        lat=l3["alighting_stop"].lat,
                        lon=l3["alighting_stop"].lon,
                        direction=location_service._determine_direction(l3["alighting_stop"].station_name)
                    ),
                    intermediate_stops_count=l3["stops_count"],
                    intermediate_stops=[],
                    distance_km=l3["distance_km"],
                    trip_id=l3["trip_info"]["trip_id"] if l3["trip_info"] else None,
                    vehicle_no=l3["trip_info"]["vehicle_no"] if l3["trip_info"] else None,
                    departure_time=l3["trip_info"]["departure_str"] if l3["trip_info"] else None,
                    departure_mins=l3["trip_info"]["departure_mins"] if l3["trip_info"] else None,
                    arrival_time=l3["trip_info"]["arrival_str"] if l3["trip_info"] else None,
                    arrival_mins=l3["trip_info"]["arrival_mins"] if l3["trip_info"] else None,
                    duration_mins=l3["trip_info"]["duration_mins"] if l3["trip_info"] else 20,
                    timetable_trips_count=l3["trips_count"],
                    scheduled_departures=l3["departures"]
                )

                j4 = JourneyLeg(
                    leg_number=4,
                    route_id=l4["route_id"],
                    rno=l4["rno"],
                    route_name=l4["route_name"],
                    route_type="Standard",
                    boarding_stop=StopInfo(
                        station_id=l4["boarding_stop"].station_id,
                        name=l4["boarding_stop"].station_name,
                        display_name=l4["boarding_stop"].display_name,
                        sequence=l4["boarding_stop"].sequence,
                        distance_from_origin_km=l4["boarding_stop"].total_distance_km,
                        lat=l4["boarding_stop"].lat,
                        lon=l4["boarding_stop"].lon,
                        direction=location_service._determine_direction(l4["boarding_stop"].station_name)
                    ),
                    alighting_stop=StopInfo(
                        station_id=l4["alighting_stop"].station_id,
                        name=l4["alighting_stop"].station_name,
                        display_name=l4["alighting_stop"].display_name,
                        sequence=l4["alighting_stop"].sequence,
                        distance_from_origin_km=l4["alighting_stop"].total_distance_km,
                        lat=l4["alighting_stop"].lat,
                        lon=l4["alighting_stop"].lon,
                        direction=location_service._determine_direction(l4["alighting_stop"].station_name)
                    ),
                    intermediate_stops_count=l4["stops_count"],
                    intermediate_stops=[],
                    distance_km=l4["distance_km"],
                    trip_id=l4["trip_info"]["trip_id"] if l4["trip_info"] else None,
                    vehicle_no=l4["trip_info"]["vehicle_no"] if l4["trip_info"] else None,
                    departure_time=l4["trip_info"]["departure_str"] if l4["trip_info"] else None,
                    departure_mins=l4["trip_info"]["departure_mins"] if l4["trip_info"] else None,
                    arrival_time=l4["trip_info"]["arrival_str"] if l4["trip_info"] else None,
                    arrival_mins=l4["trip_info"]["arrival_mins"] if l4["trip_info"] else None,
                    duration_mins=l4["trip_info"]["duration_mins"] if l4["trip_info"] else 20,
                    timetable_trips_count=l4["trips_count"],
                    scheduled_departures=l4["departures"]
                )

                tp1 = TransferPoint(
                    from_leg_number=1,
                    to_leg_number=2,
                    interchange_hub=tr3["interchange1"],
                    interchange_hub_mr=tr3["interchange1"],
                    transfer_from_stop=j1.alighting_stop,
                    transfer_to_stop=j2.boarding_stop,
                    walk_distance_m=tr3["transfer1"]["walk_distance_m"],
                    walk_time_mins=tr3["transfer1"]["walk_time_mins"],
                    wait_time_mins=tr3["transfer1"]["wait_time_mins"],
                    description=f"Transfer at {tr3['interchange1']}"
                )

                tp2 = TransferPoint(
                    from_leg_number=2,
                    to_leg_number=3,
                    interchange_hub=tr3["interchange2"],
                    interchange_hub_mr=tr3["interchange2"],
                    transfer_from_stop=j2.alighting_stop,
                    transfer_to_stop=j3.boarding_stop,
                    walk_distance_m=tr3["transfer2"]["walk_distance_m"],
                    walk_time_mins=tr3["transfer2"]["walk_time_mins"],
                    wait_time_mins=tr3["transfer2"]["wait_time_mins"],
                    description=f"Transfer at {tr3['interchange2']}"
                )

                tp3 = TransferPoint(
                    from_leg_number=3,
                    to_leg_number=4,
                    interchange_hub=tr3["interchange3"],
                    interchange_hub_mr=tr3["interchange3"],
                    transfer_from_stop=j3.alighting_stop,
                    transfer_to_stop=j4.boarding_stop,
                    walk_distance_m=tr3["transfer3"]["walk_distance_m"],
                    walk_time_mins=tr3["transfer3"]["walk_time_mins"],
                    wait_time_mins=tr3["transfer3"]["wait_time_mins"],
                    description=f"Transfer at {tr3['interchange3']}"
                )

                f1 = self._compute_fare(l1["distance_km"])
                f2 = self._compute_fare(l2["distance_km"])
                f3 = self._compute_fare(l3["distance_km"])
                f4 = self._compute_fare(l4["distance_km"])

                planned_journeys.append(PlannedJourney(
                    journey_id=f"transfer_3_{l1['rno']}_{l2['rno']}_{l3['rno']}_{l4['rno']}",
                    transfers_count=3,
                    legs_count=4,
                    departure_time=j1.departure_time or dep_str_used,
                    arrival_time=j4.arrival_time or dep_str_used,
                    total_duration_mins=tr3["total_duration_mins"],
                    total_distance_km=tr3["total_distance_km"],
                    total_stops_count=tr3["total_stops_count"],
                    total_fare_inr=f1 + f2 + f3 + f4,
                    legs=[j1, j2, j3, j4],
                    transfers=[tp1, tp2, tp3],
                    is_timetable_feasible=tr3["is_feasible"],
                    summary_text_mr=f"३ बस बदला: {tr3['interchange1']} (मार्ग {l1['rno']} -> {l2['rno']}), {tr3['interchange2']} (मार्ग {l2['rno']} -> {l3['rno']}), आणि {tr3['interchange3']} (मार्ग {l3['rno']} -> {l4['rno']}).",
                    summary_text_hi=f"3 बस बदलें: {tr3['interchange1']}, {tr3['interchange2']}, {tr3['interchange3']}।",
                    summary_text_en=f"3 transfers: via {tr3['interchange1']}, {tr3['interchange2']}, and {tr3['interchange3']}."
                ))

        # Rank planned journeys by feasibility, fewest transfers, shortest duration
        planned_journeys.sort(key=lambda pj: (
            not pj.is_timetable_feasible,
            pj.transfers_count,
            pj.total_duration_mins,
            pj.total_stops_count
        ))

        # -------------------------------------------------------------
        # 4. Fare Calculation
        # -------------------------------------------------------------
        fare_estimate = None
        if pruned_direct:
            top_dist = pruned_direct[0].distance_km
            fare_amt = self._compute_fare(top_dist)
            fare_estimate = {
                "distance_km": top_dist,
                "single_fare_inr": fare_amt,
                "pass_category": "Standard Municipal Tariff Slab"
            }
        elif transfer_options:
            top_dist = transfer_options[0].total_distance_km
            f1 = self._compute_fare(transfer_options[0].leg1.distance_km)
            f2 = self._compute_fare(transfer_options[0].leg2.distance_km)
            fare_estimate = {
                "distance_km": top_dist,
                "single_fare_inr": f1 + f2,
                "pass_category": "Multi-Leg Transfer (Individual Ticket per bus)"
            }
        elif planned_journeys:
            top_pj = planned_journeys[0]
            fare_estimate = {
                "distance_km": top_pj.total_distance_km,
                "single_fare_inr": top_pj.total_fare_inr,
                "pass_category": "Multi-Leg Journey"
            }

        # -------------------------------------------------------------
        # 5. Presentation Messages
        # -------------------------------------------------------------
        has_results = bool(pruned_direct or transfer_options or planned_journeys)
        if has_results:
            msg = f"Found {len(pruned_direct)} direct routes"
            if transfer_options:
                msg += f" and {len(transfer_options)} 1-transfer options"
            elif planned_journeys and planned_journeys[0].transfers_count == 2:
                msg += f" and {len(planned_journeys)} 2-transfer options"
            msg += f" between {origin_resolved.display_name} and {dest_resolved.display_name}."
        else:
            msg = f"No direct or connecting bus route found between {origin_resolved.display_name} and {dest_resolved.display_name}."

        return JourneySearchResult(
            success=has_results,
            origin=origin_resolved,
            destination=dest_resolved,
            direct_routes_count=len(pruned_direct),
            direct_routes=pruned_direct,
            transfer_routes_count=len(transfer_options),
            transfer_routes=transfer_options,
            multi_hop_journeys=planned_journeys,
            fare_estimate=fare_estimate,
            message=msg,
            departure_time_used=dep_str_used
        )

    def _compute_fare(self, distance_km: float) -> int:
        """Computes official CityLinc Nashik passenger fare slab using fare_service."""
        return fare_service.compute_stage_fare(distance_km)


# Global singleton
route_engine = TransitRouteEngine()
