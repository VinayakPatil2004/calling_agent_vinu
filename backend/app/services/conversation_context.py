"""
CityLinc Conversation Context Engine.
Tracks multi-turn conversational session state, user intent, resolved origins/destinations,
current user location, selected routes, timetable results, and handles natural follow-up queries.

Phase 4.1 Upgrades:
- Distinguishes Marathi 'पासून'/'पासुन' (from) from standalone 'पास' (bus pass).
- English syntax support for 'reach [Dest] from [Orig]', 'get to [Dest] from [Orig]', etc.
- Four distinct hospital intents: HOSPITAL_COUNT, NEARBY_HOSPITAL, HOSPITAL_NAVIGATION, HOSPITAL_DISTANCE.
- Multi-turn current location tracking ('मी KBT Circle जवळ आहे', 'I am at CBS').
- Follow-up query resolution reusing structured context.
- Strict session isolation and graceful error recovery.
"""
import re
import time
import logging
from typing import Dict, List, Optional, Any, Tuple
from pydantic import BaseModel, Field

from backend.app.services.route_engine import route_engine, JourneySearchResult, DirectRouteOption
from backend.app.services.timetable_engine import timetable_engine, TimetableResponse
from backend.app.services.location_service import location_service
from backend.app.services.citylinc_knowledge import knowledge_service, LOCATION_ALIASES, DEVANAGARI_DIGITS
from backend.app.services.number_formatter import format_text_for_speech
from backend.app.services.fare_service import fare_service

logger = logging.getLogger("citylinc.context")

# Explicit conversation termination phrases
TERMINATION_PATTERNS = [
    r'\b(?:goodbye|bye|bye\s*bye|see\s*you|exit|quit|stop)\b',
    r'\b(?:बाय|बाय\s*बाय|अलविदा|टाटा|थोड्या वेळाने बोलू)\b',
    r'मला\s*(?:आता|सध्या)?\s*(?:मदत|काही)?\s*नको',
    r'आता\s*नको',
    r'बस\s*आता\s*काही\s*नको',
    r'मुझे\s*(?:अब|और)?\s*मदद\s*नहीं\s*चाहिए',
    r'बस\s*इतना\s*ही',
    r'बस\s*(?:अब\s*)?बंद\s*(?:कर\s*देंगे|कर\s*दो|करो|कीजिए|करना\s*है)',
    r'\b(?:बंद\s*कर\s*देंगे|बंद\s*कर\s*दो|बंद\s*करो|कॉल\s*काट\s*दो|कॉल\s*रख\s*दो)\b',
    r'\b(?:कॉल\s*बंद\s*करा|कॉल\s*कट\s*करा|फोन\s*ठेवा|थांबवा|बस\s*बंद\s*करा)\b'
]

# Polite acknowledgement phrases (MUST NOT terminate session)
POLITE_THANKS_PATTERNS = [
    r'\b(?:thank\s*you|thanks|thankyou|thx)\b',
    r'\b(?:धन्यवाद|आभार|थँक्यू|थॅंक\s*यु|शुक्रिया|मेहरबानी)\b'
]


class SessionTransitContext(BaseModel):
    session_id: str
    language: str = "mr-IN"
    origin: Optional[str] = None
    destination: Optional[str] = None
    origin_display: Optional[str] = None
    destination_display: Optional[str] = None
    current_location: Optional[str] = None
    current_location_display: Optional[str] = None
    current_location_station_ids: List[int] = []
    current_location_lat: Optional[float] = None
    current_location_lon: Optional[float] = None
    selected_route_id: Optional[int] = None
    selected_rno: Optional[str] = None
    selected_route_name: Optional[str] = None
    selected_hospital_id: Optional[str] = None
    selected_hospital_name: Optional[str] = None
    boarding_stop_id: Optional[int] = None
    boarding_stop_name: Optional[str] = None
    alighting_stop_id: Optional[int] = None
    alighting_stop_name: Optional[str] = None
    last_direct_routes: List[Dict[str, Any]] = []
    last_transfer_routes: List[Dict[str, Any]] = []
    last_fare_estimate: Optional[Dict[str, Any]] = None
    last_timetable_response: Optional[Dict[str, Any]] = None
    pronounce_numbers_in_words: bool = True
    pronounce_numbers_lang: Optional[str] = None
    last_intent: str = "general"
    last_relevant_intent: str = "general"
    pending_clarification: Optional[str] = None
    is_terminated: bool = False
    turn_count: int = 0
    created_at: float = Field(default_factory=time.time)
    last_active: float = Field(default_factory=time.time)


