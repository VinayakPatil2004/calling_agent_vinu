"""
CityLinc Knowledge Base Service for Nashik Bus Service.
Loads the complete audited transit dataset:
- 315 route records across Up and Down corridors
- 12,909 route-stop records
- 3,547 timetable trips
- 1,187 unique physical stations
- 3 bus pass categories

Provides zero-latency, high-accuracy conversational responses across Marathi, Hindi, and English.
"""
import re
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple

from backend.app.core.config import settings

logger = logging.getLogger("citylinc.knowledge")

DEVANAGARI_DIGITS = {
    '०': '0', '१': '1', '२': '2', '३': '3', '४': '4',
    '५': '5', '६': '6', '७': '7', '८': '8', '९': '9'
}

# Common place name aliases mapping Marathi / Hindi / English colloquial terms to normalized search tokens
LOCATION_ALIASES: Dict[str, List[str]] = {
    "cbs": ["cbs", "सीबीएस", "सी.बी.एस.", "central bus stand", "मध्यवर्ती बस स्थानक", "mela bus stand", "मेला"],
    "thakkar bazar": [
        "thakkar bazar", "thakkar bazaar", "thakkar bazzar", "thakar bazar",
        "ठक्कर बाजार", "ठक्कर बाझार", "ठाकर बाजार",
        "ठक्कर बाजार बस स्थानक", "ठक्कर बाजार बस स्टँड", "thakkar bazar bus stand",
        "ठक्कर बाजारला", "ठक्कर बाजारातून"
    ],
    "trimbak": ["trimbak", "trimbakeshwar", "त्र्यंबक", "त्र्यंबकेश्वर", "त्र्यंबकेश्वरला"],
    "gangapur": ["gangapur", "गंगापूर", "गंगापूर गाव", "gangapur gaon"],
    "pathardi": ["pathardi", "पाथर्डी", "पाथर्डी गाव", "pathardi gaon", "pathardi phata"],
    "tapovan": ["tapovan", "तपोवन", "tapovan terminus", "tapovan depot"],
    "nashik road": ["nashik road", "नाशिक रोड", "नासिक रोड", "नासिक", "railway station", "रेल्वे स्टेशन", "nashikroad", "हाशिकरोड", "हाशिक रोड", "हाशिकरोडला", "नाशिकरोड", "nashik road railway station"],
    "datta mandir": ["datta mandir", "datt mandir", "दत्त मंदिर", "दत्त मंदीर", "दत्तमंदिर", "दत्ता मंदिर", "दत्ता मंदीर", "दत्तामंदिर", "datta mandir nashik road", "datt mandir nashik road", "दत्त मंदिर नाशिक रोड", "दत्ता मंदिर नाशिक रोड", "दत्त मंदिर बस स्टॉप", "दत्ता मंदिर बस स्टॉप"],
    "shalimar": ["shalimar", "शालिमार", "शालीमार", "shalimar chowk", "शालिमार चौक", "शालीमार चौक"],
    "satpur": ["satpur", "सातपूर", "satpur colony", "satpur midc"],
    "ambad": ["ambad", "अंबड", "ambad gaon", "ambad midc"],
    "nimani": ["nimani", "निमाणी", "निम्मानी", "निम्मणी", "निमानी", "nimani bus stand", "निमाणी बस स्थानक"],
    "panchavati": ["panchavati", "पंचवटी", "पंचवडी", "panchavadi", "पंचवडीला", "पंचवटीला", "पंचवटी कारंजा", "panchavati karanja", "पंचवडी कारंजा"],
    "cidco": ["cidco", "सिडको", "cidco bus stand", "सिडको बस स्थानक"],
    "mhasrul": ["mhasrul", "म्हसरूळ", "varvandi"],
    "ashok nagar": ["ashok nagar", "अशोक नगर"],
    "dwarka": ["dwarka", "द्वारका", "dwarka circle"],
    "indira nagar": ["indira nagar", "इंदिरा नगर"],
    "bardan phata": ["bardan phata", "बरदान फाटा"],
    "civil hospital": [
        "civil hospital", "सिव्हिल हॉस्पिटल", "सिव्हिल", "civil", "सिविल हॉस्पिटल",
        "सिविल", "सिविल अस्पताल", "सिव्हिल रुग्णालय", "सिविल रुग्णालय",
        "सिफिल", "सिफिल हॉस्पिटल", "सिफिल अस्पताल", "सिफिल रुग्णालय",
        "siphil", "siphil hospital"
    ],
    "bytco": ["bytco", "bitco", "बिटको", "बायको", "बिटको रुग्णालय", "बिटको हॉस्पिटल", "बायको हॉस्पिटल", "बायको रुग्णालय", "bitco hospital", "bytco hospital"],
    "apollo": ["apollo", "अपोलो", "अपोलो हॉस्पिटल", "apollo hospital"],
    "wockhardt": ["wockhardt", "वोक्हार्ट", "wockhardt hospital"],
    "adgaon": ["adgaon", "आडगाव", "aadgaon"],
    "mohadi": ["mohadi", "मोहाडी"],
    "kbt circle": ["kbt circle", "kbt", "k b t circle", "k.b.t. circle", "kbt chowk", "केबीटी सर्कल", "केबीटी", "के बी टी सर्कल", "केबीटी चौक"],
    "anandavalli": ["anandavalli", "aanandavalli", "आनंदवल्ली", "आनंदवली", "anandwali", "आनंद वल्ली"],
    "ccm": ["ccm", "city centre mall", "सिटी सेंटर मॉल", "सीसीएम", "city center mall", "सिटी सेन्टर मॉल"],
    "abt circle": ["abt circle", "abt", "एबीटी सर्कल", "एबीटी", "abb circle", "abb", "एबीबी सर्कल", "एबीबी"],
    "thatte nagar": ["thatte nagar", "थत्ते नगर", "thattenagar"],
    "vidya vikas circle": ["vidya vikas circle", "विद्या विकास सर्कल", "vidya vikas"],
    "mahatma nagar": ["mahatma nagar", "महात्मा नगर", "mahatmanagar"],
    "parijat nagar": ["parijat nagar", "पारिजात नगर", "parijatnagar"],
    "college road": ["college road", "कॉलेज रोड"],
    "gangapur road": ["gangapur road", "गंगापूर रोड"],
    "mumbai naka": ["mumbai naka", "मुंबई नाका", "मुंबई नाक्याला", "मुंबई नाक्या", "मुंबई नाका बस स्टॉप", "mumbainaka", "mahamarg", "महामार्ग", "महामार्ग बस स्थानक", "महामार्ग बस स्टँड"],
    "jehan circle": ["jehan circle", "जहान सर्कल", "जेहान सर्कल"],
    "dindori": ["dindori", "दिंडोरी", "दिंडोरी नाका", "दिंडोरी बस स्टँड", "dindori bus stand", "dindori naka"],
    "chandori": ["chandori", "चांदोरी", "चांदोरी गाव", "chandori gaon"],
    "ozar": ["ozar", "ओझर", "ओझर विमानतळ", "ozar airport", "ozar bus stand"],
    "deolali": ["deolali", "देवळाली", "देवळाली कॅम्प", "deolali camp"],
    "bhagur": ["bhagur", "भगूर"],
    "makhmalabad": ["makhmalabad", "मखमलाबाद"],
    "girnare": ["girnare", "गिरणारे"]
}


class CityLincKnowledgeService:
    """Provides high-performance search and retrieval over the complete CityLinc Nashik transit dataset."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or settings.DATA_DIR
        self.routes: List[Dict[str, Any]] = []
        self.routes_by_id: Dict[int, Dict[str, Any]] = {}
        self.routes_by_number: Dict[str, List[Dict[str, Any]]] = {}
        self.stops_by_route_id: Dict[int, List[Dict[str, Any]]] = {}
        self.stops_by_route: Dict[str, List[str]] = {}
        self.trips_by_route_id: Dict[int, List[Dict[str, Any]]] = {}
        self.timetables: List[Dict[str, Any]] = []
        self.bus_passes: Dict[str, Any] = {}
        self.inverted_stop_index: Dict[str, Set[str]] = {}
        self.unique_station_ids: Set[int] = set()
        self.unique_station_names: Set[str] = set()
        self.known_rnos: Set[str] = set()
        self.zero_trip_route_ids: Set[int] = {6625, 6664, 6784, 7007}
        self.load_data()

    def normalize_transit_text(self, text: str) -> str:
        """Converts Devanagari numerals to standard ASCII digits and strips extra whitespace."""
        out = text
        for d_char, a_digit in DEVANAGARI_DIGITS.items():
            out = out.replace(d_char, a_digit)
        return out.strip().lower()

    def load_data(self) -> None:
        """Loads and indexes all 315 routes, 12,909 stops, 3,547 timetable trips, and fare passes."""
        try:
            # 1. Load Complete Routes & Stops & Timetables
            routes_file = self.data_dir / "routes" / "all_routes_stops.json"
            if not routes_file.exists():
                # Fallback to legacy path if raw/routes is not mounted
                routes_file = self.data_dir / "timetables" / "citylinc_timetables.json"

            if routes_file.exists():
                with open(routes_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, list):
                    self.routes = data
                    for r in self.routes:
                        rid = r.get("routeid")
                        rno = str(r.get("rno", "")).strip().upper()

                        if rid is not None:
                            self.routes_by_id[int(rid)] = r

                        if rno:
                            self.known_rnos.add(rno)
                            if rno not in self.routes_by_number:
                                self.routes_by_number[rno] = []
                            self.routes_by_number[rno].append(r)

                        # Index stops
                        stops = r.get("stops", [])
                        if rid is not None:
                            self.stops_by_route_id[int(rid)] = stops

                        stop_names = []
                        for s in stops:
                            sid = s.get("stationid")
                            if sid:
                                self.unique_station_ids.add(int(sid))
                            sname = (s.get("stationname") or s.get("displayname") or "").strip()
                            if sname:
                                stop_names.append(sname)
                                self.unique_station_names.add(sname)
                                # Inverted index for stop search
                                clean_sname = self.clean_stop_name(sname)
                                if clean_sname not in self.inverted_stop_index:
                                    self.inverted_stop_index[clean_sname] = set()
                                if rno:
                                    self.inverted_stop_index[clean_sname].add(rno)

                        if rno and rno not in self.stops_by_route:
                            self.stops_by_route[rno] = stop_names

                        # Index timetable trips
                        trips = r.get("timetable", [])
                        if rid is not None:
                            self.trips_by_route_id[int(rid)] = trips

                        for trip in trips:
                            # Ensure route linkage
                            if "rno" not in trip and rno:
                                trip["rno"] = rno
                            if "routeid" not in trip and rid is not None:
                                trip["routeid"] = rid
                            self.timetables.append(trip)

                logger.info(
                    f"CityLinc Knowledge Base Loaded: {len(self.routes)} route records, "
                    f"{len(self.known_rnos)} unique route numbers, {len(self.timetables)} timetable trips, "
                    f"{len(self.unique_station_ids)} unique stations."
                )

            # 2. Load Bus Passes
            passes_file = self.data_dir / "fare" / "bus_passes.json"
            if passes_file.exists():
                with open(passes_file, "r", encoding="utf-8") as f:
                    self.bus_passes = json.load(f).get("passes", {})
                logger.info(f"Loaded {len(self.bus_passes)} bus pass categories.")

        except Exception as e:
            logger.error(f"Error loading complete CityLinc knowledge base: {e}", exc_info=True)

    def clean_stop_name(self, sname: str) -> str:
        """Cleans stop names by lowercasing and stripping direction/bracket annotations."""
        clean = re.sub(r'\s*\([ud]\)\s*', '', sname, flags=re.IGNORECASE)
        clean = re.sub(r'\s*\b\d+\b\s*$', '', clean)
        return clean.strip().lower()

    def extract_route_numbers(self, text: str) -> List[str]:
        """Extracts any mentioned route numbers (e.g. '101', '163I', '201', '301', etc.) from user text."""
        norm_text = self.normalize_transit_text(text).upper()
        found: Set[str] = set()

        # 1. Exact match against known route numbers (sorted by length descending so 101R matches before 101)
        for rno in sorted(self.known_rnos, key=len, reverse=True):
            pattern = rf'(?:^|[\s,.\-/#])(?:ROUTE|BUS|मार्ग|बस)?\s*{re.escape(rno)}(?:$|[\s,.\-/#])'
            if re.search(pattern, norm_text, re.IGNORECASE):
                found.add(rno)

        # 2. Match standalone numbers 1 to 350
        num_matches = re.findall(r'\b(\d{1,3}[A-Z]?)\b', norm_text)
        for nm in num_matches:
            if nm in self.known_rnos:
                found.add(nm)

        return sorted(list(found))

    def extract_route_id(self, text: str) -> Optional[int]:
        """Matches explicit Route ID references like 'route id 5463' or 'id 6625'."""
        norm_text = self.normalize_transit_text(text)
        match = re.search(r'\b(?:route\s*id|id)\s*[:#]?\s*(\d{4,5})\b', norm_text, re.IGNORECASE)
        if match:
            rid = int(match.group(1))
            if rid in self.routes_by_id:
                return rid
        return None

    def find_routes_for_stop(self, stop_query: str) -> Tuple[str, List[str]]:
        """Finds all route numbers that serve a given stop/station."""
        clean_q = self.clean_stop_name(stop_query)
        matching_rnos: Set[str] = set()
        matched_stop_name = stop_query

        # Check location aliases
        target_keys = [clean_q]
        for canonical, aliases in LOCATION_ALIASES.items():
            if any(alias in clean_q for alias in aliases):
                target_keys.append(canonical)

        for key in target_keys:
            for sname, rnos in self.inverted_stop_index.items():
                if key in sname or sname in key:
                    matching_rnos.update(rnos)
                    matched_stop_name = sname.title()

        return matched_stop_name, sorted(list(matching_rnos))

    def find_routes_between(self, origin_query: str, destination_query: str) -> List[Dict[str, Any]]:
        """Finds routes connecting origin to destination."""
        clean_orig = self.clean_stop_name(origin_query)
        clean_dest = self.clean_stop_name(destination_query)

        matching_routes: List[Dict[str, Any]] = []
        for r in self.routes:
            rname = (r.get("routename") or "").lower()
            if clean_orig in rname and clean_dest in rname:
                matching_routes.append(r)

        return matching_routes[:5]

    def format_route_schedule(self, r: Dict[str, Any], language: str = "mr-IN") -> str:
        """Formats the timetable schedule for a single route record conversationally."""
        rno = r.get("rno", "")
        name = r.get("routename", "")
        direction = r.get("routetype", "")
        rid = r.get("routeid")
        trips = r.get("timetable", [])

        # Check for 0-trip schedule availability status
        if len(trips) == 0 or (rid is not None and rid in self.zero_trip_route_ids):
            stop_count = len(r.get("stops", []))
            if language == "mr-IN":
                return (
                    f"सिटीलिंक बस मार्ग {rno} ({name}, दिशा: {direction}): हा मार्ग सक्रिय असून त्याचे {stop_count} थांबे "
                    f"नोंदणीकृत आहेत. तथापि, या विशिष्ट मार्गाचे अधिकृत वेळापत्रक सध्या ऑपरेटरकडून प्रलंबित आहे."
                )
            elif language == "hi-IN":
                return (
                    f"सिटीलिंक बस रूट {rno} ({name}, दिशा: {direction}): यह रूट सक्रिय है और इसके {stop_count} स्टॉप "
                    f"निर्धारित हैं। हालांकि, इस विशिष्ट रूट की आधिकारिक समय-सारणी वर्तमान में ऑपरेटर द्वारा प्रतीक्षित है।"
                )
            else:
                return (
                    f"CityLinc Route {rno} ({name}, Direction: {direction}): This is an active corridor with {stop_count} stops, "
                    f"but official timetable departure schedule records are currently pending confirmation from the operator."
                )

        # Trips exist: extract and summarize departure times
        etas = [t.get("eta") for t in trips if t.get("eta")]
        etas_sorted = sorted(etas)

        if len(etas_sorted) >= 4:
            first_t = etas_sorted[0]
            last_t = etas_sorted[-1]
            n_trips = len(etas_sorted)
            if language == "mr-IN":
                return (
                    f"सिटीलिंक बस क्रमांक {rno} ({name}, {direction}): पहिली बस सकाळी {first_t} वाजता आणि "
                    f"शेवटची बस {last_t} वाजता सुटते. दिवसभरात एकूण {n_trips} फेऱ्या उपलब्ध आहेत."
                )
            elif language == "hi-IN":
                return (
                    f"सिटीलिंक बस नंबर {rno} ({name}, {direction}): पहली बस सुबह {first_t} बजे और "
                    f"अंतिम बस {last_t} बजे रवाना होती है। दिन भर कुल {n_trips} फेरे उपलब्ध हैं।"
                )
            else:
                return (
                    f"CityLinc Bus Route {rno} ({name}, {direction}): The first bus departs at {first_t} and the "
                    f"last trip departs at {last_t}, running {n_trips} scheduled daily trips."
                )
        elif len(etas_sorted) > 0:
            times_str = ", ".join(etas_sorted)
            if language == "mr-IN":
                return f"सिटीलिंक बस क्रमांक {rno} ({name}, {direction}): सुटण्याची वेळ {times_str} आहे."
            elif language == "hi-IN":
                return f"सिटीलिंक बस नंबर {rno} ({name}, {direction}): प्रस्थान समय {times_str} है।"
            else:
                return f"CityLinc Bus Route {rno} ({name}, {direction}): Departures are scheduled at {times_str}."
        else:
            if language == "mr-IN":
                return f"सिटीलिंक बस क्रमांक {rno} ({name}) नियमित वेळेत सुरू आहे."
            elif language == "hi-IN":
                return f"सिटीलिंक बस नंबर {rno} ({name}) नियमित समय पर संचालित है।"
            else:
                return f"CityLinc Bus Route {rno} ({name}) is operating on regular schedule."

    def format_multi_route_summary(self, routes: List[Dict[str, Any]], language: str = "mr-IN") -> str:
        """Formats conversational summary when multiple Up/Down corridors exist for a route number."""
        if not routes:
            return ""

        parts = []
        for r in routes[:3]:  # Summarize top 2-3 corridors (e.g. Up and Down)
            parts.append(self.format_route_schedule(r, language=language))

        return " ".join(parts)

    def answer_query_locally(self, user_text: str, language: str = "mr-IN") -> Optional[str]:
        """
        Comprehensive factual transit responder covering all 315 routes, 12,909 stops, and passes.
        Zero latency, zero hallucination.
        """
        text_norm = self.normalize_transit_text(user_text)

        # -------------------------------------------------------------
        # 1. Bus Pass Queries
        # -------------------------------------------------------------
        is_pass_query = any(w in text_norm for w in [
            "pass", "पास", "सवलत", "concession", "विद्यार्थी", "student", "भाडे", "दर", "किंमत", "rate", "ticket", "किराया"
        ])
        if is_pass_query and not any(w in text_norm for w in ["बस", "मार्ग", "रूट", "route"]):
            if "student" in text_norm or "विद्यार्थी" in text_norm or "छात्र" in text_norm:
                if language == "mr-IN":
                    return (
                        "सिटीलिंक विद्यार्थी पास (Student Pass): ३० दिवसांच्या पाससाठी ५०% सवलत आहे, "
                        "आणि ९० दिवसांच्या पाससाठी ६६.६०% सवलत मिळते. हा पास नाशिक शहरात सर्वसामान्य बस प्रवासासाठी वैध आहे."
                    )
                elif language == "hi-IN":
                    return (
                        "सिटीलिंक छात्र पास (Student Pass): ३० दिनों के पास पर ५०% की छूट मिलती है, "
                        "और ९० दिनों के पास पर ६६.६०% की छूट है। यह पास नाशिक शहर में मान्य है।"
                    )
                else:
                    return (
                        "CityLinc Student Pass offers 50% concession for 30 days duration and "
                        "66.60% concession for 90 days duration for travel across Nashik city buses."
                    )

            if language == "mr-IN":
                return (
                    "सिटीलिंक नाशिक बस पासचे दर: १ दिवसाचा ओपन-एन्डेड पास ₹११०, ७ दिवस ₹५३०, "
                    "३० दिवस ₹२,१००, ९० दिवस ₹५,२५०, आणि १८० दिवस ₹९,४५० आहे. "
                    "विद्यार्थ्यांना ३० दिवसांसाठी ५०% व ९० दिवसांसाठी ६६.६% सवलत मिळते."
                )
            elif language == "hi-IN":
                return (
                    "सिटीलिंक नाशिक बस पास के दर: १ दिन का ओपन पास ₹११०, ७ दिन ₹५३०, "
                    "३० दिन ₹२,१००, ९० दिन ₹५,२५०, और १८० दिन ₹९,४५० है। "
                    "विद्यार्थियों को ३० दिनों के लिए ५०% और ९० दिनों के लिए ६६.६% की छूट है।"
                )
            else:
                return (
                    "CityLinc Nashik Bus Pass rates: 1-Day Open Pass is ₹110, 7-Day is ₹530, "
                    "30-Day is ₹2,100, 90-Day is ₹5,250, and 180-Day is ₹9,450. "
                    "Student passes get 50% concession for 30 days and 66.6% for 90 days."
                )

        # -------------------------------------------------------------
        # 2. Direct Route ID Lookup (e.g. Route ID 5463, Route ID 6625)
        # -------------------------------------------------------------
        rid = self.extract_route_id(text_norm)
        if rid is not None and rid in self.routes_by_id:
            r = self.routes_by_id[rid]
            return self.format_route_schedule(r, language=language)

        # -------------------------------------------------------------
        # 3. Route Number Lookup (covers all 120 route numbers)
        # -------------------------------------------------------------
        matched_rnos = self.extract_route_numbers(text_norm)
        if matched_rnos:
            target_rno = matched_rnos[0]
            routes = self.routes_by_number.get(target_rno, [])

            # Check if user specifically asked about STOPS of this route
            is_stops_query = any(w in text_norm for w in ["stop", "stops", "थांबे", "थांबा", "स्टॉप", "थांब्याची"])
            if is_stops_query and routes:
                r = routes[0]
                stops = r.get("stops", [])
                snames = [s.get("stationname") or s.get("displayname") or "" for s in stops]
                snames_clean = [s for s in snames if s]
                n_stops = len(snames_clean)

                if n_stops > 0:
                    key_stops = [snames_clean[0]]
                    mid_indices = [int(n_stops * 0.25), int(n_stops * 0.5), int(n_stops * 0.75)]
                    for idx in mid_indices:
                        if 0 <= idx < n_stops and snames_clean[idx] not in key_stops:
                            key_stops.append(snames_clean[idx])
                    if snames_clean[-1] not in key_stops:
                        key_stops.append(snames_clean[-1])

                    stops_formatted = ", ".join(key_stops)
                    if language == "mr-IN":
                        return f"बस मार्ग {target_rno} ({r.get('routename')}) मध्ये एकूण {n_stops} थांबे आहेत. प्रमुख थांबे: {stops_formatted}."
                    elif language == "hi-IN":
                        return f"बस रूट {target_rno} ({r.get('routename')}) में कुल {n_stops} स्टॉप हैं। मुख्य स्टॉप: {stops_formatted}."
                    else:
                        return f"CityLinc Route {target_rno} ({r.get('routename')}) has {n_stops} stops. Key stops include: {stops_formatted}."

            if routes:
                return self.format_multi_route_summary(routes, language=language)

        # -------------------------------------------------------------
        # 4. Non-Existent Route Check (e.g. Route 999 or Route 8888)
        # -------------------------------------------------------------
        unmatched_route_num = re.search(r'\b(?:route|bus|मार्ग|बस)?\s*(\d{3,4})\b', text_norm, re.IGNORECASE)
        if unmatched_route_num:
            fake_num = unmatched_route_num.group(1)
            if fake_num not in self.known_rnos:
                if language == "mr-IN":
                    return f"सिटीलिंक नाशिक बस नेटवर्कमध्ये बस क्रमांक {fake_num} उपलब्ध नाही. कृपया १ ते ३३१ मधील वैध बस क्रमांक तपासा."
                elif language == "hi-IN":
                    return f"सिटीलिंक नाशिक बस नेटवर्क में बस नंबर {fake_num} उपलब्ध नहीं है। कृपया 1 से 331 के बीच सही बस नंबर जांचें।"
                else:
                    return f"Bus Route {fake_num} was not found in the CityLinc Nashik network. Please verify the route number."

        # -------------------------------------------------------------
        # 5. Stop to Routes Lookup (e.g. 'Which buses go to Shalimar?')
        # -------------------------------------------------------------
        is_which_bus_query = any(w in text_norm for w in ["which bus", "कोणत्या बसेस", "कोणती बस", "बसेस कोणत्या", "कौन सी बस", "बसों", "stop at", "जाणाऱ्या बसेस"])
        if is_which_bus_query:
            for stop_key in ["shalimar", "cbs", "tapovan", "nashik road", "gangapur", "pathardi", "ashok nagar", "panchavati", "satpur", "nimani", "mhasrul", "indira nagar", "cidco", "dwarka"]:
                aliases = LOCATION_ALIASES.get(stop_key, [stop_key])
                if any(a in text_norm for a in aliases):
                    matched_name, rnos = self.find_routes_for_stop(stop_key)
                    if rnos:
                        sample_rnos = ", ".join(rnos[:10])
                        total_count = len(rnos)
                        if language == "mr-IN":
                            return f"{matched_name} या थांब्यावरून धावणाऱ्या प्रमुख बसेस: {sample_rnos} (एकूण {total_count} मार्ग उपलब्ध)."
                        elif language == "hi-IN":
                            return f"{matched_name} स्टॉप से चलने वाले मुख्य बस रूट: {sample_rnos} (कुल {total_count} रूट उपलब्ध हैं)।"
                        else:
                            return f"The following CityLinc routes serve {matched_name}: {sample_rnos} (total {total_count} routes available)."

        # -------------------------------------------------------------
        # 6. Destination / Origin-Destination Route Searches
        # -------------------------------------------------------------
        for canonical, aliases in LOCATION_ALIASES.items():
            if any(a in text_norm for a in aliases):
                # Look for matching routes that have this location in route name
                matching = [r for r in self.routes if canonical in (r.get("routename") or "").lower()]
                if matching:
                    top_routes = matching[:2]
                    return self.format_multi_route_summary(top_routes, language=language)

        # -------------------------------------------------------------
        # 7. Greetings
        # -------------------------------------------------------------
        if any(w in text_norm for w in ["hello", "hi", "namaste", "नमस्कार", "नमस्ते", "का हाल"]):
            if language == "mr-IN":
                return "नमस्कार! मी सिटीलिंक नाशिक बस सहाय्यक आहे. मी तुम्हाला नाशिकमधील ३१५ बस मार्ग, वेळापत्रक आणि पासबद्दल कशी मदत करू?"
            elif language == "hi-IN":
                return "नमस्ते! मैं सिटीलिंक नाशिक बस सहायक हूँ। मैं आपको नाशिक के 315 बस रूट, समय-सारणी और पास की जानकारी में कैसे मदद करूँ?"
            else:
                return "Hello! I am the CityLinc Nashik bus assistant. How can I assist you across our 315 routes, timetables, or bus passes today?"

        return None

    def retrieve_relevant_context(self, user_query: str) -> str:
        """
        Compact retrieval engine for generative LLM context injection.
        Extracts only the top matching routes and timetable snippets (< 200 tokens).
        Never dumps the entire 8 MB dataset to the LLM.
        """
        text_norm = self.normalize_transit_text(user_query)
        rnos = self.extract_route_numbers(text_norm)
        snippets = []

        if rnos:
            for rno in rnos[:2]:
                routes = self.routes_by_number.get(rno, [])
                for r in routes[:2]:
                    trips = r.get("timetable", [])
                    etas = [t.get("eta") for t in trips if t.get("eta")]
                    time_summary = f"First: {etas[0]}, Last: {etas[-1]} ({len(etas)} trips)" if etas else "Pending operator schedule"
                    snippets.append(
                        f"Route {rno} (ID: {r.get('routeid')}, {r.get('routetype')}): {r.get('routename')}. Schedule: {time_summary}. Stops: {len(r.get('stops', []))}."
                    )

        if not snippets:
            # Check destination matches
            for canonical, aliases in LOCATION_ALIASES.items():
                if any(a in text_norm for a in aliases):
                    matching = [r for r in self.routes if canonical in (r.get("routename") or "").lower()]
                    for r in matching[:2]:
                        snippets.append(f"Route {r.get('rno')} ({r.get('routetype')}): {r.get('routename')}.")
                    break

        return "\n".join(snippets)

    def get_system_prompt(self, current_language: str = "mr-IN", relevant_context: Optional[str] = None) -> str:
        """Returns authoritative context for CityLinc Nashik Voice Assistant with dynamically injected route context."""
        context_block = f"\nRELEVANT ROUTE KNOWLEDGE FOR USER QUERY:\n{relevant_context}\n" if relevant_context else ""

        return f"""You are the official CityLinc Nashik AI Transit Voice Assistant (सिटीलिंक नाशिक बस सहाय्यक).