class ContextEngine:
    """Manages multi-turn transit dialog context across active sessions."""

    def __init__(self):
        self.sessions: Dict[str, SessionTransitContext] = {}

    def get_or_create_context(self, session_id: str, language: str = "mr-IN") -> SessionTransitContext:
        """Retrieves or creates context state for a session."""
        norm_lang = "mr-IN"
        if language:
            if language.startswith("mr"):
                norm_lang = "mr-IN"
            elif language.startswith("hi"):
                norm_lang = "hi-IN"
            elif language.startswith("en"):
                norm_lang = "en-IN"

        if session_id not in self.sessions:
            self.sessions[session_id] = SessionTransitContext(session_id=session_id, language=norm_lang)
            logger.info(f"Initialized new transit context for session: {session_id}")
        ctx = self.sessions[session_id]
        ctx.last_active = time.time()
        ctx.language = norm_lang
        return ctx

    def get_context(self, session_id: str) -> Optional[SessionTransitContext]:
        """Retrieves active session context without creating one if it does not exist."""
        return self.sessions.get(session_id)

    def clear_context(self, session_id: str):
        """Cleans up session context upon connection close."""
        if session_id in self.sessions:
            del self.sessions[session_id]

    def _response(
        self,
        msg: str,
        intent: str,
        map_action: Optional[Dict[str, Any]] = None,
        is_terminated: bool = False,
        lang: str = "mr-IN"
    ) -> Dict[str, Any]:
        """Wraps deterministic turn response with natural spoken phonetic text for TTS."""
        return {
            "handled_deterministically": True,
            "response_text": msg,
            "speech_text": format_text_for_speech(msg, lang),
            "intent": intent,
            "map_action": map_action,
            "is_terminated": is_terminated
        }

    def check_termination(self, text: str) -> bool:
        """Checks if user explicitly requested to end the conversation."""
        norm = text.lower().strip()
        return any(re.search(pat, norm, re.IGNORECASE) for pat in TERMINATION_PATTERNS)

    def check_polite_thanks(self, text: str) -> bool:
        """Checks if user expressed polite thanks without ending conversation."""
        norm = text.lower().strip()
        if self.check_termination(text):
            return False
        return any(re.search(pat, norm, re.IGNORECASE) for pat in POLITE_THANKS_PATTERNS)

    def detect_intent(self, text: str, ctx: SessionTransitContext) -> str:
        """
        Classifies incoming user intent into:
        - 'termination': user explicitly requested to end
        - 'thanks': user thanked assistant
        - 'location_declaration': user states current location ('मी KBT Circle जवळ आहे')
        - 'hospital_count': total hospitals query
        - 'nearby_hospital': nearest hospital query
        - 'hospital_distance': hospital walking/spatial distance query
        - 'hospital_navigation': hospital directions / navigation query
        - 'route_search': origin to destination pathfinding
        - 'next_bus': next bus query
        - 'timetable': timetable / departures query
        - 'fare': ticket price / bus pass query
        - 'stops': query about stops of a route
        - 'greeting': greetings
        - 'general': general query
        """
        norm = text.lower().strip()
        for d_char, a_digit in DEVANAGARI_DIGITS.items():
            norm = norm.replace(d_char, a_digit)

        # 1. Termination
        if self.check_termination(norm):
            return "termination"

        # 2. Polite thanks
        if self.check_polite_thanks(norm):
            return "thanks"

        # 2B. Number Pronunciation Preference (e.g. 'आकडे मराठीत बोल', 'संख्या मराठीत सांगा', 'speak numbers in english')
        num_pronounce_patterns = [
            r'(?:आकडे|संख्या|अंक|नंबर|numbers?|digits?)\s+(?:मराठीत?|हिंदीत?|इंग्रजीत?|marathi|hindi|english|मराठी|हिंदी|इंग्लिश|इंग्रजी)',
            r'(?:मराठीत?|हिंदीत?|इंग्रजीत?|marathi|hindi|english)\s+(?:आकडे|संख्या|अंक|नंबर|numbers?|digits?)',
            r'(?:speak|tell|say|pronounce)\s+(?:numbers?|digits?)\s+(?:in\s+)?(?:marathi|hindi|english)',
            r'(?:speak|tell|say)\s+in\s+(?:marathi|hindi|english)\s+(?:numbers?|digits?)',
            r'\b(?:आकडे|संख्या|अंक|नंबर)\s*(?:मराठीत|हिंदीत|इंग्रजीत|मराठी\s*मध्ये|हिंदी\s*मध्ये|इंग्रजी\s*मध्ये)\b'
        ]
        if any(re.search(pat, norm, re.IGNORECASE) for pat in num_pronounce_patterns):
            return "number_pronunciation_preference"

        # 3. Location Declaration (e.g., 'मी KBT Circle जवळ आहे', 'I am at CBS', 'मैं केबीटी सर्कल पर हूँ')
        loc_patterns = [
            r'(?:मी|आम्ही)\s+([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:जवळ|येथे|वर|पाशी|मध्ये)\s*आहे',
            r'i(?:\x27m|m|\s+am)\s+(?:at|near|around|in)\s+([a-zA-Z0-9\s]+)',
            r'(?:मैं|हम)\s+([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:के पास|पर|में)\s*हूँ'
        ]
        if any(re.search(pat, norm, re.IGNORECASE) for pat in loc_patterns):
            return "location_declaration"

        # 4. Hospital Intents (Disambiguated into 4 distinct types)
        hosp_keywords = [
            "hospital", "hospitals", "रुग्णालय", "रुग्णालये", "दवाखाना", "हॉस्पिटल", "clinic", "dispensary",
            "सिव्हिल", "civil", "esi", "बायको", "bytco", "namco", "नामको", "अपोलो", "apollo",
            "wockhardt", "व्हॉकहार्ट", "ashoka", "अशोका", "sushrut", "सुश्रुत", "धोंडी", "dhariwal", "nagji", "नागजी"
        ]
        is_hosp_term = any(w in norm for w in hosp_keywords)

        if is_hosp_term:
            # 4A. Hospital Distance (Check distance FIRST so 'किती लांब' / 'how far' matches distance, not count)
            if any(w in norm for w in ["लांब", "अंतर", "distance", "how far", "दूर", "दूरी"]):
                return "hospital_distance"

            # 4B. Nearby Hospital
            if any(w in norm for w in ["जवळचे", "जवळचा", "जवळचं", "जवळ", "nearby", "nearest", "closest", "नज़दीकी", "पास का", "पास में"]):
                return "nearby_hospital"

            # 4C. Hospital Count
            if any(w in norm for w in ["किती", "how many", "total", "संख्या", "count", "number of", "कितने", "टोटल", "सगळे", "एकूण"]):
                return "hospital_count"

            # 4D. Hospital Navigation
            return "hospital_navigation"

        # Follow-up hospital navigation when a hospital is already in context
        if ctx.selected_hospital_id and any(w in norm for w in [
            "कसं जाऊ", "तिथे कसं", "जाण्यासाठी", "मार्ग काय", "how to go", "how to reach", "directions", "जाना है"
        ]):
            return "hospital_navigation"

        # 4E. Route Confirmation Query (e.g. 'ही बस केबीटी सर्कलवरून पण जाते ना?', 'Does this bus go through KBT Circle?', 'Kya ye bus KBT Circle se jaati hai?')
        conf_patterns = [
            r'\b(?:does\s+(?:this|the)\s+bus|will\s+this\s+bus|is\s+this\s+bus)\s+(?:also\s+)?(?:go\s+through|pass\s+through|stop\s+at|serve|go\s+to|reach|cover)\b',
            r'(?:ही\s+बस|ही\s+गाडी|हा\s+मार्ग|या\s+बसने)\s+.+?\s*(?:वरून|हून|मधून|ला|कडे|तिथून)?\s*(?:पण\s+)?(?:जाते\s+का|जाते\s+ना|जाईल\s+का|थांबते\s+का|थांबते\s+ना|येते\s+का|जाणार\s+का)',
            r'.+?\s*(?:वरून|हून|मधून)\s*(?:पण\s+)?(?:जाते\s+का|जाते\s+ना|जाईल\s+का|थांबते\s+का|थांबते\s+ना)',
            r'(?:क्या\s+यह\s+बस|क्या\s+ये\s+बस|ये\s+बस|यह\s+बस)\s+.+?\s*(?:से|पर|तक)?\s*(?:भी\s+)?(?:होकर\s+)?(?:जाती\s+है\s+क्या|जाती\s+है\s+ना|रुकती\s+है\s+क्या|रुकती\s+है\s+ना|जाएगी\s+क्या)',
            r'\b(?:ye\s+bus|yeh\s+bus|hi\s+bus)\s+.+?\s*(?:se|varun|houn|la)?\s*(?:pan\s+)?(?:jaati\s+hai|jaate\s+ka|jaate\s+na|jaayegi\s+kya|rukegi\s+kya)\b'
        ]
        if any(re.search(pat, norm, re.IGNORECASE) for pat in conf_patterns):
            return "route_confirmation"

        # 5. Route Search Check (checks if text contains origin-destination pair or single destination)
        parsed_od = self._extract_origin_destination(norm)
        if parsed_od is not None:
            # If the user specifically asked for FARE between this origin and destination
            if any(w in norm for w in ["भाडं", "भाडे", "तिकीट", "तिकिट", "किराया", "टिकट", "fare", "ticket", "cost", "price"]):
                return "fare"
            return "route_search"

        if self._extract_single_destination(norm) is not None:
            return "route_search"

        # 6. Next Bus Query
        if any(w in norm for w in [
            "next bus", "पुढची बस", "पुढील बस", "अगली बस", "next", "किती वाजता", "कधी आहे", "कब है", "when is the next", "टाइम काय", "timing"
        ]):
            return "next_bus"

        # 7. Timetable / Schedule Query
        if any(w in norm for w in [
            "वेळापत्रक", "वेळ", "टाइमटेबल", "timetable", "schedule", "पहिली बस", "शेवटची बस", "सकाळची बस", "संध्याकाळची बस", "first bus", "last bus"
        ]):
            return "timetable"

        # 8. Fare / Bus Pass Query (Strict Unicode-aware matching preventing Marathi पासून / पासुन collision)
        is_pass_phrase = any(w in norm for w in [
            "bus pass", "student pass", "monthly pass", "daily pass", "pass rate", "pass price", "pass cost",
            "बस पास", "विद्यार्थी पास", "मासिक पास", "दैनिक पास", "पास दर", "पासचे दर", "पास बद्दल", "पास पाहिजे",
            "पास हवा", "पास मिळेल का", "पास कसा काढायचा"
        ])
        t_no_from = re.sub(r'पा(?:सू|सु)न[ा-्]*', '', norm)
        tokens = set(re.findall(r'[a-zA-Z0-9\u0900-\u097F]+', t_no_from))
        has_pass_token = "पास" in tokens or "pass" in tokens
        has_fare_token = any(tok in tokens for tok in ["भाडं", "भाडे", "तिकीट", "तिकिट", "किराया", "टिकट", "fare", "ticket", "cost", "price"])

        if is_pass_phrase or has_pass_token or has_fare_token:
            return "fare"

        # 9. Stops of a route
        if any(w in norm for w in ["थांबे", "थांबा", "स्टॉप", "stops"]):
            return "stops"

        # 10. Greetings
        if any(re.search(rf'\b{re.escape(w)}\b', norm, re.IGNORECASE) for w in ["hello", "hi", "namaste", "नमस्कार", "नमस्ते", "का हाल"]):
            return "greeting"

        # 11. Route Search Fallback when place aliases are combined with travel prepositions
        if any(w in norm for w in ["वरून", "ते", "हून", "कडून", "to", "from", "से", "तक", "ला जायचं", "जाणे"]):
            return "route_search"

        return "general"

    def _handle_route_search(
        self,
        ctx: SessionTransitContext,
        orig_raw: str,
        dest_raw: str,
        lang: str = "mr-IN",
        user_text: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes complete Origin-Destination transit pathfinding, updates session context,
        and returns structured response with all eligible routes.
        """
        # Reset previous active journey state before evaluating new query
        ctx.origin = orig_raw
        ctx.destination = dest_raw
        ctx.origin_display = None
        ctx.destination_display = None
        ctx.selected_route_id = None
        ctx.selected_rno = None
        ctx.selected_route_name = None
        ctx.boarding_stop_id = None
        ctx.boarding_stop_name = None
        ctx.alighting_stop_id = None
        ctx.alighting_stop_name = None
        ctx.last_direct_routes = []
        ctx.last_transfer_routes = []
        ctx.last_fare_estimate = None
        ctx.last_timetable_response = None

        # Extract target departure time if user specified time in text
        dep_time = None
        if user_text:
            from backend.app.services.route_engine import parse_natural_departure_time
            from backend.app.services.transit_graph import format_minutes_to_time
            dep_m = parse_natural_departure_time(user_text)
            if dep_m is not None:
                dep_time = format_minutes_to_time(dep_m)

        # Complete Route Discovery (direct, 1-transfer, 2-transfer)
        search_res = route_engine.search_routes(orig_raw, dest_raw, departure_time=dep_time, limit_direct=None)

        if search_res.success and search_res.direct_routes:
            top_r = search_res.direct_routes[0]
            # Update Context
            ctx.origin_display = search_res.origin.display_name
            ctx.destination_display = search_res.destination.display_name
            ctx.selected_route_id = top_r.route_id
            ctx.selected_rno = top_r.rno
            ctx.selected_route_name = top_r.route_name
            ctx.boarding_stop_id = top_r.boarding_stop.station_id
            ctx.boarding_stop_name = top_r.boarding_stop.name
            ctx.alighting_stop_id = top_r.alighting_stop.station_id
            ctx.alighting_stop_name = top_r.alighting_stop.name
            # Keep ALL direct routes in context and map payload
            ctx.last_direct_routes = [r.model_dump() for r in search_res.direct_routes]
            ctx.last_fare_estimate = search_res.fare_estimate

            # P1.1 Layer 2: Voice Presentation
            all_rnos = [r.rno for r in search_res.direct_routes]
            top_rnos = all_rnos[:3]
            rnos_spoken = ", ".join(top_rnos)
            rem = len(all_rnos) - len(top_rnos)
            more_mr = f" (आणि इतर {rem} पर्याय उपलब्ध आहेत)" if rem > 0 else ""
            more_hi = f" (और अन्य {rem} विकल्प उपलब्ध हैं)" if rem > 0 else ""
            more_en = f" (and {rem} more options available)" if rem > 0 else ""

            b_stop = top_r.boarding_stop.name
            a_stop = top_r.alighting_stop.name
            n_stops = top_r.intermediate_stops_count
            dist = top_r.distance_km
            fare_breakdown = search_res.fare_estimate.get("fare_breakdown") if search_res.fare_estimate else None
            fare_mr = fare_service.format_fare_message(fare_breakdown, lang="mr-IN") if fare_breakdown else f"भाडे ₹{search_res.fare_estimate.get('single_fare_inr', 20)}"
            fare_hi = fare_service.format_fare_message(fare_breakdown, lang="hi-IN") if fare_breakdown else f"किराया ₹{search_res.fare_estimate.get('single_fare_inr', 20)}"
            fare_en = fare_service.format_fare_message(fare_breakdown, lang="en-IN") if fare_breakdown else f"fare ₹{search_res.fare_estimate.get('single_fare_inr', 20)}"

            time_est_mr = " (हा थांब्यावरील वेळ अंदाजित आहे; अधिकृत थांबा-स्तरीय वेळ उपलब्ध नाही.)"
            time_est_hi = " (यह समय अनुमानित है; इस स्टॉप का आधिकारिक समय उपलब्ध नहीं है।)"
            time_est_en = " (This is an estimated stop arrival time, not an official scheduled stop time.)"

            msg = {
                "mr-IN": f"{ctx.origin_display} ते {ctx.destination_display} दरम्यान थेट बस मार्ग: {rnos_spoken}{more_mr}. प्रमुख मार्ग {top_r.rno} ({top_r.route_name}) ने '{b_stop}' वरून चढावे आणि '{a_stop}' वर उतरावे ({n_stops} थांबे, {dist} किमी, {fare_mr}).{time_est_mr}",
                "hi-IN": f"{ctx.origin_display} से {ctx.destination_display} के लिए सीधी बसें: {rnos_spoken}{more_hi}। मुख्य रूट {top_r.rno} ({top_r.route_name}) से '{b_stop}' से बैठें और '{a_stop}' पर उतरें ({n_stops} स्टॉप, {dist} किमी, {fare_hi})।{time_est_hi}",
                "en-IN": f"Direct CityLinc routes between {ctx.origin_display} and {ctx.destination_display}: Routes {rnos_spoken}{more_en}. Take Route {top_r.rno} ({top_r.route_name}) boarding at '{b_stop}' and alighting at '{a_stop}' ({n_stops} stops, {dist} km, {fare_en}).{time_est_en}"
            }.get(lang, f"Take Route {top_r.rno} from {b_stop} to {a_stop}.")

            map_action = {
                "action": "highlight_journey",
                "route_id": top_r.route_id,
                "rno": top_r.rno,
                "route_name": top_r.route_name,
                "origin": {
                    "name": top_r.boarding_stop.name,
                    "lat": top_r.boarding_stop.lat,
                    "lon": top_r.boarding_stop.lon
                },
                "destination": {
                    "name": top_r.alighting_stop.name,
                    "lat": top_r.alighting_stop.lat,
                    "lon": top_r.alighting_stop.lon
                },
                "boarding_stop": top_r.boarding_stop.model_dump(),
                "alighting_stop": top_r.alighting_stop.model_dump(),
                "intermediate_stops": [s.model_dump() for s in top_r.intermediate_stops],
                "polyline": top_r.polyline,
                "direct_routes": [r.model_dump() for r in search_res.direct_routes],
                "all_rnos": all_rnos
            }

            return self._response(msg, "route_search", map_action=map_action, lang=lang)

        elif search_res.success and search_res.transfer_routes:
            tr = search_res.transfer_routes[0]
            ctx.origin_display = search_res.origin.display_name
            ctx.destination_display = search_res.destination.display_name
            ctx.selected_route_id = tr.leg1.route_id
            ctx.selected_rno = tr.leg1.rno
            ctx.selected_route_name = tr.leg1.route_name
            ctx.boarding_stop_id = tr.leg1.boarding_stop.station_id
            ctx.boarding_stop_name = tr.leg1.boarding_stop.name
            ctx.alighting_stop_id = tr.leg2.alighting_stop.station_id
            ctx.alighting_stop_name = tr.leg2.alighting_stop.name
            ctx.last_transfer_routes = [t.model_dump() for t in search_res.transfer_routes]
            ctx.last_fare_estimate = search_res.fare_estimate

            if not tr.is_timetable_feasible:
                msg = {
                    "mr-IN": f"{search_res.origin.display_name} ते {search_res.destination.display_name} साठी {tr.interchange_hub_mr} येथे जोडणी मार्ग (मार्ग {tr.leg1.rno} -> {tr.leg2.rno}) आहे, परंतु निवडलेल्या वेळेनुसार जोडणी बस वेळापत्रकात उपलब्ध नाही.",
                    "hi-IN": f"{search_res.origin.display_name} से {search_res.destination.display_name} के लिए {tr.interchange_hub} पर कनेक्टिंग रूट (रूट {tr.leg1.rno} -> {tr.leg2.rno}) है, लेकिन चयनित समय पर कनेक्टिंग बस उपलब्ध नहीं है।",
                    "en-IN": f"Transfer available at {tr.interchange_hub} (Route {tr.leg1.rno} -> {tr.leg2.rno}), but connection is not viable at the requested departure time due to timetable schedule."
                }.get(lang, "Transfer infeasible at selected time.")
                return self._response(msg, "route_search", lang=lang)

            time_est_mr = " (हा थांब्यावरील वेळ अंदाजित आहे; अधिकृत थांबा-स्तरीय वेळ उपलब्ध नाही.)"
            time_est_hi = " (यह समय अनुमानित है; इस स्टॉप का आधिकारिक समय उपलब्ध नहीं है।)"
            time_est_en = " (This is an estimated stop arrival time, not an official scheduled stop time.)"

            fare_breakdown = search_res.fare_estimate.get("fare_breakdown") if search_res.fare_estimate else None
            fare_mr = f" ({fare_service.format_fare_message(fare_breakdown, lang='mr-IN')})" if fare_breakdown else ""
            fare_hi = f" ({fare_service.format_fare_message(fare_breakdown, lang='hi-IN')})" if fare_breakdown else ""
            fare_en = f" ({fare_service.format_fare_message(fare_breakdown, lang='en-IN')})" if fare_breakdown else ""

            wait_str = f" (बस बदलण्यासाठी प्रतीक्षा: {tr.wait_time_mins} मिनिटे)" if tr.wait_time_mins else ""
            msg = {
                "mr-IN": f"{search_res.origin.display_name} ते {search_res.destination.display_name} थेट बस नाही. तुम्ही {tr.interchange_hub_mr} येथे बस बदलू शकता: प्रथम मार्ग {tr.leg1.rno} ने '{tr.transfer_stop_name}' ला जा, व तेथून मार्ग {tr.leg2.rno} ने '{tr.leg2.alighting_stop.name}' ला पोहोचा{wait_str}{fare_mr}.{time_est_mr}",
                "hi-IN": f"{search_res.origin.display_name} से {search_res.destination.display_name} के लिए सीधी बस नहीं है। आप {tr.interchange_hub} पर बस बदल सकते हैं: पहले रूट {tr.leg1.rno} से '{tr.transfer_stop_name}' जाएं, फिर रूट {tr.leg2.rno} लें{fare_hi}।{time_est_hi}",
                "en-IN": f"No direct route between {search_res.origin.display_name} and {search_res.destination.display_name}. Transfer at {tr.interchange_hub}: take Route {tr.leg1.rno} to '{tr.transfer_stop_name}', then transfer to Route {tr.leg2.rno}{fare_en}.{time_est_en}"
            }.get(lang, "Transfer route available.")

            return self._response(msg, "route_search", lang=lang)

        elif search_res.success and search_res.multi_hop_journeys:
            pj = search_res.multi_hop_journeys[0]
            ctx.origin_display = search_res.origin.display_name
            ctx.destination_display = search_res.destination.display_name
            ctx.selected_route_id = pj.legs[0].route_id
            ctx.selected_rno = pj.legs[0].rno
            ctx.selected_route_name = pj.legs[0].route_name
            ctx.boarding_stop_id = pj.legs[0].boarding_stop.station_id
            ctx.boarding_stop_name = pj.legs[0].boarding_stop.name
            ctx.alighting_stop_id = pj.legs[-1].alighting_stop.station_id
            ctx.alighting_stop_name = pj.legs[-1].alighting_stop.name
            ctx.last_fare_estimate = search_res.fare_estimate

            time_est_mr = " (हा थांब्यावरील वेळ अंदाजित आहे; अधिकृत थांबा-स्तरीय वेळ उपलब्ध नाही.)"
            time_est_hi = " (यह समय अनुमानित है; इस स्टॉप का आधिकारिक समय उपलब्ध नहीं है।)"
            time_est_en = " (This is an estimated stop arrival time, not an official scheduled stop time.)"

            fare_breakdown = search_res.fare_estimate.get("fare_breakdown") if search_res.fare_estimate else None
            fare_mr = fare_service.format_fare_message(fare_breakdown, lang="mr-IN") if fare_breakdown else f"भाडे ₹{pj.total_fare_inr}"
            fare_hi = fare_service.format_fare_message(fare_breakdown, lang="hi-IN") if fare_breakdown else f"किराया ₹{pj.total_fare_inr}"
            fare_en = fare_service.format_fare_message(fare_breakdown, lang="en-IN") if fare_breakdown else f"fare ₹{pj.total_fare_inr}"

            if pj.transfers_count == 2 and len(pj.legs) >= 3:
                l1, l2, l3 = pj.legs[0], pj.legs[1], pj.legs[2]
                t1, t2 = pj.transfers[0], pj.transfers[1]
                msg = {
                    "mr-IN": f"{search_res.origin.display_name} ते {search_res.destination.display_name} दरम्यान २ बस बदलाव्या लागतील: प्रथम मार्ग {l1.rno} ने '{t1.interchange_hub_mr}' येथे जा, तेथून मार्ग {l2.rno} ने '{t2.interchange_hub_mr}' येथे जा, आणि अंतिम मार्ग {l3.rno} ने '{l3.alighting_stop.name}' ला पोहोचा (एकूण वेळ अंदाजे {pj.total_duration_mins} मिनिटे, {fare_mr}).{time_est_mr}",
                    "hi-IN": f"{search_res.origin.display_name} से {search_res.destination.display_name} के लिए 2 बसें बदलनी होंगी: पहले रूट {l1.rno} से '{t1.interchange_hub}' जाएं, फिर रूट {l2.rno} से '{t2.interchange_hub}' जाएं, और अंतिम रूट {l3.rno} से पहुंचे (कुल समय {pj.total_duration_mins} मिनट, {fare_hi})।{time_est_hi}",
                    "en-IN": f"Journey between {search_res.origin.display_name} and {search_res.destination.display_name} requires 2 transfers: take Route {l1.rno} to {t1.interchange_hub}, transfer to Route {l2.rno} to {t2.interchange_hub}, and take Route {l3.rno} to '{l3.alighting_stop.name}' (total duration ~{pj.total_duration_mins} mins, {fare_en}).{time_est_en}"
                }.get(lang, pj.summary_text_en)
                return self._response(msg, "route_search", lang=lang)

            elif pj.transfers_count == 3 and len(pj.legs) >= 4:
                l1, l2, l3, l4 = pj.legs[0], pj.legs[1], pj.legs[2], pj.legs[3]
                t1, t2, t3 = pj.transfers[0], pj.transfers[1], pj.transfers[2]
                msg = {
                    "mr-IN": f"{search_res.origin.display_name} ते {search_res.destination.display_name} दरम्यान ३ बस बदलाव्या लागतील: प्रथम मार्ग {l1.rno} ने '{t1.interchange_hub_mr}' येथे जा, तेथून मार्ग {l2.rno} ने '{t2.interchange_hub_mr}' येथे, नंतर मार्ग {l3.rno} ने '{t3.interchange_hub_mr}' येथे, आणि अंतिम मार्ग {l4.rno} ने '{l4.alighting_stop.name}' ला पोहोचा (एकूण वेळ अंदाजे {pj.total_duration_mins} मिनिटे, {fare_mr}).{time_est_mr}",
                    "hi-IN": f"{search_res.origin.display_name} से {search_res.destination.display_name} के लिए 3 बसें बदलनी होंगी: पहले रूट {l1.rno} से '{t1.interchange_hub}' जाएं, फिर रूट {l2.rno} से '{t2.interchange_hub}', फिर रूट {l3.rno} से '{t3.interchange_hub}', और अंतिम रूट {l4.rno} से '{l4.alighting_stop.name}' पहुंचे (कुल समय {pj.total_duration_mins} मिनट, {fare_hi})।{time_est_hi}",
                    "en-IN": f"Journey between {search_res.origin.display_name} and {search_res.destination.display_name} requires 3 transfers: take Route {l1.rno} to {t1.interchange_hub}, Route {l2.rno} to {t2.interchange_hub}, Route {l3.rno} to {t3.interchange_hub}, and Route {l4.rno} to '{l4.alighting_stop.name}' (total duration ~{pj.total_duration_mins} mins, {fare_en}).{time_est_en}"
                }.get(lang, pj.summary_text_en)
                return self._response(msg, "route_search", lang=lang)

            else:
                msg = {
                    "mr-IN": f"{pj.summary_text_mr} ({fare_mr}).{time_est_mr}",
                    "hi-IN": f"{pj.summary_text_hi} ({fare_hi}).{time_est_hi}",
                    "en-IN": f"{pj.summary_text_en} ({fare_en}).{time_est_en}"
                }.get(lang, pj.summary_text_en)
                return self._response(msg, "route_search", lang=lang)

        else:
            # Route unserviceable: clear stale selected route information
            ctx.origin_display = orig_raw
            ctx.destination_display = dest_raw
            ctx.selected_route_id = None
            ctx.selected_rno = None
            ctx.selected_route_name = None
            ctx.boarding_stop_id = None
            ctx.boarding_stop_name = None
            ctx.alighting_stop_id = None
            ctx.alighting_stop_name = None
            ctx.last_direct_routes = []
            ctx.last_transfer_routes = []
            ctx.last_fare_estimate = None
            ctx.last_timetable_response = None

            if search_res.clarification_question:
                msg = search_res.clarification_question
            else:
                msg = {
                    "mr-IN": f"{orig_raw} ते {dest_raw} दरम्यान थेट किंवा जोडणी मार्ग आढळला नाही. कृपया थांब्याचे नाव तपासून पुन्हा विचारा.",
                    "hi-IN": f"{orig_raw} से {dest_raw} के बीच कोई सीधा या कनेक्टिंग रूट नहीं मिला। कृपया स्टॉप का नाम जांचकर पुनः प्रयास करें।",
                    "en-IN": f"No direct or connecting bus route found between {orig_raw} and {dest_raw}. Please verify the stop names and try again."
                }.get(lang, f"No route found between {orig_raw} and {dest_raw}.")
            return self._response(msg, "route_search", lang=lang)

    def process_turn(
        self,
        session_id: str,
        user_text: str,
        language: str = "mr-IN"
    ) -> Dict[str, Any]:
        """
        Processes a conversation turn deterministically using structured session context.
        Returns:
        {
            "handled_deterministically": bool,
            "response_text": str,
            "intent": str,
            "map_action": Optional[Dict[str, Any]],
            "is_terminated": bool
        }
        """
        ctx = self.get_or_create_context(session_id, language=language)
        ctx.turn_count += 1
        intent = self.detect_intent(user_text, ctx)
        ctx.last_intent = intent
        lang = ctx.language

        # -------------------------------------------------------------
        # 1. Termination Intent
        # -------------------------------------------------------------
        if intent == "termination":
            ctx.is_terminated = True
            msg = {
                "mr-IN": "सिटीलिंक नाशिक बस सेवेचा वापर केल्याबद्दल धन्यवाद! आपला प्रवास सुखकर होवो. बाय!",
                "hi-IN": "सिटीलिंक नाशिक बस सेवा का उपयोग करने के लिए धन्यवाद! आपकी यात्रा मंगलमय हो। अलविदा!",
                "en-IN": "Thank you for using CityLinc Nashik bus assistant! Have a safe and pleasant journey. Goodbye!"
            }.get(lang, "Goodbye!")
            return self._response(msg, "termination", is_terminated=True, lang=lang)

        # -------------------------------------------------------------
        # 2. Polite Thanks (Keeps session alive)
        # -------------------------------------------------------------
        if intent == "thanks":
            msg = {
                "mr-IN": "काही हरकत नाही! मी तुम्हाला मार्ग, वेळापत्रक किंवा बस पासबद्दल आणखी काही मदत करू का?",
                "hi-IN": "आपका स्वागत है! क्या आपको रूट, समय-सारणी या बस पास के बारे में कोई और सहायता चाहिए?",
                "en-IN": "You are welcome! Would you like any further assistance regarding bus routes, timetables, or passes?"
            }.get(lang, "You are welcome!")
            return self._response(msg, "thanks", lang=lang)

        # -------------------------------------------------------------
        # 2B. Number Pronunciation Preference Intent
        # -------------------------------------------------------------
        if intent == "number_pronunciation_preference":
            t_low = user_text.lower()
            if any(w in t_low for w in ["मराठी", "marathi"]):
                ctx.pronounce_numbers_lang = "mr-IN"
                msg = "होय, आतापासून मी सर्व बस क्रमांक, वेळ आणि भाडे मराठी शब्दात सांगेन."
            elif any(w in t_low for w in ["हिंदी", "hindi"]):
                ctx.pronounce_numbers_lang = "hi-IN"
                msg = "जी हाँ, अब से मैं सभी बस नंबर, समय और किराया हिंदी शब्दों में बोलूँगा।"
            elif any(w in t_low for w in ["english", "इंग्रजी", "इंग्लिश"]):
                ctx.pronounce_numbers_lang = "en-IN"
                msg = "Sure, I will pronounce all bus route numbers, timings, and fares in English words."
            else:
                msg = {
                    "mr-IN": "होय, आतापासून मी सर्व बस क्रमांक, वेळ आणि भाडे मराठी शब्दात सांगेन.",
                    "hi-IN": "जी हाँ, अब से मैं सभी बस नंबर, समय और किराया शब्दों में बोलूँगा।",
                    "en-IN": "Sure, I will pronounce all bus route numbers, timings, and fares in words."
                }.get(lang, "Understood. Numbers will be pronounced in words.")

            ctx.pronounce_numbers_in_words = True
            return self._response(msg, "number_pronunciation_preference", lang=lang)

        # -------------------------------------------------------------
        # 3. Location Declaration ("मी KBT Circle जवळ आहे", "I am at CBS")
        # -------------------------------------------------------------
        if intent == "location_declaration":
            loc_match = re.search(r'(?:मी|आम्ही)\s+([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:जवळ|येथे|वर|पाशी|मध्ये)\s*आहे', user_text, re.IGNORECASE)
            if not loc_match:
                loc_match = re.search(r'i(?:\x27m|m|\s+am)\s+(?:at|near|around|in)\s+([a-zA-Z0-9\s]+)', user_text, re.IGNORECASE)
            if not loc_match:
                loc_match = re.search(r'(?:मैं|हम)\s+([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:के पास|पर|में)\s*हूँ', user_text, re.IGNORECASE)

            loc_raw = loc_match.group(1).strip() if loc_match else user_text.strip()
            resolved = route_engine.resolve_place(loc_raw)
            ctx.current_location = resolved.canonical_name.title() if resolved.canonical_name else loc_raw.title()
            ctx.current_location_display = resolved.display_name
            ctx.current_location_station_ids = resolved.matched_station_ids

            if resolved.matched_station_ids:
                st = location_service.stations_by_id.get(resolved.matched_station_ids[0])
                if st:
                    ctx.current_location_lat = st["lat"]
                    ctx.current_location_lon = st["lon"]
            elif "kbt" in loc_raw.lower() or "केबीटी" in loc_raw:
                ctx.current_location_lat = 20.0097
                ctx.current_location_lon = 73.7658

            # P0.2: Synchronize origin with current location
            ctx.origin = ctx.current_location
            ctx.origin_display = ctx.current_location_display

            # If user already specified a destination, immediately calculate and return route
            if ctx.destination:
                return self._handle_route_search(ctx, ctx.origin, ctx.destination, lang=lang, user_text=user_text)

            stops_sample = ", ".join(resolved.sample_stops[:2]) if resolved.sample_stops else "जवळचे थांबे"
            msg = {
                "mr-IN": f"समजले, तुम्ही सध्या {ctx.current_location_display} जवळ आहात (जवळचे थांबे: {stops_sample}). तुम्हाला कोठे जायचे आहे किंवा जवळचे रुग्णालय हवे आहे का?",
                "hi-IN": f"समझ गया, आप अभी {ctx.current_location_display} के पास हैं (निकटतम स्टॉप: {stops_sample})। आपको कहाँ जाना है या अस्पताल की जानकारी चाहिए?",
                "en-IN": f"Got it! You are currently near {ctx.current_location_display} (nearby stops: {stops_sample}). Where would you like to travel, or would you like to find the nearest hospital?"
            }.get(lang, f"Current location noted: {ctx.current_location_display}.")

            return self._response(msg, "location_declaration", lang=lang)

        # -------------------------------------------------------------
        # 4. Hospital Count Query
        # -------------------------------------------------------------
        if intent == "hospital_count":
            total_count = len(location_service.hospital_entities)
            hosp_samples = ", ".join([h["name_mr" if lang == "mr-IN" else "name_en"] for h in location_service.hospital_entities[:5]])
            msg = {
                "mr-IN": f"सिटीलिंक अधिकृत ट्रॅन्झिट नोंदणीत एकूण {total_count} प्रमुख पडताळलेली रुग्णालये समाविष्ट आहेत (उदा. {hosp_samples}). ही सिटीलिंक बस थांब्यांशी थेट जोडलेली प्रमुख रुग्णालये आहेत, शहरातील सर्व खाजगी क्लिनिकची एकूण संख्या नाही.",
                "hi-IN": f"सिटीलिंक आधिकारिक ट्रांजिट रजिस्ट्री में कुल {total_count} सत्यापित अस्पताल शामिल हैं (जैसे {hosp_samples})। यह बस सेवा से जुड़े प्रमुख अस्पतालों की संख्या है, शहर के सभी निजी क्लीनिकों की नहीं।",
                "en-IN": f"The CityLinc verified transit registry includes {total_count} verified physical hospital entities across Nashik (including {hosp_samples}). Note that this represents CityLinc transit-linked hospitals with verified bus stops, not the total count of all private clinics in the city."
            }.get(lang, f"There are {total_count} verified hospitals in the CityLinc registry.")
            return self._response(msg, "hospital_count", lang=lang)

        # -------------------------------------------------------------
        # 5. Nearby Hospital Query
        # -------------------------------------------------------------
        if intent == "nearby_hospital":
            user_lat = ctx.current_location_lat
            user_lon = ctx.current_location_lon
            loc_label = ctx.current_location_display or ctx.current_location or "तुमचे ठिकाण"

            # Check if user mentioned an origin place directly in this query
            # Sort aliases by max length descending to match specific long phrases first
            sorted_alias_items = sorted(
                LOCATION_ALIASES.items(),
                key=lambda item: max(len(al) for al in item[1]),
                reverse=True
            )
            for canon_key, alias_list in sorted_alias_items:
                if any(al in user_text.lower() for al in alias_list):
                    resolved = route_engine.resolve_place(canon_key)
                    if resolved.matched_station_ids:
                        st = location_service.stations_by_id.get(resolved.matched_station_ids[0])
                        if st:
                            user_lat, user_lon = st["lat"], st["lon"]
                            loc_label = resolved.display_name
                            ctx.current_location = resolved.canonical_name.title() if resolved.canonical_name else canon_key.title()
                            ctx.current_location_display = resolved.display_name
                            ctx.current_location_lat = user_lat
                            ctx.current_location_lon = user_lon
                            break
                    elif "kbt" in canon_key.lower():
                        user_lat, user_lon = 20.0097, 73.7658
                        loc_label = resolved.display_name or "केबीटी सर्कल (KBT Circle)"
                        ctx.current_location = "KBT Circle"
                        ctx.current_location_display = loc_label
                        ctx.current_location_lat = user_lat
                        ctx.current_location_lon = user_lon
                        break

            if user_lat and user_lon:
                nearest = location_service.get_nearest_hospital(user_lat, user_lon)
                if nearest:
                    ctx.selected_hospital_id = nearest["hospital_id"]
                    ctx.selected_hospital_name = nearest["name_en"]
                    dist_km = nearest.get("distance_from_user_km", 1.0)
                    h_name = nearest["name_mr" if lang == "mr-IN" else "name_en"]
                    p_stop = nearest.get("primary_stop", {})
                    stop_name = p_stop.get("name", "जवळच्या थांब्यावर")

                    msg = {
                        "mr-IN": f"{loc_label} पासून सर्वात जवळचे पडताळलेले रुग्णालय '{h_name}' आहे (अंदाजे {dist_km} किमी). जाण्यासाठी '{stop_name}' थांब्यावर उतरावे.",
                        "hi-IN": f"{loc_label} के सबसे पास का सत्यापित अस्पताल '{h_name}' है (लगभग {dist_km} किमी)। वहाँ जाने के लिए '{stop_name}' स्टॉप पर उतरें।",
                        "en-IN": f"The closest verified hospital to {loc_label} is '{h_name}' (approx. {dist_km} km away). Alight at '{stop_name}' stop."
                    }.get(lang, f"Nearest hospital: {h_name} ({dist_km} km).")

                    map_action = {
                        "action": "highlight_hospital",
                        "hospital_id": nearest["hospital_id"],
                        "hospital_name": nearest["name_en"],
                        "physical_coordinates": nearest["physical_coordinates"],
                        "primary_stop": p_stop,
                        "linked_stops": nearest.get("linked_bus_stops", [])
                    }
                    return self._response(msg, "nearby_hospital", map_action=map_action, lang=lang)

            # If user location is unknown, prompt user to specify their location
            prompt_msg = {
                "mr-IN": "तुम्हाला कोणत्या भागातून किंवा थांब्यावरून जवळचे रुग्णालय हवे आहे? कृपया तुमचे ठिकाण सांगा (उदा. केबीटी सर्कल, सीबीएस, सातपूर, नाशिक रोड).",
                "hi-IN": "आप किस क्षेत्र या स्टॉप के पास का अस्पताल जानना चाहते हैं? कृपया अपना स्थान बताएं (उदा. केबीटी सर्कल, सीबीएस, सातपुर)।",
                "en-IN": "Which area or bus stop are you currently near? Please specify your location (e.g., KBT Circle, CBS, Satpur, Nashik Road) to find the nearest hospital."
            }.get(lang, "Please specify your current location.")
            return self._response(prompt_msg, "nearby_hospital", lang=lang)

        # -------------------------------------------------------------
        # 6. Hospital Distance Query
        # -------------------------------------------------------------
        if intent == "hospital_distance":
            nav_res = location_service.get_hospital_navigation(user_text)
            if not nav_res.get("resolved") and ctx.selected_hospital_id:
                nav_res = location_service.get_hospital_navigation(ctx.selected_hospital_id)

            if nav_res.get("resolved"):
                h_name = nav_res["name_mr" if lang == "mr-IN" else "name_en"]
                p_stop = nav_res.get("primary_alighting_stop", {})
                walk_m = p_stop.get("approx_walk_distance_m", 50)
                stop_name = p_stop.get("name", "बस थांबा")

                transit_dist_str = ""
                if ctx.current_location_lat and ctx.current_location_lon:
                    coords = nav_res.get("physical_coordinates", {})
                    if coords.get("lat") and coords.get("lon"):
                        u_dist = location_service.haversine_distance_km(ctx.current_location_lat, ctx.current_location_lon, coords["lat"], coords["lon"])
                        transit_dist_str = f" ({ctx.current_location} पासून अंतर अंदाजे {u_dist} किमी)"

                msg = {
                    "mr-IN": f"{h_name}{transit_dist_str}: '{stop_name}' बस थांब्यावरून रुग्णालयाच्या मुख्य प्रवेशद्वारापर्यंत अंदाजे {walk_m} मीटर चालण्याचे अंतर आहे.",
                    "hi-IN": f"{h_name}: '{stop_name}' बस स्टॉप से अस्पताल के मुख्य गेट तक लगभग {walk_m} मीटर पैदल दूरी है।",
                    "en-IN": f"For {nav_res['name_en']}: The walking distance from '{stop_name}' bus stop to the hospital main entrance is approximately {walk_m} meters."
                }.get(lang, f"Walking distance is approx. {walk_m} meters from {stop_name}.")

                return self._response(msg, "hospital_distance", lang=lang)

        # -------------------------------------------------------------
        # 7. Hospital Navigation Query
        # -------------------------------------------------------------
        if intent == "hospital_navigation":
            nav_res = location_service.get_hospital_navigation(user_text)
            if not nav_res.get("resolved") and ctx.selected_hospital_id:
                nav_res = location_service.get_hospital_navigation(ctx.selected_hospital_id)

            if nav_res.get("resolved"):
                ctx.selected_hospital_id = nav_res["hospital_id"]
                ctx.selected_hospital_name = nav_res["name_en"]
                h_name = nav_res["name_mr" if lang == "mr-IN" else "name_en"]
                p_stop = nav_res.get("primary_alighting_stop", {})
                stop_name = p_stop.get("name", "जवळच्या थांब्यावर")
                walk_m = p_stop.get("approx_walk_distance_m", 50)
                routes_sample = ", ".join(nav_res.get("direct_routes", [])[:6])

                # Provide transit routing if current location is known
                transit_route_msg = ""
                if ctx.current_location:
                    journey = route_engine.search_routes(ctx.current_location, stop_name)
                    if journey.success:
                        if journey.direct_routes:
                            dr = journey.direct_routes[0]
                            transit_route_msg = f" {ctx.current_location} वरून थेट मार्ग {dr.rno} ({dr.route_name}) ने '{dr.boarding_stop.name}' वरून बसावे."
                        elif journey.transfer_routes:
                            tr = journey.transfer_routes[0]
                            transit_route_msg = f" {ctx.current_location} वरून थेट बस नाही. तुम्ही {tr.interchange_hub_mr if lang == 'mr-IN' else tr.interchange_hub} येथे बस बदलू शकता (मार्ग {tr.leg1.rno} ने '{tr.transfer_stop_name}' ला जा व तेथून मार्ग {tr.leg2.rno} घ्या)."

                msg = {
                    "mr-IN": f"{h_name} ला जाण्यासाठी '{stop_name}' थांब्यावर उतरावे (प्रवेशद्वारापासून अंदाजे {walk_m} मीटर चालणे).{transit_route_msg} या थांब्यावरून धावणाऱ्या प्रमुख बसेस: {routes_sample}.",
                    "hi-IN": f"{h_name} जाने के लिए '{stop_name}' स्टॉप पर उतरें (अस्पताल के मुख्य गेट से लगभग {walk_m} मीटर पैदल)। मुख्य बसें: {routes_sample}।",
                    "en-IN": f"To reach {nav_res['name_en']}, alight at '{stop_name}' (approx. {walk_m}m walk to hospital main entrance). Passing routes: {routes_sample}."
                }.get(lang, f"Alight at {stop_name}.")

                map_action = {
                    "action": "highlight_hospital",
                    "hospital_id": nav_res["hospital_id"],
                    "hospital_name": nav_res["name_en"],
                    "physical_coordinates": nav_res["physical_coordinates"],
                    "primary_stop": p_stop,
                    "linked_stops": nav_res.get("linked_stops", [])
                }
                return self._response(msg, "hospital_navigation", map_action=map_action, lang=lang)

        # -------------------------------------------------------------
        # 7B. Route Confirmation Intent (P0.1)
        # e.g., "ही बस केबीटी सर्कलवरून पण जाते ना?", "Does this bus stop at CBS?"
        # -------------------------------------------------------------
        if intent == "route_confirmation":
            conf_stop = self._extract_confirmation_stop(user_text)

            # If no active route in session
            if not ctx.selected_route_id and not ctx.selected_rno:
                msg = {
                    "mr-IN": "सध्या कोणताही बस मार्ग निवडलेला नाही. कृपया तुम्हाला कोणत्या मार्गाबद्दल किंवा ठिकाणाबद्दल विचारायचे आहे ते सांगा.",
                    "hi-IN": "वर्तमान में कोई बस रूट सक्रिय नहीं है। कृपया बताएं कि आप किस रूट या स्थान के बारे में जानना चाहते हैं।",
                    "en-IN": "There is no active bus route currently selected. Please specify which route or destination you would like to ask about."
                }.get(lang, "No active route currently selected.")
                return self._response(msg, "route_confirmation", lang=lang)

            # Active route exists: verify whether conf_stop is on the active route
            active_rno = ctx.selected_rno or "निवडलेला मार्ग"
            active_rname = ctx.selected_route_name or ""
            target_stop_raw = conf_stop or user_text

            r_obj = None
            if ctx.selected_route_id:
                for r in knowledge_service.routes:
                    if r.get("routeid") == ctx.selected_route_id:
                        r_obj = r
                        break
            if not r_obj and ctx.selected_rno:
                for r in knowledge_service.routes:
                    if str(r.get("rno", "")).strip().upper() == str(ctx.selected_rno).strip().upper():
                        r_obj = r
                        break

            is_served = False
            matched_stop_name = target_stop_raw

            if r_obj:
                res_stop = route_engine.resolve_place(target_stop_raw)
                target_ids = set(res_stop.matched_station_ids)
                route_st_ids = {s.get("stationid") for s in r_obj.get("stops", [])}
                route_st_names_low = [s.get("stationname", "").lower() for s in r_obj.get("stops", [])]

                if target_ids and target_ids.intersection(route_st_ids):
                    is_served = True
                    matched_stop_name = res_stop.display_name
                elif any(target_stop_raw.lower() in sn for sn in route_st_names_low):
                    is_served = True
                    matched_stop_name = res_stop.display_name or target_stop_raw
                else:
                    for st in r_obj.get("stops", []):
                        sn = st.get("stationname", "")
                        if target_stop_raw.lower() in sn.lower():
                            is_served = True
                            matched_stop_name = sn
                            break

            if is_served:
                msg = {
                    "mr-IN": f"होय, मार्ग {active_rno} ({active_rname}) हा '{matched_stop_name}' थांब्यावरून जातो.",
                    "hi-IN": f"हाँ, रूट {active_rno} ({active_rname}) '{matched_stop_name}' स्टॉप से होकर जाता है।",
                    "en-IN": f"Yes, Route {active_rno} ({active_rname}) does serve '{matched_stop_name}' stop."
                }.get(lang, f"Yes, Route {active_rno} serves {matched_stop_name}.")
            else:
                msg = {
                    "mr-IN": f"नाही, मार्ग {active_rno} हा '{matched_stop_name}' थांब्यावरून जात नाही.",
                    "hi-IN": f"नहीं, रूट {active_rno} '{matched_stop_name}' स्टॉप से होकर नहीं जाता है।",
                    "en-IN": f"No, Route {active_rno} does not serve '{matched_stop_name}'."
                }.get(lang, f"No, Route {active_rno} does not serve {matched_stop_name}.")

            # Crucial: Preserves session active journey state without resetting
            return self._response(msg, "route_confirmation", lang=lang)

        # -------------------------------------------------------------
        # 7C. Journey Follow-Up Queries (Alighting Stop, Transfer Next Bus, Duration)
        # -------------------------------------------------------------
        u_low = user_text.lower()
        if any(w in u_low for w in ["कुठे उतरायचं", "उतरायचं कुठे", "कोणत्या थांब्यावर उतरावे", "where to get off", "where to alight", "कहाँ उतरना है", "कहाँ उतरें", "उतरना कहाँ"]):
            if ctx.last_direct_routes:
                dr = ctx.last_direct_routes[0]
                msg = {
                    "mr-IN": f"तुम्ही '{dr['alighting_stop']['name']}' थांब्यावर उतरावे.",
                    "hi-IN": f"आप '{dr['alighting_stop']['name']}' स्टॉप पर उतरें।",
                    "en-IN": f"You should alight at '{dr['alighting_stop']['name']}' stop."
                }.get(lang, f"Alight at {dr['alighting_stop']['name']}.")
                return self._response(msg, "journey_followup", lang=lang)
            elif ctx.last_transfer_routes:
                tr = ctx.last_transfer_routes[0]
                msg = {
                    "mr-IN": f"प्रथम {tr['interchange_hub_mr']} येथील '{tr['transfer_stop_name']}' थांब्यावर उतरावे आणि तेथून मार्ग {tr['leg2']['rno']} घेऊन अंतिम '{tr['leg2']['alighting_stop']['name']}' थांब्यावर उतरावे.",
                    "hi-IN": f"पहले {tr['interchange_hub']} पर '{tr['transfer_stop_name']}' स्टॉप पर उतरें और फिर रूट {tr['leg2']['rno']} से '{tr['leg2']['alighting_stop']['name']}' पर उतरें।",
                    "en-IN": f"First alight at '{tr['transfer_stop_name']}' ({tr['interchange_hub']}) and then take Route {tr['leg2']['rno']} to alight at '{tr['leg2']['alighting_stop']['name']}'."
                }.get(lang, f"Alight at {tr['transfer_stop_name']}.")
                return self._response(msg, "journey_followup", lang=lang)

        if any(w in u_low for w in ["तिथून पुढची बस", "तिथून कोणती बस", "पुढची बस कोणती", "वहाँ से अगली बस", "next bus from there"]):
            if ctx.last_transfer_routes:
                tr = ctx.last_transfer_routes[0]
                dep_time = tr['leg2'].get('departure_time') or 'वेळापत्रकानुसार'
                msg = {
                    "mr-IN": f"तिथून ({tr['interchange_hub_mr']}) पुढील जोडणी बस मार्ग {tr['leg2']['rno']} ({tr['leg2']['route_name']}) ही {dep_time} वाजता आहे, जी '{tr['leg2']['alighting_stop']['name']}' पर्यंत जाते.",
                    "hi-IN": f"वहाँ से ({tr['interchange_hub']}) अगली बस रूट {tr['leg2']['rno']} ({tr['leg2']['route_name']}) {dep_time} बजे है, जो '{tr['leg2']['alighting_stop']['name']}' तक जाती है।",
                    "en-IN": f"From there ({tr['interchange_hub']}), the connecting bus is Route {tr['leg2']['rno']} ({tr['leg2']['route_name']}) departing at {dep_time} towards '{tr['leg2']['alighting_stop']['name']}'."
                }.get(lang, f"Connecting bus is Route {tr['leg2']['rno']}.")
                return self._response(msg, "journey_followup", lang=lang)

        if any(w in u_low for w in ["किती वेळ लागेल", "वेळ किती लागेल", "how much time", "how long will it take", "कितना समय लगेगा"]):
            if ctx.last_direct_routes:
                dur = ctx.last_direct_routes[0].get('duration_mins', 25)
                msg = {
                    "mr-IN": f"या थेट प्रवासासाठी अंदाजे {dur} मिनिटे लागतील.",
                    "hi-IN": f"इस सीधी यात्रा में लगभग {dur} मिनट लगेंगे।",
                    "en-IN": f"This direct journey will take approximately {dur} minutes."
                }.get(lang, f"Journey will take approx. {dur} minutes.")
                return self._response(msg, "journey_followup", lang=lang)
            elif ctx.last_transfer_routes:
                dur = ctx.last_transfer_routes[0].get('total_duration_mins', 45)
                msg = {
                    "mr-IN": f"बस बदलणे आणि प्रवासासह एकूण अंदाजे {dur} मिनिटे लागतील.",
                    "hi-IN": f"बस बदलने और यात्रा सहित कुल लगभग {dur} मिनट लगेंगे।",
                    "en-IN": f"Total journey with transfer will take approximately {dur} minutes."
                }.get(lang, f"Journey will take approx. {dur} minutes.")
                return self._response(msg, "journey_followup", lang=lang)

        # -------------------------------------------------------------
        # 7D. Negation & Mid-Conversation Correction Handling
        # -------------------------------------------------------------
        corr_type, old_entity, new_entity = self._extract_negation_correction(user_text, ctx)
        if corr_type == "dest" and new_entity:
            known_origin = ctx.origin or ctx.current_location
            if known_origin:
                return self._handle_route_search(ctx, known_origin, new_entity, lang=lang, user_text=user_text)
            else:
                ctx.destination = new_entity
                msg = {
                    "mr-IN": f"समजले, गंतव्य बदलून {new_entity} केले आहे. कृपया तुम्ही कोणत्या थांब्यावरून प्रवास सुरू करणार आहात ते सांगा.",
                    "hi-IN": f"समझ गया, गंतव्य बदलकर {new_entity} किया गया है। कृपया बताएं कि आप किस स्टॉप से यात्रा शुरू करेंगे।",
                    "en-IN": f"Understood, destination updated to {new_entity}. Where will you be boarding or starting your journey from?"
                }.get(lang, f"Starting point needed for {new_entity}.")
                return self._response(msg, "route_search", lang=lang)
        elif corr_type == "origin" and new_entity:
            if ctx.destination:
                return self._handle_route_search(ctx, new_entity, ctx.destination, lang=lang, user_text=user_text)
            else:
                ctx.origin = new_entity
                msg = {
                    "mr-IN": f"समजले, सुरुवातीचा थांबा बदलून {new_entity} केला आहे. तुम्हाला कोठे जायचे आहे ते सांगा.",
                    "hi-IN": f"समझ गया, प्रारंभिक स्टॉप बदलकर {new_entity} किया गया है। आपको कहाँ जाना है बताएं।",
                    "en-IN": f"Understood, starting point updated to {new_entity}. Where would you like to travel to?"
                }.get(lang, f"Destination needed from {new_entity}.")
                return self._response(msg, "route_search", lang=lang)

        # -------------------------------------------------------------
        # 8. Origin-Destination Route Search & Multi-turn Slots (P0.2 & P1.1)
        # -------------------------------------------------------------
        parsed_od = self._extract_origin_destination(user_text)
        if parsed_od:
            orig_raw, dest_raw = parsed_od
            return self._handle_route_search(ctx, orig_raw, dest_raw, lang=lang, user_text=user_text)

        # Single-slot Destination ("मला तपोवन जायचंय", "Take me to Nashik Road")
        dest_only = self._extract_single_destination(user_text)
        if dest_only:
            known_origin = ctx.origin or ctx.current_location
            if known_origin:
                return self._handle_route_search(ctx, known_origin, dest_only, lang=lang, user_text=user_text)
            else:
                ctx.destination = dest_only
                msg = {
                    "mr-IN": f"समजले, तुम्हाला {dest_only} ला जायचे आहे. कृपया तुम्ही कोणत्या थांब्यावरून किंवा भागातून प्रवास सुरू करणार आहात ते सांगा.",
                    "hi-IN": f"समझ गया, आपको {dest_only} जाना है। कृपया बताएं कि आप किस स्टॉप या स्थान से यात्रा शुरू करेंगे।",
                    "en-IN": f"Got it, you want to reach {dest_only}. Where will you be boarding or starting your journey from?"
                }.get(lang, f"Starting point needed for destination {dest_only}.")
                return self._response(msg, "route_search", lang=lang)

        # Single-slot Origin update ("आता मी द्वारका येथे आहे", "I am now at CBS")
        orig_only = self._extract_single_origin(user_text)
        if orig_only and ctx.destination:
            return self._handle_route_search(ctx, orig_only, ctx.destination, lang=lang, user_text=user_text)

        # Contextual bare stop slot-filling when waiting for origin or destination
        resolved_bare = route_engine.resolve_place(user_text)
        if resolved_bare.matched_station_ids:
            if ctx.destination and not ctx.origin:
                return self._handle_route_search(ctx, user_text, ctx.destination, lang=lang, user_text=user_text)
            elif ctx.origin and not ctx.destination:
                return self._handle_route_search(ctx, ctx.origin, user_text, lang=lang, user_text=user_text)
            elif not ctx.origin and not ctx.destination:
                # Standalone stop query with no context yet (Test Case D & E)
                ctx.origin = resolved_bare.canonical_name.title()
                ctx.origin_display = resolved_bare.display_name
                msg = {
                    "mr-IN": f"समजले, तुम्ही {ctx.origin_display} येथून प्रवास सुरू करणार आहात. तुम्हाला कोठे जायचे आहे? (उदा. Nashik Road, Panchavati, CBS).",
                    "hi-IN": f"समझ गया, आप {ctx.origin_display} से यात्रा शुरू कर रहे हैं। आपको कहाँ जाना है? (जैसे Nashik Road, Panchavati, CBS)।",
                    "en-IN": f"Understood, starting from {ctx.origin_display}. Where would you like to travel to? (e.g., Nashik Road, Panchavati, CBS)."
                }.get(lang, f"Starting from {ctx.origin_display}. Where would you like to go?")
                return self._response(msg, "route_search", lang=lang)
            elif ctx.origin and ctx.destination:
                ctx.destination = resolved_bare.canonical_name.title()
                ctx.destination_display = resolved_bare.display_name
                return self._handle_route_search(ctx, ctx.origin, ctx.destination, lang=lang, user_text=user_text)

        # -------------------------------------------------------------
        # 9. Next Bus Follow-Up Query
        # -------------------------------------------------------------
        if intent == "next_bus":
            if ctx.selected_rno or ctx.selected_route_id:
                target_r = str(ctx.selected_route_id or ctx.selected_rno)
                b_name = ctx.boarding_stop_name
                tt_res = timetable_engine.query_route_departures(
                    target_r,
                    boarding_stop_id=ctx.boarding_stop_id,
                    boarding_stop_name=b_name,
                    query_mode="next"
                )
                if tt_res.success and tt_res.trips_count > 0:
                    ctx.last_timetable_response = tt_res.model_dump()
                    reply = {
                        "mr-IN": tt_res.message_mr,
                        "hi-IN": tt_res.message_hi,
                        "en-IN": tt_res.message_en
                    }.get(lang, tt_res.message_en)
                    return self._response(reply, "next_bus", lang=lang)

            if ctx.origin and ctx.destination:
                tt_res = timetable_engine.get_next_bus_for_od(ctx.origin, ctx.destination)
                if tt_res.success and tt_res.trips_count > 0:
                    ctx.last_timetable_response = tt_res.model_dump()
                    reply = {
                        "mr-IN": tt_res.message_mr,
                        "hi-IN": tt_res.message_hi,
                        "en-IN": tt_res.message_en
                    }.get(lang, tt_res.message_en)
                    return self._response(reply, "next_bus", lang=lang)

            ask_msg = {
                "mr-IN": "तुम्हाला कोणत्या मार्गाची किंवा कुठून कुठे जाणाऱ्या बसची वेळ जाणून घ्यायची आहे? कृपया सांगा.",
                "hi-IN": "आप किस रूट या कहाँ से कहाँ तक की बस का समय जानना चाहते हैं? कृपया बताएं।",
                "en-IN": "Which route or origin-destination would you like the next bus timing for? Please let me know."
            }.get(lang, "Please specify the route or origin and destination.")
            return self._response(ask_msg, "next_bus", lang=lang)

        # -------------------------------------------------------------
        # 10. Fare & Bus Pass Query
        # -------------------------------------------------------------
        if intent == "fare":
            t_norm = user_text.lower()
            t_no_from = re.sub(r'पा(?:सू|सु)न[ा-्]*', '', t_norm)
            tokens = set(re.findall(r'[a-zA-Z0-9\u0900-\u097F]+', t_no_from))
            is_pass = any(p in t_norm for p in [
                "bus pass", "student pass", "monthly pass", "daily pass", "pass rate", "pass price", "pass cost",
                "बस पास", "विद्यार्थी पास", "मासिक पास", "दैनिक पास", "पास दर", "पासचे दर", "पास बद्दल", "पास पाहिजे",
                "पास हवा", "पास मिळेल का"
            ]) or "पास" in tokens or "pass" in tokens

            if is_pass:
                if any(w in t_norm for w in ["student", "विद्यार्थी", "छात्र"]):
                    msg = {
                        "mr-IN": "सिटीलिंक विद्यार्थी पास (Student Pass): ३० दिवसांच्या मासिक पासवर ५०% सवलत आहे, आणि ९० दिवसांच्या त्रैमासिक पासवर ६६.६०% सवलत मिळते. हा पास नाशिक शहरात सर्वसामान्य बस प्रवासासाठी वैध आहे.",
                        "hi-IN": "सिटीलिंक छात्र पास (Student Pass): 30 दिनों के पास पर 50% छूट और 90 दिनों के पास पर 66.60% छूट मिलती है। यह नाशिक शहर की सभी बसों में मान्य है।",
                        "en-IN": "CityLinc Student Bus Pass: 50% concession on 30-Day monthly pass and 66.60% concession on 90-Day quarterly pass across all Nashik routes."
                    }.get(lang, "Student pass offers 50% discount for 30 days and 66.6% discount for 90 days.")
                else:
                    msg = {
                        "mr-IN": "सिटीलिंक बस पास दर: १ दिवसाचा ओपन पास ₹११०, ७ दिवस ₹५३०, ३० दिवसांचा मासिक पास ₹२,१००, त्रैमासिक पास (९० दिवस) ₹५,२५०, आणि अर्धवार्षिक पास (१८० दिवस) ₹९,४५०. विद्यार्थ्यांना मासिक पासवर ५०% सवलत मिळते.",
                        "hi-IN": "सिटीलिंक बस पास दर: 1 दिन का पास ₹110, 7 दिन ₹530, 30 दिन का अनलिमिटेड मासिक पास ₹2,100, 90 दिन ₹5,250, और 180 दिन ₹9,450 है। छात्रों को 50% छूट मिलती है।",
                        "en-IN": "CityLinc All-Route Bus Passes: 1-Day Open Pass is ₹110, 7-Day is ₹530, 30-Day monthly pass is ₹2,100, 90-Day is ₹5,250, and 180-Day is ₹9,450. Students receive 50% concession."
                    }.get(lang, "CityLinc 30-Day pass is ₹2,100; 90-Day pass is ₹5,250.")
                return self._response(msg, "fare", lang=lang)

            orig = ctx.origin_display or ctx.origin
            dest = ctx.destination_display or ctx.destination
            if orig and dest:
                fare = ctx.last_fare_estimate.get("single_fare_inr", 20) if ctx.last_fare_estimate else 20
                dist = ctx.last_fare_estimate.get("distance_km", 10.0) if ctx.last_fare_estimate else 10.0
                fare_bd = ctx.last_fare_estimate.get("fare_breakdown") if ctx.last_fare_estimate else None
                fare_detail_mr = fare_service.format_fare_message(fare_bd, lang="mr-IN") if fare_bd else f"एका व्यक्तीचे तिकीट भाडे ₹{fare} आहे"
                fare_detail_hi = fare_service.format_fare_message(fare_bd, lang="hi-IN") if fare_bd else f"एक व्यक्ति का टिकट किराया ₹{fare} है"
                fare_detail_en = fare_service.format_fare_message(fare_bd, lang="en-IN") if fare_bd else f"single adult ticket fare is ₹{fare}"

                msg = {
                    "mr-IN": f"{orig} ते {dest} ({dist} किमी) प्रवासासाठी {fare_detail_mr}. (मासिक पासवर ५०% सवलत उपलब्ध).",
                    "hi-IN": f"{orig} से {dest} ({dist} किमी) की यात्रा के लिए {fare_detail_hi}। (मासिक पास पर 50% छूट उपलब्ध है)।",
                    "en-IN": f"For travel between {orig} and {dest} ({dist} km), {fare_detail_en}. (Monthly pass discount available)."
                }.get(lang, f"The fare is ₹{fare}.")
                return self._response(msg, "fare", lang=lang)

            msg = {
                "mr-IN": "सिटीलिंक नाशिक बस तिकीट दर १ जानेवारी २०२३ च्या अधिकृत टप्प्यांनुसार अंतरावर आधारित आहेत. १ दिवसाचा ओपन पास ₹११० आणि ३० दिवसांचा मासिक पास ₹२,१०० आहे.",
                "hi-IN": "सिटीलिंक बस टिकट का किराया १ जनवरी २०२३ की आधिकारिक तालिका के अनुसार दूरी पर आधारित है। 1 दिन का ओपन पास ₹110 और 30 दिन का पास ₹2,100 है।",
                "en-IN": "CityLinc bus fares are distance-based according to official Jan-2023 tariff slabs. 1-Day open pass is ₹110 and 30-Day pass is ₹2,100."
            }.get(lang, "Bus fares are distance-based. 1-Day pass is ₹110 and 30-Day pass is ₹2,100.")
            return self._response(msg, "fare", lang=lang)

        # Fallback for unrecognized general queries - NEVER return empty string
        fallback_msg = {
            "mr-IN": "मला समजले नाही. कृपया तुम्हाला कुठून कुठे जायचे आहे ते सांगा (उदा. 'निमाणी ते सीबीएस' किंवा 'नाशिक रोडला जायचं आहे').",
            "hi-IN": "मुझे समझ नहीं आया। कृपया बताएं कि आपको कहाँ से कहाँ जाना है (जैसे 'निमाणी से सीबीएस' या 'नाशिक रोड जाना है')।",
            "en-IN": "I didn't quite catch that. Please let me know your origin and destination (e.g., 'From Nimani to CBS' or 'I want to go to Nashik Road')."
        }.get(lang, "Please specify your origin and destination.")
        return self._response(fallback_msg, "general", lang=lang)

    def _clean_token(self, t: str) -> str:
        val = t.strip()

        # Strip common vocative particles / discourse tags (e.g., 'रे बाबा', 'दादा', 'भावा')
        vocatives_patterns = [
            r'\bरे\s*बाबा\b', r'\bबाबा\b', r'\bदादा\b', r'\bभावा\b', r'\bभाऊ\b',
            r'\bमित्रा\b', r'\bयार\b', r'\bभाई\b', r'\bसर\b', r'\bमॅडम\b'
        ]
        for voc in vocatives_patterns:
            val = re.sub(voc, ' ', val, flags=re.IGNORECASE)

        prefixes = [
            "हॅलो,", "हॅलो", "नमस्कार,", "नमस्कार", "नमस्ते,", "नमस्ते", "hello,", "hello",
            "मला ना ", "मला फक्त ", "मला जरा ", "मला कृपया ", "मला आता ", "मला तर ", "मला सांगा ना ", "मला सांगा ", "मला ",
            "आम्हाला ", "कृपया मला ", "कृपया ", "ना ", "हो ", "अरे ", "अहो ", "फक्त ",
            "मुझे ", "कृपया मुझे ", "please ", "can you tell me ", "i want to go from ", "how to go from ",
            "how can i reach ", "how to get to ", "how do i go to ", "i want to travel from ",
            "i want to go to ", "how to reach ", "from ", "आता ", "ते ", "ते आपलं ", "आपलं ", "आपल्या ", "आपला "
        ]
        suffixes = [
            " बस स्टॉप", " बस स्थानक", " बस स्टँड", " थांबा", " स्टॉप", " बस", " गाडी", " bus", " stop",
            " जायचं आहे", " जायचे आहे", " जायचंय", " जायचेय", " जायचं", " जायचे", " जाणे आहे", " जाना है", " जाना",
            " जाण्यासाठी कोणती बस उपलब्ध आहे", " जाण्यासाठी कोणती बस आहे", " जाण्यासाठी कोणती गाडी उपलब्ध आहे",
            " जाण्यासाठी कोणती बस जाते", " जाण्यासाठी बस", " जाण्यासाठी",
            " कोणती बस जाते", " कोणती बस उपलब्ध आहे", " कोणती गाडी जाते", " कोणती बस", " कोणती गाडी",
            " उपलब्ध आहे", " उपलब्ध आहेत", " जाते", " जातात", " पर्यंत", " ला", " कडे", " स",
            " to go", " want to reach"
        ]

        changed = True
        while changed:
            changed = False
            val = val.strip(' ,.?!;:()\"\'’‘[]{}')
            for pfx in prefixes:
                if val.lower().startswith(pfx.lower()):
                    val = val[len(pfx):].strip()
                    changed = True
                    break
            for sfx in suffixes:
                if val.lower().endswith(sfx.lower()):
                    val = val[:-len(sfx)].strip()
                    changed = True
                    break

        # Marathi oblique case normalization (विभक्ती सामान्य रूप -> प्रथमा)
        obliques = [
            ("नाक्या", "नाका"),
            ("फाट्या", "फाटा"),
            ("चौका", "चौक"),
            ("मंदिरा", "मंदिर"),
            ("स्थानका", "स्थानक"),
            ("सर्कलला", "सर्कल"),
            ("रोडला", "रोड"),
        ]
        for obl, nom in obliques:
            if val.endswith(obl):
                val = val[:-len(obl)] + nom
                break

        return val.strip(' ,.?!;:()\"\'’‘[]{}')

    def _extract_confirmation_stop(self, text: str) -> Optional[str]:
        """Extracts the queried waypoint or stop from a route confirmation question."""
        norm = text.strip()

        # English: Does this bus / will this bus go through / stop at / serve [Stop]?
        m_en = re.search(r'(?:does\s+(?:this|the)\s+bus|will\s+this\s+bus|is\s+this\s+bus)\s+(?:also\s+)?(?:go\s+through|pass\s+through|stop\s+at|serve|go\s+to|reach|cover)\s+([a-zA-Z0-9\s]+?)(?:\s*(?:also|too|\?|$))', norm, re.IGNORECASE)
        if m_en:
            return self._clean_token(m_en.group(1))

        # Marathi: ही बस / ही गाडी [Stop] वरून / कडे / ला / मधून / तिथून [पण] जाते का / जाते ना / थांबते का / थांबते ना
        m_mr = re.search(r'(?:ही\s+बस|ही\s+गाडी|हा\s+मार्ग|या\s+बसने|बस)\s+([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:वरून|पा(?:सू|सु)न|हून|मधून|ला|कडे|तिथून)?\s*(?:पण\s+)?(?:जाते\s+का|जाते\s+ना|जाईल\s+का|थांबते\s+का|थांबते\s+ना|येते\s+का|जाणार\s+का|होते\s+का|\?|$)', norm, re.IGNORECASE)
        if m_mr:
            token = self._clean_token(m_mr.group(1))
            for sfx in ["वरून", "पासून", "हून", "मधून", "ला", "कडे"]:
                if token.endswith(sfx):
                    token = token[:-len(sfx)].strip()
            if token and token not in ["पण", "का", "ना", "बस"]:
                return token

        # Marathi alternative: [Stop] वरून पण जाते ना? / [Stop] वरून जाते का?
        m_mr_alt = re.search(r'([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:वरून|पा(?:सू|सु)न|हून|मधून)\s*(?:पण\s+)?(?:जाते\s+का|जाते\s+ना|जाईल\s+का|थांबते\s+का|थांबते\s+ना|\?)', norm, re.IGNORECASE)
        if m_mr_alt:
            token = self._clean_token(m_mr_alt.group(1))
            for pfx in ["ही बस", "ही गाडी", "हा मार्ग", "या बसने"]:
                if token.lower().startswith(pfx):
                    token = token[len(pfx):].strip()
            if token and token not in ["पण", "का", "ना"]:
                return token

        # Hindi: क्या यह बस / ये बस [Stop] से / पर (भी) जाती है क्या / रुकती है क्या / जाती है ना
        m_hi = re.search(r'(?:क्या\s+यह\s+बस|क्या\s+ये\s+बस|ये\s+बस|यह\s+बस)\s+([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:से|पर|तक)?\s*(?:भी\s+)?(?:होकर\s+)?(?:जाती\s+है\s+क्या|जाती\s+है\s+ना|रुकती\s+है\s+क्या|रुकती\s+है\s+ना|जाएगी\s+क्या|\?|$)', norm, re.IGNORECASE)
        if m_hi:
            token = self._clean_token(m_hi.group(1))
            for sfx in ["से", "पर", "तक"]:
                if token.endswith(sfx):
                    token = token[:-len(sfx)].strip()
            if token and token not in ["भी", "क्या", "ना"]:
                return token

        # Roman Marathi / Hinglish: ye bus [Stop] se jaati hai na? / hi bus [Stop] varun jaate ka?
        m_rom = re.search(r'(?:ye\s+bus|yeh\s+bus|hi\s+bus)\s+([a-zA-Z0-9\s]+?)\s*(?:se|varun|houn|la)?\s*(?:pan\s+)?(?:jaati\s+hai|jaate\s+ka|jaate\s+na|jaayegi\s+kya|rukegi\s+kya)', norm, re.IGNORECASE)
        if m_rom:
            return self._clean_token(m_rom.group(1))

        return None

    def _extract_negation_correction(
        self,
        text: str,
        ctx: SessionTransitContext
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Detects negation corrections such as:
        - 'नाही, सीबीएस नाही, शालिमारला जायचं आहे'
        - 'सीबीएस नाही, शालिमार'
        - 'निमाणी नहीं, सीबीएस जाना है'
        - 'no, not CBS, Shalimar'
        - 'नाही, मला सातपूरवरून जायचं आहे'
        - 'ना निमानी नाही रे बाबा, निमानी फक्त निमानी'
        Returns (action_type, old_entity, new_entity) where action_type is 'dest', 'origin', or None.
        """
        t = text.strip()

        # Clean vocatives first so idiom tokens (e.g., 'रे बाबा', 'दादा') are not misidentified as places
        vocatives_patterns = [
            r'\bरे\s*बाबा\b', r'\bबाबा\b', r'\bदादा\b', r'\bभावा\b', r'\bभाऊ\b',
            r'\bमित्रा\b', r'\bयार\b', r'\bभाई\b'
        ]
        t_voc = t
        for voc in vocatives_patterns:
            t_voc = re.sub(voc, ' ', t_voc, flags=re.IGNORECASE)
        t_voc = re.sub(r'\s+', ' ', t_voc).strip()

        m_lead = re.match(r'^(?:नाही|नको|नहीं|no)[,\s]+', t_voc, re.IGNORECASE)
        t_clean = t_voc[m_lead.end():].strip() if m_lead else t_voc

        # Pattern 1: [Old] नाही/नहीं/not, [New]
        m_corr = re.search(
            r'([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:नाही|नको|नहीं|not)[,\s]+(?:मला\s+|मुझे\s+)?([a-zA-Z0-9\u0900-\u097F\s]+)',
            t_voc,
            re.IGNORECASE
        )
        if m_corr:
            old_p = self._clean_token(m_corr.group(1))
            new_p = m_corr.group(2).strip()
            old_p = re.sub(r'^(?:नाही|नको|नहीं|no)[,\s]+', '', old_p, flags=re.IGNORECASE).strip()

            # Clean trailing verbs from new_p
            new_p_clean = re.sub(
                r'\s*(?:जायचे|जायचं|जायचेय|जायचंय|जाना\s*है|जाना|पोहोचायचे|to\s*go|निघायचे|आहे|का|\?|\.|$).*',
                '',
                new_p,
                flags=re.IGNORECASE
            ).strip()
            new_p_clean = self._clean_token(new_p_clean)

            # Ensure new_p_clean actually resolves to a valid transit place/stop
            new_res = route_engine.resolve_place(new_p_clean)
            if not new_res.matched_station_ids:
                # Try individual words if user said e.g. "निमानी फक्त निमानी"
                found_tok = None
                for word in new_p_clean.split():
                    w_c = self._clean_token(word)
                    if route_engine.resolve_place(w_c).matched_station_ids:
                        found_tok = w_c
                        break
                if found_tok:
                    new_p_clean = found_tok
                    new_res = route_engine.resolve_place(new_p_clean)
                else:
                    return None, None, None

            # Check if old_p matches current destination or if new_p has destination markers
            dest_res = route_engine.resolve_place(old_p)
            ctx_dest_res = route_engine.resolve_place(ctx.destination or "")
            if (
                ctx.destination
                and (old_p.lower() in ctx.destination.lower() or dest_res.canonical_name == ctx_dest_res.canonical_name)
            ) or any(sfx in new_p for sfx in ["ला", "कडे", "पर्यंत", "स", "तक", "को", "to"]):
                return "dest", old_p, new_p_clean

            # Check if old_p matches current origin or if new_p has origin markers
            orig_res = route_engine.resolve_place(old_p)
            ctx_orig_res = route_engine.resolve_place(ctx.origin or "")
            if (
                ctx.origin
                and (old_p.lower() in ctx.origin.lower() or orig_res.canonical_name == ctx_orig_res.canonical_name)
            ) or any(sfx in new_p for sfx in ["वरून", "पासून", "हून", "कडून", "से", "from"]):
                return "origin", old_p, new_p_clean

            # Default: treat as destination correction if destination exists
            if ctx.destination:
                return "dest", old_p, new_p_clean
            return "origin", old_p, new_p_clean

        # Pattern 2 & 3: Single origin or dest update with explicit leading negation (e.g. 'नाही, मला सातपूरवरून जायचं आहे')
        if m_lead:
            orig_cand = self._extract_single_origin(t_clean)
            if orig_cand:
                return "origin", None, orig_cand

            dest_cand = self._extract_single_destination(t_clean)
            if dest_cand:
                return "dest", None, dest_cand

        return None, None, None

    def _extract_single_destination(self, text: str) -> Optional[str]:
        """Extracts single destination place token when user specifies destination without origin."""
        norm = text.strip()

        # If contains origin markers (from, वरून, पासून, से, हून, कडून), it's not a pure single destination
        if re.search(r'(?:from|पा(?:सू|सु)न|वरून|हून|कडून|से)', norm, re.IGNORECASE):
            return None

        # Marathi patterns:
        m_mr = re.search(r'(?:आता\s+)?(?:मला\s+)?([a-zA-Z0-9\u0900-\u097F\s]+?)(?:ला|कडे|पर्यंत|स)\s*(?:जायचे|जायचं|जायचेय|जायचंय|पोहोचायचे|पोहोचायचं|निघायचे|निघायचं|कसे\s+जावे|जाता\s+येईल|जाऊ)?(?:\s+आहे)?(?:\.|$)', norm, re.IGNORECASE)
        if m_mr:
            cand = self._clean_token(m_mr.group(1))
            if cand and cand not in ["बस", "तिकीट", "पास", "हॉस्पिटल", "रुग्णालय", "कसे", "कुठे"]:
                return cand

        m_mr2 = re.search(r'(?:आता\s+)?(?:मला\s+)?([a-zA-Z0-9\u0900-\u097F\s]+?)\s+(?:जायचे|जायचं|जायचेय|जायचंय|पोहोचायचे|पोहोचायचं|निघायचे|निघायचं)(?:\s+आहे)?(?:\.|$)', norm, re.IGNORECASE)
        if m_mr2:
            cand = self._clean_token(m_mr2.group(1))
            if cand and cand not in ["बस", "तिकीट", "पास", "हॉस्पिटल", "रुग्णालय", "कसे", "कुठे"]:
                return cand

        # Hindi patterns:
        m_hi = re.search(r'(?:अब\s+)?(?:मुझे\s+)?([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:जाना\s+है|पहुंचना\s+है|ले\s+चलो|जाना)(?:\.|$)', norm, re.IGNORECASE)
        if m_hi:
            cand = self._clean_token(m_hi.group(1))
            if cand and cand not in ["बस", "टिकट", "पास", "अस्पताल"]:
                return cand

        # English patterns:
        m_en = re.search(r'(?:now\s+)?(?:take\s+me\s+to|i\s+want\s+to\s+go\s+to|how\s+to\s+reach|how\s+can\s+i\s+get\s+to|travel\s+to|go\s+to|to)\s+([a-zA-Z0-9\s]+?)(?:\s+please)?(?:\.|$)', norm, re.IGNORECASE)
        if m_en:
            cand = self._clean_token(m_en.group(1))
            if cand and cand not in ["bus", "ticket", "pass", "hospital"]:
                return cand

        return None

    def _extract_single_origin(self, text: str) -> Optional[str]:
        """Extracts single origin place token when user specifies origin update without destination."""
        norm = text.strip()

        # Marathi affix patterns: 'मला [Stop] वरून / पासून जायचे आहे' or '[Stop] वरून'
        m_affix = re.search(r'(?:मला\s+)?([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:वरून|पा(?:सू|सु)न|हून|कडून|से)(?:\s+(?:जायचे|जायचं|निघायचे|निघायचं|प्रवास|सुरू|जाना|travel).*)?$', norm, re.IGNORECASE)
        if m_affix:
            cand = self._clean_token(m_affix.group(1))
            if cand and cand not in ["बस", "तिकीट", "पास", "कसे", "कुठे"]:
                return cand

        # Marathi patterns:
        m_mr = re.search(r'(?:आता\s+)?(?:मी|आम्ही)\s+(?:सध्या\s+)?([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:जवळ|येथे|इथे|वरून|थेट)\s*(?:आहे|आहोत|निघतो|निघतोय|आलो|आलोय)?(?:\.|$)', norm, re.IGNORECASE)
        if m_mr:
            cand = self._clean_token(m_mr.group(1))
            if cand and cand not in ["बस", "तिकीट"]:
                return cand

        # Hindi patterns:
        m_hi = re.search(r'(?:अब\s+)?(?:मैं|हम)\s+(?:अभी\s+)?([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:के\s+पास|पर|में|से)\s*(?:हूँ|हैं)?(?:\.|$)', norm, re.IGNORECASE)
        if m_hi:
            cand = self._clean_token(m_hi.group(1))
            if cand and cand not in ["बस", "टिकट"]:
                return cand

        # English patterns:
        m_en = re.search(r'(?:now\s+)?(?:i\s+am|i\x27m|we\s+are)\s+(?:currently\s+|now\s+)?(?:at|near|around|in|from)\s+([a-zA-Z0-9\s]+?)(?:\.|$)', norm, re.IGNORECASE)
        if m_en:
            cand = self._clean_token(m_en.group(1))
            if cand and cand not in ["bus", "ticket"]:
                return cand

        return None

    def _extract_origin_destination(self, text: str) -> Optional[Tuple[str, str]]:
        """Extracts origin and destination place tokens from natural language input."""
        norm = text.strip()
        invalid_dest_particles = {"पण", "का", "ना", "तर", "की", "हो", "नाही", "also", "too", "bus", "बस", "गाडी", "रूट", "ते", "तो", "ती"}

        # Pattern 1: English inverted: "how can I reach / get to / go to / travel to [Dest] from [Orig]"
        m_inv = re.search(r'(?:how\s+(?:can\s+i|to|do\s+i)?\s*)?(?:reach|get\s+to|go\s+to|travel\s+to)\s+([a-zA-Z\s]+?)\s+from\s+([a-zA-Z\s]+)', norm, re.IGNORECASE)
        if m_inv:
            orig = self._clean_token(m_inv.group(2))
            dest = self._clean_token(m_inv.group(1))
            if dest.lower() not in invalid_dest_particles and len(dest) >= 2 and len(orig) >= 2:
                return orig, dest

        # Pattern 2: English standard: "(travel) from [Orig] to [Dest]"
        m_from_to = re.search(r'(?:travel\s+)?from\s+([a-zA-Z0-9\u0900-\u097F\s]+?)\s+to\s+([a-zA-Z0-9\u0900-\u097F\s]+)', norm, re.IGNORECASE)
        if m_from_to:
            orig = self._clean_token(m_from_to.group(1))
            dest = self._clean_token(m_from_to.group(2))
            if dest.lower() not in invalid_dest_particles and len(dest) >= 2 and len(orig) >= 2:
                return orig, dest

        # Pattern 3: Marathi: "[Orig] वरून / पासून / पासुन / हून / कडून (ते/to/टू) [Dest] (ला / कडे / पर्यंत / स) (trailing query/verb)"
        m_mr = re.search(
            r'([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:वरून|पा(?:सू|सु)न|हून|कडून)\s+(?:ते\s+|to\s+|टू\s+)?([a-zA-Z0-9\u0900-\u097F\s]+?)(?:ला|कडे|पर्यंत|स)?(?:\s+(?:जाण्यासाठी|जायचे|जायचं|जायचेय|जायचंय|जावे|जाणार|साठी|जा|पाहिजे|निघा|पोहोच|कसे|कोणती|कोणत्या|कोणता|बस|गाडी|उपलब्ध|आहे|जाते|का|\?|\.|$).*)?$',
            norm,
            re.IGNORECASE
        )
        if m_mr:
            orig = self._clean_token(m_mr.group(1))
            dest = self._clean_token(m_mr.group(2))
            if (
                dest.lower() not in invalid_dest_particles
                and len(dest) >= 2
                and len(orig) >= 2
                and not orig.lower().startswith("ही बस")
                and not orig.lower().startswith("ही गाडी")
            ):
                return orig, dest

        # Pattern 4: Marathi/Hindi: "[Orig] ते/to/टू [Dest]"
        m_te = re.search(r'([a-zA-Z0-9\u0900-\u097F\s]+?)\s+(?:ते|to|टू)\s+([a-zA-Z0-9\u0900-\u097F\s]+?)(?:ला|कडे|पर्यंत|स)?(?:\s+(?:जाण्यासाठी|जायचे|जायचं|जायचेय|जायचंय|जावे|जाणार|साठी|पाहिजे|बस|गाडी|उपलब्ध|आहे|जाते|\?|\.|$).*)?$', norm, re.IGNORECASE)
        if m_te:
            orig = self._clean_token(m_te.group(1))
            dest = self._clean_token(m_te.group(2))
            if dest.lower() not in invalid_dest_particles and len(dest) >= 2 and len(orig) >= 2:
                return orig, dest

        # Pattern 5: Hindi: "[Orig] से [Dest] तक / को / के लिए"
        m_hi = re.search(r'([a-zA-Z0-9\u0900-\u097F\s]+?)\s+से\s+([a-zA-Z0-9\u0900-\u097F\s]+?)\s*(?:तक|को|के लिए|जाना|\.|$)', norm, re.IGNORECASE)
        if m_hi:
            orig = self._clean_token(m_hi.group(1))
            dest = self._clean_token(m_hi.group(2))
            if dest.lower() not in invalid_dest_particles and len(dest) >= 2 and len(orig) >= 2:
                return orig, dest

        return None


# Global singleton
context_engine = ContextEngine()