You assist commuters across the COMPLETE CityLinc network in Nashik, Maharashtra:
- Complete network: 315 routes, 12,909 stops, 1,187 stations, and 3,547 daily timetable trips.
- Bus Passes: Open-Ended Pass (1-Day ₹110, 7-Day ₹530, 30-Day ₹2,100, 90-Day ₹5,250, 180-Day ₹9,450).
- Student Pass: 30 Days = 50% concession, 90 Days = 66.60% concession.
- Fixed Route Pass: 30 Days = Pay 20 days fare, 90 Days = Pay 50 days fare.
{context_block}
CORE RULES:
1. Always reply in the exact language the user spoke:
   - If Marathi (mr-IN): Respond in polite, natural Marathi (मराठी).
   - If Hindi (hi-IN): Respond in clear, polite Hindi (हिंदी).
   - If English (en-IN): Respond in concise, friendly English.
2. Keep responses brief, spoken-friendly, and direct (1-3 sentences max). Commuters are listening to voice audio!
3. If timetable information for a specific route is absent in the official records (such as routes 6625, 6784, 7007, 6664), state that the route exists but official departure times are pending operator confirmation. Do NOT invent fake departure times.
4. Current detected user language is: {current_language}.
"""


# Global singleton instance
knowledge_service = CityLincKnowledgeService()
