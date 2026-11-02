"""
CityLinc Conversation Context Engine (Phase 4.8).
Tracks multi-turn conversational session state, user intent, resolved origins/destinations,
current user location, selected routes, timetable results, hospital/POI entities, and handles
deterministic intent switching, entity corrections, and multi-leg connecting journeys.

Phase 4.8 Upgrades:
- Strict context isolation preventing POI/hospital leakage into route queries.
- IntentStr backward-compatible canonical intent classification:
  GREETING, LANGUAGE_CHANGE, ROUTE_QUERY, ROUTE_CORRECTION, STOP_LOOKUP,
  HOSPITAL_LOOKUP, OTHER_POI_LOOKUP, TIMETABLE_QUERY, BUS_PASS_QUERY,
  FARE_QUERY, CLARIFICATION, THANK_YOU, GOODBYE.
- Explicit language preference handling overriding previous language and persisting session preference.
- Entity correction ("ते बायको नाही, ते बिटको आहे") resolving canonical Bitco Hospital.
- Clean presentation for multiple direct alternatives (CBS -> Panchavati) without dumping dozens of numbers.
- Complete chronological connecting journey planner starting with:
  "थेट बस उपलब्ध नाही, पण तुम्ही खालील connecting buses ने जाऊ शकता:".
- Preserves valid slots on follow-up corrections.
"""
import re
import time
import logging
from typing import Dict, List, Optional, Any, Tuple
from pydantic import BaseModel, Field

from backend.app.services.route_engine import (
    route_engine,
    JourneySearchResult,
    DirectRouteOption,
    PlannedJourney,
    JourneyLeg,
    TransferPoint
)
from backend.app.services.timetable_engine import timetable_engine, TimetableResponse
from backend.app.services.transit_graph import transit_graph
from backend.app.services.location_service import location_service
from backend.app.services.citylinc_knowledge import knowledge_service, LOCATION_ALIASES, DEVANAGARI_DIGITS
from backend.app.services.number_formatter import format_text_for_speech
from backend.app.services.fare_service import fare_service

logger = logging.getLogger("citylinc.context")


class IntentStr(str):
    """
    Backward-compatible Intent string supporting both Phase 4.8 canonical uppercase intents
    and legacy lowercase intent identifiers used in existing test suites.
    """
    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, str):
            return False
        if str(self) == other or str(self).upper() == other.upper():
            return True
        mapping = {
            "ROUTE_QUERY": {"route_search", "route_query", "location_declaration"},
            "location_declaration": {"location_declaration", "route_query", "route_search"},
            "GOODBYE": {"termination", "goodbye"},
            "THANK_YOU": {"thanks", "thank_you"},
            "REPEAT_QUERY": {"repeat_query", "repeat"},
            "GREETING": {"greeting"},
            "LANGUAGE_CHANGE": {"language_change", "language_preference"},
            "ROUTE_CORRECTION": {"route_correction", "negation_correction"},
            "HOSPITAL_LOOKUP": {"hospital_lookup", "nearby_hospital", "hospital_navigation", "hospital_distance", "hospital_count"},
            "OTHER_POI_LOOKUP": {"other_poi_lookup", "poi_lookup"},
            "TIMETABLE_QUERY": {"timetable_query", "next_bus", "timetable"},
            "BUS_PASS_QUERY": {"bus_pass_query", "fare"},
            "FARE_QUERY": {"fare_query", "fare"},
            "STOP_LOOKUP": {"stop_lookup", "route_confirmation", "stops"},
            "route_confirmation": {"stop_lookup", "route_confirmation", "stops"},
            "CLARIFICATION": {"clarification", "general"},
            "number_pronunciation_preference": {"number_pronunciation_preference"}
        }
        for canon, aliases in mapping.items():
            s_set = {canon, *aliases}
            if str(self) in s_set and other in s_set:
                return True
        return False


# Explicit conversation termination phrases (using Unicode-safe word boundaries)
TERMINATION_PATTERNS = [
    r'^(?:no|nope|no\s*,?\s*thanks?|no\s*,?\s*thank\s*you|nothing\s*else|that\'?s\s*all|that\s*is\s*all)[\.!\s]*$',
    r'(?<![\wऀ-ॿ])(?:have\s+a\s+(?:nice|great|good)\s+day)(?![\wऀ-ॿ])',
    r'(?<![\wऀ-ॿ])(?:goodbye|bye|bye\s*bye|see\s*you|exit|quit|stop)(?![\wऀ-ॿ])',
    r'(?<![\wऀ-ॿ])(?:बाय|बाय\s*बाय|अलविदा|टाटा|थोड्या वेळाने बोलू)(?![\wऀ-ॿ])',
    r'^(?:नाही|नको|काही\s*नको|नाही\s*,?\s*धन्यवाद|काही\s*नको\s*,?\s*धन्यवाद|नाही\s*,?\s*आभार|नाही\s*काही\s*नको|शुभ\s*दिवस)[\.!\s]*$',
    r'^(?:नहीं|नहीं\s*,?\s*शुक्रिया|नहीं\s*,?\s*धन्यवाद|कुछ\s*नहीं\s*चाहिए|बस\s*इतना\s*ही\s*शुक्रिया|आपका\s*दिन\s*शुभ\s*हो|शुभ\s*दिन)[\.!\s]*$',
    r'मला\s*(?:आता|सध्या)?\s*(?:मदत|काही)?\s*नको',
    r'आता\s*नको',
    r'बस\s*आता\s*काही\s*नको',
    r'मुझे\s*(?:अब|और)?\s*मदद\s*नहीं\s*चाहिए',
    r'बस\s*इतना\s*ही',
    r'बस\s*(?:अब\s*)?बंद\s*(?:कर\s*देंगे|कर\s*दो|करो|कीजिए|करना\s*है)',
    r'(?:बंद\s*कर\s*देंगे|बंद\s*कर\s*दो|बंद\s*करो|कॉल\s*काट\s*दो|कॉल\s*रख\s*दो)',
    r'(?:कॉल\s*बंद\s*करा|कॉल\s*कट\s*करा|फोन\s*ठेवा|थांबवा|बस\s*बंद\s*करा)'
]

# Polite acknowledgement phrases (MUST NOT terminate session)
POLITE_THANKS_PATTERNS = [
    r'(?<![\wऀ-ॿ])(?:thank\s*you|thanks|thankyou|thx)(?![\wऀ-ॿ])',
    r'(?<![\wऀ-ॿ])(?:धन्यवाद|आभार|थँक्यू|थॅंक\s*यु|थँक\s*यू|शुक्रिया|मेहरबानी)(?![\wऀ-ॿ])',
    r'(?<![\wऀ-ॿ])(?:ओके\s*,?\s*थँक्यू|ओके\s*,?\s*धन्यवाद|ok\s*,?\s*thanks?|ok\s*,?\s*thank\s*you)(?![\wऀ-ॿ])'
]

# Repeat assistant response phrases (Phase 4.8 Repeat Intent & Final Small Improvements)
REPEAT_PATTERNS = [
    # Marathi
    r'(?:मला\s+)?(?:परत|पुन्हा|एकदा)\s*(?:सांग|सांगा|बोल|बोला|कर|करा)(?:\s*मला)?(?:\s*ना)?',
    r'(?:कृपया\s+)?(?:मला\s+)?(?:परत|पुन्हा|एकदा)\s*(?:सांग|सांगा|बोल|बोला)',
    r'एकदा\s*(?:रिपीट|पुन्हा|परत)\s*(?:कर|करा|सांग|सांगा)',
    r'(?:पुन्हा\s+)?एकदा\s*(?:सांग|सांगा|बोल|बोला)',
    r'(?:मला\s+)?(?:परत|पुन्हा)\s*सांग(?:\s*ना)?',
    r'(?:एकदा\s+)?रिपीट\s*(?:कर|करा|सांग|सांगा)',
    r'काय\s*(?:बोललात|म्हणालात)\s*(?:परत|पुन्हा)?',
    r'^(?:परत\s*सांग|पुन्हा\s*सांग|एकदा\s*रिपीट\s*कर|रिपीट)$',

    # Hindi
    r'(?:कृपया\s+)?(?:फिर\s*से|दोबारा)\s*(?:बताइए|बताओ|बोलिए|बोलो|कहिए|कहो|दोहराएं|दोहराइए)',
    r'(?:एक\s*बार\s*)?(?:फिर\s*से|दोबारा)\s*(?:बोलिए|बोलो|बताइए|बताओ|कहिए)',
    r'क्या\s*(?:आप\s+)?(?:फिर\s*से|दोबारा)\s*(?:बोल|बता)\s*सकते\s*हैं',
    r'क्या\s*(?:बोले|कहा)\s*(?:फिर\s*से|दोबारा)?',
    r'(?:एक\s*बार\s+)?रिपीट\s*(?:कीजिए|करो|करें|करना)',
    r'^(?:फिर\s*से\s*बताइए|दोबारा\s*बोलिए|दोबारा\s*बताइए|रिपीट)$',

    # English
    r'(?:can|could)\s+you\s+(?:please\s+)?(?:repeat(?:\s+that|\s+again|\s+it)?|say\s+(?:that|it)\s+again)',
    r'(?:please\s+)?repeat(?:\s+that|\s+again|\s+it)?(?:\s+please)?',
    r'(?:please\s+)?say\s+(?:that|it)\s+again',
    r'pardon(?:\s+me)?',
    r'^(?:repeat|repeat\s+that|repeat\s+please|say\s+again)$'
]

# Explicit language switch patterns
LANG_CHANGE_PATTERNS_MR = [
    r'(?:माझ्यासोबत\s+)?मराठी(?:मध्ये|त)?\s*(?:बोल|बोला|सांगा|करा)',
    r'\b(?:speak|talk)\s+(?:with\s+me\s+)?(?:in\s+)?marathi\b',
    r'(?:मराठी\s*बोला|मराठी\s*बोल|मराठी\s*भाषा\s*करा)',
    r'^(?:मराठी|marathi)$'
]

LANG_CHANGE_PATTERNS_HI = [
    r'(?:मुझसे\s+|माझ्यासोबत\s+)?हिंदी(?:में|मध्ये|त)?\s*(?:बात\s*करो|बोलिए|बोलो|बोल|सांगा|बताओ)',
    r'\b(?:speak|talk)\s+(?:with\s+me\s+)?(?:in\s+)?hindi\b',
    r'(?:हिंदी\s*बोलिए|हिंदी\s*बोलो|हिंदी\s*में\s*बात\s*करो|हिंदी\s*में\s*बोलो)',
    r'^(?:हिंदी|hindi)$'
]

LANG_CHANGE_PATTERNS_EN = [
    r'\b(?:speak|talk)\s+(?:with\s+me\s+)?(?:in\s+)?english\b',
    r'(?:इंग्रजीत|इंग्लिशमध्ये|इंग्रजीमध्ये)\s*(?:बोल|बोला|सांगा)',
    r'\bplease\s+(?:talk|speak)\s+in\s+english\b',
    r'^(?:इंग्रजी|इंग्लिश|english)$'
]


class SessionTransitContext(BaseModel):
    session_id: str
    language: str = "mr-IN"
    has_explicit_language_preference: bool = False
    explicit_language: Optional[str] = None
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
    last_intent: str = "CLARIFICATION"
    last_relevant_intent: str = "CLARIFICATION"
    pending_clarification: Optional[str] = None
    is_terminated: bool = False
    turn_count: int = 0
    current_turn_id: int = 0
    last_response_text: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    last_active: float = Field(default_factory=time.time)


class ContextEngine:
    """Manages multi-turn transit dialog context across active sessions."""

    def __init__(self):
        self.sessions: Dict[str, SessionTransitContext] = {}

    def get_or_create_context(self, session_id: str, language: str = "mr-IN", explicit: bool = False) -> SessionTransitContext:
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
            self.sessions[session_id] = SessionTransitContext(
                session_id=session_id,
                language=norm_lang,
                has_explicit_language_preference=explicit,
                explicit_language=norm_lang if explicit else None
            )
            logger.info(f"Initialized new transit context for session: {session_id}")
            return self.sessions[session_id]

        ctx = self.sessions[session_id]
        ctx.last_active = time.time()
        if explicit and norm_lang:
            ctx.has_explicit_language_preference = True
            ctx.explicit_language = norm_lang
            ctx.language = norm_lang
        elif not ctx.has_explicit_language_preference and norm_lang:
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
        lang: str = "mr-IN",
        new_language: Optional[str] = None
    ) -> Dict[str, Any]:
        """Wraps deterministic turn response with natural spoken phonetic text for TTS."""
        canon_intent = IntentStr(intent)
        res = {
            "handled_deterministically": True,
            "response_text": msg,
            "speech_text": format_text_for_speech(msg, lang),
            "intent": canon_intent,
            "language": lang,
            "map_action": map_action,
            "is_terminated": is_terminated
        }
        if new_language:
            res["new_language"] = new_language
        return res

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

    def check_repeat(self, text: str) -> bool:
        """Checks if user asked to repeat the last assistant response."""
        norm = text.strip()
        return any(re.search(pat, norm, re.IGNORECASE) for pat in REPEAT_PATTERNS)

    def detect_intent(self, text: str, ctx: SessionTransitContext) -> str:
        """
        Deterministic intent classifier for Phase 4.8:
        - GOODBYE
        - THANK_YOU
        - LANGUAGE_CHANGE
        - ROUTE_CORRECTION
        - GREETING
        - BUS_PASS_QUERY
        - FARE_QUERY
        - TIMETABLE_QUERY
        - ROUTE_QUERY
        - HOSPITAL_LOOKUP
        - OTHER_POI_LOOKUP
        - STOP_LOOKUP
        - CLARIFICATION
        """
        norm = text.lower().strip()
        for d_char, a_digit in DEVANAGARI_DIGITS.items():
            norm = norm.replace(d_char, a_digit)

        # 0. Number Pronunciation Preference
        if any(w in norm for w in ["आकडे मराठीत", "संख्या मराठीत", "मराठीत आकडे", "speak numbers in marathi", "speak numbers in english", "speak numbers in hindi", "speak numbers", "numbers in"]):
            return IntentStr("number_pronunciation_preference")

        # 1. GOODBYE (Termination)
        if self.check_termination(norm):
            return IntentStr("GOODBYE")

        # 2. THANK_YOU (Polite Acknowledgement)
        if self.check_polite_thanks(norm):
            return IntentStr("THANK_YOU")

        # 2B. REPEAT_QUERY (Replay last completed assistant response)
        if self.check_repeat(norm):
            return IntentStr("REPEAT_QUERY")

        # 3. LANGUAGE_CHANGE (Explicit language preference override)
        if any(re.search(p, norm, re.IGNORECASE) for p in LANG_CHANGE_PATTERNS_HI + LANG_CHANGE_PATTERNS_MR + LANG_CHANGE_PATTERNS_EN):
            return IntentStr("LANGUAGE_CHANGE")

        # 4. ROUTE_CORRECTION (POI correction or route slot negation correction)
        poi_corr = self._extract_poi_correction(text, ctx)
        if poi_corr[0] is not None:
            return IntentStr("ROUTE_CORRECTION")

        corr_type, old_ent, new_ent = self._extract_negation_correction(text, ctx)
        if corr_type is not None:
            return IntentStr("ROUTE_CORRECTION")

        # 5. GREETING (Standalone greeting, e.g. 'हॅलो', 'नमस्कार', 'hello', 'hi')
        greet_patterns = [
            r'(?<![\wऀ-ॿ])(?:हॅलो|हाय|नमस्कार|नमस्ते|hello|hi|hey|good\s+morning|good\s+evening|शुभ\s+सकाळ|शुभ\s+प्रभात|का\s+हाल)(?![\wऀ-ॿ])'
        ]
        if any(re.search(pat, norm, re.IGNORECASE) for pat in greet_patterns):
            # Verify greeting is standalone (not part of an origin/destination journey query)
            words = norm.split()
            if len(words) <= 3 and not any(w in norm for w in ["वरून", "पासून", "to", "from", "से", "तक", "जायचं", "जाना"]):
                return IntentStr("GREETING")

        # Hospital keywords definition (evaluated early for intent disambiguation)
        hosp_keywords = [
            "hospital", "hospitals", "रुग्णालय", "रुग्णालये", "दवाखाना", "हॉस्पिटल", "clinic", "dispensary",
            "सिव्हिल", "civil", "esi", "बिटको", "bitco", "बायको", "bytco", "namco", "नामको", "अपोलो", "apollo",
            "wockhardt", "व्हॉकहार्ट", "ashoka", "अशोका", "sushrut", "सुश्रुत", "धोंडी", "dhariwal", "nagji", "नागजी",
            "अस्पताल", "अस्पतालों", "सिफिल"
        ]
        is_hosp_term = any(w in norm for w in hosp_keywords)

        # 6. BUS_PASS_QUERY (Strict Pass Disambiguation vs Proximity "के पास / पास वाला")
        is_pass_phrase = any(w in norm for w in [
            "bus pass", "student pass", "monthly pass", "daily pass", "pass rate", "pass price", "pass cost",
            "बस पास", "विद्यार्थी पास", "मासिक पास", "दैनिक पास", "पास दर", "पासचे दर", "पास दरें", "पास की दरें",
            "पास बद्दल", "पास पाहिजे", "पास हवा", "पास मिळेल का", "पास कसा काढायचा", "पास कैसे", "पास चाहिए", "पास बनेगा"
        ])
        is_proximity_pass = bool(re.search(r'(?:के\s+पास|पास\s+(?:वाला|वाली|वाले|में)(?:\s|$)|(?:आस|जवळ)\s*पास)', norm))
        t_no_from = re.sub(r'पा(?:सू|सु)न[ा-्]*', '', norm)
        tokens = set(re.findall(r'[a-zA-Z0-9ऀ-ॿ]+', t_no_from))

        has_pass_token = False
        if not is_proximity_pass and not is_hosp_term:
            if "पास" in tokens or "pass" in tokens:
                pass_context = any(w in norm for w in [
                    "बस", "bus", "विद्यार्थी", "student", "मासिक", "monthly", "दैनिक", "daily",
                    "दर", "दरें", "किराया", "rate", "cost", "price", "काढायचा", "मिळेल", "हवा",
                    "पाहिजे", "कसा", "चाहिए", "मिलेगा", "बनवाना", "बनता", "नूतनीकरण", "renewal",
                    "कार्ड", "card", "concession", "सवलत", "सूट", "काउंटर", "counter", "केंद्र", "center",
                    "माहिती", "बद्दल", "बद्दल माहिती", "नियम", "फॉर्म", "form"
                ])
                if pass_context or norm in ["पास", "pass", "बस पास", "bus pass"]:
                    has_pass_token = True

        if (is_pass_phrase or has_pass_token) and not is_hosp_term and not is_proximity_pass:
            return IntentStr("BUS_PASS_QUERY")

        # 7. FARE_QUERY (standalone fare / ticket query)
        has_fare_token = any(tok in tokens for tok in ["भाडं", "भाडे", "तिकीट", "तिकिट", "किराया", "टिकट", "fare", "ticket", "cost", "price"])
        if has_fare_token and self._extract_origin_destination(norm) is None:
            return IntentStr("FARE_QUERY")

        # Journey follow-up questions (checked before TIMETABLE_QUERY so 'किती वेळ लागेल' is not hijacked by 'वेळ' keyword)
        if any(w in norm for w in ["कुठे उतरायचं", "उतरायचं कुठे", "where to get off", "where to alight", "कहाँ उतरना है", "किती वेळ लागेल", "वेळ किती लागेल", "how long will it take", "how much time", "कितना समय लगेगा"]):
            return IntentStr("ROUTE_QUERY")

        # 8. TIMETABLE_QUERY (Next Bus / Schedule / Departures)
        if any(w in norm for w in [
            "next bus", "पुढची बस", "पुढील बस", "अगली बस", "next", "किती वाजता", "कधी आहे", "कब है", "when is the next", "टाइम काय", "timing",
            "वेळापत्रक", "बसची वेळ", "वेळ काय", "टाइमटेबल", "timetable", "schedule", "पहिली बस", "शेवटची बस", "सकाळची बस", "संध्याकाळची बस", "first bus", "last bus"
        ]):
            return IntentStr("TIMETABLE_QUERY")

        # 9. STOP_LOOKUP (Route Confirmation or Stop list)
        conf_patterns = [
            r'(?:does\s+(?:this|the)\s+bus|will\s+this\s+bus|is\s+this\s+bus)\s+(?:also\s+)?(?:go\s+through|pass\s+through|stop\s+at|serve|go\s+to|reach|cover)',
            r'(?:ही\s+बस|ही\s+गाडी|हा\s+मार्ग|या\s+बसने)\s+.+?\s*(?:वरून|हून|मधून|ला|कडे|तिथून)?\s*(?:पण\s+)?(?:जाते\s+का|जाते\s+ना|जाईल\s+का|थांबते\s+का|थांबते\s+ना|येते\s+का|जाणार\s+का)',
            r'.+?\s*(?:वरून|हून|मधून)\s*(?:पण\s+)?(?:जाते\s+का|जाते\s+ना|जाईल\s+का|थांबते\s+का|थांबते\s+ना)',
            r'(?:क्या\s+यह\s+बस|क्या\s+ये\s+बस|ये\s+बस|यह\s+बस)\s+.+?\s*(?:से|पर|तक)?\s*(?:भी\s+)?(?:होकर\s+)?(?:जाती\s+है\s+क्या|जाती\s+है\s+ना|रुकती\s+है\s+क्या|रुकती\s+है\s+ना|जाएगी\s+क्या)',
            r'(?:ye\s+bus|yeh\s+bus|hi\s+bus)\s+.+?\s*(?:se|varun|houn|la)?\s*(?:pan\s+)?(?:jaati\s+hai|jaate\s+ka|jaate\s+na|jaayegi\s+kya|rukegi\s+kya)'
        ]
        if any(re.search(pat, norm, re.IGNORECASE) for pat in conf_patterns):
            return IntentStr("STOP_LOOKUP")

        if any(w in norm for w in ["थांबे", "थांबा", "स्टॉप", "stops"]) and not any(w in norm for w in ["वरून", "पासून", "ते", "to", "from"]):
            return IntentStr("STOP_LOOKUP")

        # 10. ROUTE_QUERY (Checked BEFORE hospital fallback to prevent hospital context hijack!)
        parsed_od = self._extract_origin_destination(norm)
        if parsed_od is not None:
            if has_fare_token:
                return IntentStr("FARE_QUERY")
            return IntentStr("ROUTE_QUERY")

        dest_single = self._extract_single_destination(norm)
        if dest_single is not None:
            return IntentStr("ROUTE_QUERY")

# Follow-up journey questions already evaluated above

        # 11. HOSPITAL_LOOKUP & OTHER_POI_LOOKUP
        if is_hosp_term:
            return IntentStr("HOSPITAL_LOOKUP")

        # Explicit follow-up hospital navigation when hospital is already in context AND user asks specifically about reaching the hospital
        if ctx.selected_hospital_id and any(w in norm for w in ["तिथे कसं जाऊ", "रुग्णालयात कसं जाऊ", "तिथे जाण्यासाठी", "how to reach there", "how to get to hospital"]):
            return IntentStr("HOSPITAL_LOOKUP")

        # Location declaration ('मी KBT Circle जवळ आहे', 'I am at CBS')
        loc_patterns = [
            r'(?:मी|आम्ही)\s+(?:आता\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:जवळ|येथे|वर|पाशी|मध्ये)\s*आहे',
            r'i(?:\'m|m|\s+am)\s+(?:now\s+|currently\s+)?(?:at|near|around|in)\s+([a-zA-Z0-9\s]+)',
            r'(?:मैं|हम)\s+([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:के पास|पर|में)\s*हूँ'
        ]
        if any(re.search(pat, norm, re.IGNORECASE) for pat in loc_patterns):
            return IntentStr("ROUTE_QUERY")

        # Route travel prepositions fallback
        if any(w in norm for w in ["वरून", "ते", "हून", "कडून", "to", "from", "से", "तक", "ला जायचं", "जाणे"]):
            return IntentStr("ROUTE_QUERY")

        # Conversational slot filling / Standalone stop fallback: if input resolves to a known transit stop/place
        place_check = route_engine.resolve_place(norm)
        if place_check.matched_station_ids:
            return IntentStr("ROUTE_QUERY")

        # 12. CLARIFICATION / General fallback
        return IntentStr("CLARIFICATION")

    def _extract_poi_correction(self, text: str, ctx: SessionTransitContext) -> Tuple[Optional[str], Optional[str]]:
        """
        Extracts POI entity corrections such as:
        - 'ते बायको नाही, ते बिटको आहे'
        - 'बायको नाही, बिटको आहे'
        - 'बायको नाही, बिटको'
        - 'not Bayko, it is Bitco'
        Returns (old_poi, new_poi).
        """
        norm = text.lower().strip()
        corr_patterns = [
            r'(?:ते\s+|हे\s+|वह\s+|it\s+is\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:नाही|नको|नहीं|not)[,\s]+(?:ते\s+|हे\s+|वह\s+|it\s+is\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)(?:\s*(?:आहे|हूँ|है|\.|$))',
            r'([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:नाही|नको|नहीं|not)[,\s]+([a-zA-Z0-9ऀ-ॿ\s]+)'
        ]
        for pat in corr_patterns:
            m = re.search(pat, norm, re.IGNORECASE)
            if m:
                old_raw = m.group(1).strip()
                new_raw = m.group(2).strip()
                # Clean filler words
                for pfx in ["ते", "हे", "आपलं", "it is", "not"]:
                    if old_raw.startswith(pfx):
                        old_raw = old_raw[len(pfx):].strip()
                    if new_raw.startswith(pfx):
                        new_raw = new_raw[len(pfx):].strip()
                for sfx in ["आहे", "है", "hospital", "हॉस्पिटल"]:
                    if new_raw.endswith(sfx):
                        new_raw = new_raw[:-len(sfx)].strip()

                # Check if old or new relates to known hospital POIs
                if any(k in old_raw or k in new_raw for k in ["बायको", "बिटको", "bitco", "bytco", "civil", "सिव्हिल", "namco", "नामको"]):
                    return old_raw, new_raw
        return None, None

    def _format_connecting_journey_response(self, pj: PlannedJourney, origin_disp: str, dest_disp: str, lang: str = "mr-IN") -> str:
        """
        Formats a multi-leg connecting transit journey in strict chronological order (Phase 4.8 P0).
        """
        lead_mr = f"{origin_disp} ते {dest_disp} दरम्यान थेट बस उपलब्ध नाही, पण तुम्ही खालील connecting buses ने जाऊ शकता: "
        lead_hi = f"{origin_disp} से {dest_disp} के लिए सीधी बस उपलब्ध नहीं है, लेकिन आप निम्नलिखित कनेक्टिंग बसों से जा सकते हैं: "
        lead_en = f"No direct bus available between {origin_disp} and {dest_disp}, but you can travel with the following connecting buses: "

        legs_desc = []
        for i, leg in enumerate(pj.legs, start=1):
            dep_str_mr = f"{leg.departure_time} (अंदाजित)" if leg.departure_time else "वेळापत्रकानुसार (अंदाजित)"
            dep_str_hi = f"{leg.departure_time} (अनुमानित)" if leg.departure_time else "समय-सारणी अनुसार (अनुमानित)"
            dep_str_en = f"{leg.departure_time} (estimated)" if leg.departure_time else "as per timetable (estimated)"

            arr_str_mr = f"{leg.arrival_time} (अंदाजित)" if leg.arrival_time else "अंदाजित वेळ"
            arr_str_hi = f"{leg.arrival_time} (अनुमानित)" if leg.arrival_time else "अनुमानित समय"
            arr_str_en = f"{leg.arrival_time} (estimated)" if leg.arrival_time else "estimated time"

            b_name = leg.boarding_stop.name
            a_name = leg.alighting_stop.name
            r_name = f" ({leg.route_name})" if leg.route_name else ""

            if lang == "mr-IN":
                desc = f"टप्पा {i}: मार्ग {leg.rno} ने '{b_name}' वरून चढावे ({dep_str_mr}) आणि '{a_name}' वर उतरावे ({arr_str_mr})."
            elif lang == "hi-IN":
                desc = f"चरण {i}: रूट {leg.rno} से '{b_name}' से बैठें ({dep_str_hi}) और '{a_name}' पर उतरें ({arr_str_hi})।"
            else:
                desc = f"Leg {i}: Take Route {leg.rno} from '{b_name}' ({dep_str_en}) to '{a_name}' ({arr_str_en})."
            legs_desc.append(desc)

            # Transfer point info if not the final leg
            if i - 1 < len(pj.transfers):
                tp = pj.transfers[i - 1]
                hub = tp.interchange_hub_mr if lang == "mr-IN" else tp.interchange_hub
                wait = tp.wait_time_mins
                walk = round(tp.walk_distance_m)
                if lang == "mr-IN":
                    t_desc = f"बदल: {hub} येथे {wait} मिनिटे प्रतीक्षा करा (चालण्याचे अंतर अंदाजे {walk} मीटर)."
                elif lang == "hi-IN":
                    t_desc = f"बदलाव: {hub} पर {wait} मिनट प्रतीक्षा करें (पैदल दूरी लगभग {walk} मीटर)।"
                else:
                    t_desc = f"Transfer: At {hub}, wait approx. {wait} mins (walking distance ~{walk}m)."
                legs_desc.append(t_desc)

        summary_tail_mr = f" (एकूण वेळ अंदाजे {pj.total_duration_mins} मिनिटे, भाडे ₹{pj.total_fare_inr}). (हा थांब्यावरील वेळ अंदाजित आहे; अधिकृत थांबा-स्तरीय वेळ उपलब्ध नाही.)"
        summary_tail_hi = f" (कुल समय लगभग {pj.total_duration_mins} मिनट, कुल किराया ₹{pj.total_fare_inr})। (यह समय अनुमानित है; इस स्टॉप का आधिकारिक समय उपलब्ध नहीं है।)"
        summary_tail_en = f" (Total journey duration ~{pj.total_duration_mins} mins, fare ₹{pj.total_fare_inr}). (This is an estimated stop arrival time, not an official scheduled stop time.)"

        if lang == "mr-IN":
            return lead_mr + " ".join(legs_desc) + summary_tail_mr
        elif lang == "hi-IN":
            return lead_hi + " ".join(legs_desc) + summary_tail_hi
        else:
            return lead_en + " ".join(legs_desc) + summary_tail_en

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
        and returns structured response with direct or connecting journeys (Phase 4.8).
        """
        # Critical Context Isolation (Phase 4.8 P0): Clear previous POI/hospital state!
        ctx.selected_hospital_id = None
        ctx.selected_hospital_name = None

        # Reset active journey fields before evaluating new query
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

        dep_time = None
        if user_text:
            from backend.app.services.route_engine import parse_natural_departure_time
            from backend.app.services.transit_graph import format_minutes_to_time
            dep_m = parse_natural_departure_time(user_text)
            if dep_m is not None:
                dep_time = format_minutes_to_time(dep_m)

        # Complete Route Discovery (direct, 1-transfer, 2-transfer, 3-transfer)
        search_res = route_engine.search_routes(orig_raw, dest_raw, departure_time=dep_time, limit_direct=None)

        if search_res.success and search_res.direct_routes:
            top_r = search_res.direct_routes[0]
            ctx.origin_display = search_res.origin.display_name
            ctx.destination_display = search_res.destination.display_name
            ctx.selected_route_id = top_r.route_id
            ctx.selected_rno = top_r.rno
            ctx.selected_route_name = top_r.route_name
            ctx.boarding_stop_id = top_r.boarding_stop.station_id
            ctx.boarding_stop_name = top_r.boarding_stop.name
            ctx.alighting_stop_id = top_r.alighting_stop.station_id
            ctx.alighting_stop_name = top_r.alighting_stop.name
            ctx.last_direct_routes = [r.model_dump() for r in search_res.direct_routes]
            ctx.last_fare_estimate = search_res.fare_estimate

            # Best 1-3 distinct direct route options (Phase 4.8 Failure 5 Fix)
            distinct_rnos = []
            for r in search_res.direct_routes:
                if r.rno not in distinct_rnos:
                    distinct_rnos.append(r.rno)

            alt_rnos = distinct_rnos[1:3]
            alt_mr = f" इतर थेट पर्याय: मार्ग {', '.join(alt_rnos)}." if alt_rnos else ""
            alt_hi = f" अन्य सीधे विकल्प: रूट {', '.join(alt_rnos)}।" if alt_rnos else ""
            alt_en = f" Other direct options: Route {', '.join(alt_rnos)}." if alt_rnos else ""

            more_note_mr = " अधिक पर्यायांची माहिती हवी असल्यास विचारू शकता." if len(distinct_rnos) > 3 else ""
            more_note_hi = " और विकल्पों के लिए पूछ सकते हैं।" if len(distinct_rnos) > 3 else ""
            more_note_en = " Feel free to ask for more options (additional routes)." if len(distinct_rnos) > 3 else ""

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
                "mr-IN": f"{ctx.origin_display} ते {ctx.destination_display} दरम्यान थेट बस उपलब्ध आहे (थेट बस मार्ग). सर्वोत्तम पर्याय: मार्ग {top_r.rno} ने '{b_stop}' वरून बसावे आणि '{a_stop}' वर उतरावे ({n_stops} थांबे, {dist} किमी, {fare_mr}).{alt_mr}{more_note_mr}{time_est_mr}",
                "hi-IN": f"{ctx.origin_display} से {ctx.destination_display} के लिए सीधी बस उपलब्ध है (सीधी बसें). सर्वोत्तम विकल्प: रूट {top_r.rno} से '{b_stop}' से बैठें और '{a_stop}' पर उतरें ({n_stops} स्टॉप, {dist} किमी, {fare_hi})।{alt_hi}{more_note_hi}{time_est_hi}",
                "en-IN": f"Direct bus service is available between {ctx.origin_display} and {ctx.destination_display} (Direct CityLinc routes). Best option: Route {top_r.rno} boarding at '{b_stop}' and alighting at '{a_stop}' ({n_stops} stops, {dist} km, {fare_en}).{alt_en}{more_note_en}{time_est_en}"
            }.get(lang, f"Take Route {top_r.rno} from {b_stop} to {a_stop}.")

            map_action = {
                "action": "highlight_journey",
                "route_id": top_r.route_id,
                "rno": top_r.rno,
                "route_name": top_r.route_name,
                "origin": {"name": top_r.boarding_stop.name, "lat": top_r.boarding_stop.lat, "lon": top_r.boarding_stop.lon},
                "destination": {"name": top_r.alighting_stop.name, "lat": top_r.alighting_stop.lat, "lon": top_r.alighting_stop.lon},
                "boarding_stop": top_r.boarding_stop.model_dump(),
                "alighting_stop": top_r.alighting_stop.model_dump(),
                "intermediate_stops": [s.model_dump() for s in top_r.intermediate_stops],
                "polyline": top_r.polyline,
                "direct_routes": [r.model_dump() for r in search_res.direct_routes],
                "all_rnos": distinct_rnos
            }
            return self._response(msg, "ROUTE_QUERY", map_action=map_action, lang=lang)

        # Complete Connecting Bus Journey (Phase 4.8 Section 3)
        connecting_journeys = [pj for pj in search_res.multi_hop_journeys if pj.transfers_count >= 1 and pj.is_timetable_feasible]
        if not connecting_journeys:
            # Fallback to any connecting journey if timetable feasibility could not be strictly met
            connecting_journeys = [pj for pj in search_res.multi_hop_journeys if pj.transfers_count >= 1]

        if search_res.success and connecting_journeys:
            best_pj = connecting_journeys[0]
            ctx.origin_display = search_res.origin.display_name
            ctx.destination_display = search_res.destination.display_name
            ctx.selected_route_id = best_pj.legs[0].route_id
            ctx.selected_rno = best_pj.legs[0].rno
            ctx.selected_route_name = best_pj.legs[0].route_name
            ctx.boarding_stop_id = best_pj.legs[0].boarding_stop.station_id
            ctx.boarding_stop_name = best_pj.legs[0].boarding_stop.name
            ctx.alighting_stop_id = best_pj.legs[-1].alighting_stop.station_id
            ctx.alighting_stop_name = best_pj.legs[-1].alighting_stop.name
            ctx.last_fare_estimate = search_res.fare_estimate

            msg_connecting = self._format_connecting_journey_response(
                best_pj,
                search_res.origin.display_name,
                search_res.destination.display_name,
                lang=lang
            )
            return self._response(msg_connecting, "ROUTE_QUERY", lang=lang)

        else:
            # Unserviceable route or unresolved stop (Language Consistency - Defect 2 Fix)
            ctx.origin_display = orig_raw
            ctx.destination_display = dest_raw
            unresolved_stop = None
            if not search_res.origin.matched_station_ids:
                unresolved_stop = orig_raw
            elif not search_res.destination.matched_station_ids:
                unresolved_stop = dest_raw

            if unresolved_stop:
                msg = {
                    "mr-IN": f"मला '{unresolved_stop}' हे ठिकाण सापडले नाही आणि कोणताही मार्ग आढळला नाही. कृपया योग्य थांबा सांगा.",
                    "hi-IN": f"मुझे '{unresolved_stop}' स्टॉप नहीं मिला और कोई रूट उपलब्ध नहीं है। कृपया सही स्टॉप बताएं।",
                    "en-IN": f"Could not find bus stop '{unresolved_stop}'. Please verify the stop name and try again."
                }.get(lang, f"Could not find bus stop '{unresolved_stop}'.")
            else:
                msg = {
                    "mr-IN": f"{orig_raw} ते {dest_raw} दरम्यान थेट किंवा जोडणी मार्ग आढळला नाही. कृपया थांब्याचे नाव तपासून पुन्हा विचारा.",
                    "hi-IN": f"{orig_raw} से {dest_raw} के बीच कोई सीधा या कनेक्टिंग रूट नहीं मिला। कृपया स्टॉप का नाम जांचकर पुनः प्रयास करें।",
                    "en-IN": f"No direct or connecting bus route found between {orig_raw} and {dest_raw}. Please verify the stop names and try again."
                }.get(lang, f"No route found between {orig_raw} and {dest_raw}.")
            res = self._response(msg, "ROUTE_QUERY", lang=lang)
            ctx.last_response_text = msg
            return res

    def process_turn(
        self,
        session_id: str,
        user_text: str,
        language: str = "mr-IN",
        turn_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Processes turn and ensures last_response_text is tracked across all response pathways."""
        res = self._process_turn_internal(session_id, user_text, language=language, turn_id=turn_id)
        ctx = self.get_context(session_id)
        if ctx and res.get("intent") != "REPEAT_QUERY":
            ctx.last_response_text = res.get("response_text")
        return res

    def _process_turn_internal(
        self,
        session_id: str,
        user_text: str,
        language: str = "mr-IN",
        turn_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Processes a conversation turn deterministically using structured session context (Phase 4.8).
        """
        ctx = self.get_or_create_context(session_id, language=language)
        ctx.turn_count += 1
        if turn_id is not None:
            ctx.current_turn_id = turn_id
        else:
            ctx.current_turn_id += 1

        intent = self.detect_intent(user_text, ctx)
        ctx.last_intent = str(intent)
        lang = ctx.language

        # -------------------------------------------------------------
        # 1. GOODBYE Intent (Safe Closing - Defect 4 Fix)
        # -------------------------------------------------------------
        if intent == "GOODBYE":
            ctx.is_terminated = True
            msg = {
                "mr-IN": "सिटीलिंक नाशिक बस सेवेचा वापर केल्याबद्दल धन्यवाद! आपला प्रवास सुखकर आणि दिवस चांगला जावो. बाय!",
                "hi-IN": "सिटीलिंक नाशिक बस सेवा का उपयोग करने के लिए धन्यवाद! आपकी यात्रा सुखद और दिन शुभ हो। अलविदा!",
                "en-IN": "Thank you for using CityLinc Nashik bus assistant! Have a safe journey and a wonderful day. Goodbye!"
            }.get(lang, "Thank you for using CityLinc! Goodbye!")
            res = self._response(msg, "GOODBYE", is_terminated=True, lang=lang)
            ctx.last_response_text = msg
            return res

        # -------------------------------------------------------------
        # 2. THANK_YOU Intent (Polite Acknowledgement)
        # -------------------------------------------------------------
        if intent == "THANK_YOU":
            msg = {
                "mr-IN": "काही हरकत नाही! मी तुम्हाला मार्ग, वेळापत्रक किंवा बस पासबद्दल आणखी काही मदत करू का?",
                "hi-IN": "आपका स्वागत है! क्या आपको रूट, समय-सारणी या बस पास के बारे में कोई और सहायता चाहिए?",
                "en-IN": "You are welcome! Would you like any further assistance regarding bus routes, timetables, or passes?"
            }.get(lang, "You are welcome!")
            res = self._response(msg, "THANK_YOU", lang=lang)
            ctx.last_response_text = msg
            return res

        # -------------------------------------------------------------
        # 2B. REPEAT_QUERY Intent (Phase 4.8 Repeat - Defect 3 Fix)
        # -------------------------------------------------------------
        if intent == "REPEAT_QUERY":
            if ctx.last_response_text:
                msg = ctx.last_response_text
            else:
                msg = {
                    "mr-IN": "माफ करा, पुन्हा सांगण्यासाठी आधीचे कोणतेही संभाषण उपलब्ध नाही.",
                    "hi-IN": "क्षमा करें, दोहराने के लिए पिछला कोई उत्तर उपलब्ध नहीं है।",
                    "en-IN": "Sorry, there is no previous response to repeat."
                }.get(lang, "Sorry, no previous response to repeat.")
            return self._response(msg, "REPEAT_QUERY", lang=lang)

        # -------------------------------------------------------------
        # 3. LANGUAGE_CHANGE Intent (Phase 4.8 Section 2 P0)
        # -------------------------------------------------------------
        if intent == "LANGUAGE_CHANGE":
            norm_cmd = user_text.lower()
            new_target_lang = "mr-IN"
            if any(re.search(p, norm_cmd, re.IGNORECASE) for p in LANG_CHANGE_PATTERNS_HI):
                new_target_lang = "hi-IN"
                msg = "जी हाँ, अब से मैं आपसे हिंदी में बात करूँगा। मैं आपकी क्या मदद कर सकता हूँ?"
            elif any(re.search(p, norm_cmd, re.IGNORECASE) for p in LANG_CHANGE_PATTERNS_EN):
                new_target_lang = "en-IN"
                msg = "Sure, I will speak with you in English from now on. How can I help you?"
            else:
                new_target_lang = "mr-IN"
                msg = "होय, आतापासून मी तुमच्याशी मराठीत बोलेन. मी आपली काय मदत करू शकतो?"

            ctx.language = new_target_lang
            ctx.has_explicit_language_preference = True
            ctx.explicit_language = new_target_lang
            return self._response(msg, "LANGUAGE_CHANGE", lang=new_target_lang, new_language=new_target_lang)

        # -------------------------------------------------------------
        # 3B. NUMBER_PRONUNCIATION_PREFERENCE Intent
        # -------------------------------------------------------------
        if intent == "number_pronunciation_preference":
            msg = "होय, आतापासून मी आकडे आणि संख्या मराठी शब्दात बोलेन (रुपये, मिनिटे, words)."
            return self._response(msg, "number_pronunciation_preference", lang=lang)

        # -------------------------------------------------------------
        # 4. GREETING Intent (Phase 4.8 Section 4 P0)
        # -------------------------------------------------------------
        if intent == "GREETING":
            msg = {
                "mr-IN": "नमस्कार! मी सिटीलिंक बस सहाय्यक आहे. मी आपली काय मदत करू शकतो?",
                "hi-IN": "नमस्ते! मैं सिटीलिंक बस सहायक हूँ। मैं आपकी क्या मदद कर सकता हूँ?",
                "en-IN": "Hello! I am your CityLinc bus assistant. How can I assist you today?"
            }.get(lang, "Hello! How can I assist you today?")
            return self._response(msg, "GREETING", lang=lang)

        # -------------------------------------------------------------
        # 5. ROUTE_CORRECTION Intent (Phase 4.8 Section 5 P1)
        # -------------------------------------------------------------
        if intent == "ROUTE_CORRECTION":
            # 5A. POI / Hospital Correction ("ते बायको नाही, ते बिटको आहे")
            old_poi, new_poi = self._extract_poi_correction(user_text, ctx)
            if new_poi and any(k in new_poi for k in ["बिटको", "bitco", "bytco"]):
                ctx.selected_hospital_id = "bytco_hospital"
                ctx.selected_hospital_name = "Bitco Hospital (Nashik Road)"
                msg = {
                    "mr-IN": "माफ करा, बरोबर - ते बिटको रुग्णालय (Bitco Hospital, Nashik Road) आहे. बिटको रुग्णालय हे नाशिक रोड येथे असून 'Nashik Road Depot' किंवा 'Datta Mandir' थांब्यावरून जवळ आहे. तुम्हाला तेथे जाण्यासाठी बसचा मार्ग हवा आहे का?",
                    "hi-IN": "क्षमा करें, सही - वह बिटको अस्पताल (Bitco Hospital, Nashik Road) है। बिटको अस्पताल नाशिक रोड पर स्थित है। क्या आपको वहाँ जाने के लिए बस मार्ग चाहिए?",
                    "en-IN": "Understood and corrected - it is Bitco Hospital (Nashik Road). Bitco Hospital is located at Nashik Road near Nashik Road Depot. Would you like bus routes to reach there?"
                }.get(lang, "Corrected to Bitco Hospital (Nashik Road).")
                return self._response(msg, "ROUTE_CORRECTION", lang=lang)

            # 5B. Slot Correction (Preserving the other valid slot!)
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
                    return self._response(msg, "ROUTE_CORRECTION", lang=lang)
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
                    return self._response(msg, "ROUTE_CORRECTION", lang=lang)

        # -------------------------------------------------------------
        # 6. ROUTE_QUERY Intent (Strict Context Isolation)
        # -------------------------------------------------------------
        if intent == "ROUTE_QUERY":
            # Origin-Destination extracted
            parsed_od = self._extract_origin_destination(user_text)
            if parsed_od:
                orig_raw, dest_raw = parsed_od
                return self._handle_route_search(ctx, orig_raw, dest_raw, lang=lang, user_text=user_text)

            # Single destination
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
                    }.get(lang, f"Starting point needed for {dest_only}.")
                    return self._response(msg, "ROUTE_QUERY", lang=lang)

            # Conversational slot filling when one slot is already known
            if (ctx.destination and not ctx.origin) or (ctx.origin and not ctx.destination):
                res_place = route_engine.resolve_place(user_text)
                if res_place.matched_station_ids:
                    if ctx.destination and not ctx.origin:
                        return self._handle_route_search(ctx, user_text, ctx.destination, lang=lang, user_text=user_text)
                    elif ctx.origin and not ctx.destination:
                        return self._handle_route_search(ctx, ctx.origin, user_text, lang=lang, user_text=user_text)

            # Location declaration update
            loc_match = re.search(r'(?:मी|आम्ही)\s+(?:आता\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:जवळ|येथे|वर|पाशी|मध्ये)\s*आहे', user_text, re.IGNORECASE)
            if not loc_match:
                loc_match = re.search(r'i(?:\'m|m|\s+am)\s+(?:now\s+|currently\s+)?(?:at|near|around|in)\s+([a-zA-Z0-9\s]+)', user_text, re.IGNORECASE)
            if not loc_match:
                loc_match = re.search(r'(?:मैं|हम)\s+([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:के पास|पर|में)\s*हूँ', user_text, re.IGNORECASE)

            if loc_match:
                loc_raw = loc_match.group(1).strip()
                resolved = route_engine.resolve_place(loc_raw)
                ctx.current_location = resolved.canonical_name.title() if resolved.canonical_name else loc_raw.title()
                ctx.current_location_display = resolved.display_name
                ctx.current_location_station_ids = resolved.matched_station_ids
                if resolved.matched_station_ids:
                    st = location_service.stations_by_id.get(resolved.matched_station_ids[0])
                    if st:
                        ctx.current_location_lat = st["lat"]
                        ctx.current_location_lon = st["lon"]
                ctx.origin = ctx.current_location
                ctx.origin_display = ctx.current_location_display
                if ctx.destination:
                    return self._handle_route_search(ctx, ctx.origin, ctx.destination, lang=lang, user_text=user_text)
                msg = {
                    "mr-IN": f"समजले, तुम्ही {ctx.current_location_display} जवळ आहात. तुम्हाला कोठे जायचे आहे ते सांगा.",
                    "hi-IN": f"समझ गया, आप {ctx.current_location_display} के पास हैं। आपको कहाँ जाना है बताएं।",
                    "en-IN": f"Understood, your location is set to {ctx.current_location_display}. Where would you like to travel to?"
                }.get(lang, f"Location set to {ctx.current_location_display}.")
                return self._response(msg, "location_declaration", lang=lang)

            # Standalone place query when neither slot was set yet
            if not ctx.origin and not ctx.destination:
                res_place = route_engine.resolve_place(user_text)
                if res_place.matched_station_ids:
                    ctx.origin = res_place.canonical_name.title() if res_place.canonical_name else user_text.title()
                    ctx.origin_display = res_place.display_name
                    ctx.current_location = ctx.origin
                    ctx.current_location_display = ctx.origin_display
                    msg = {
                        "mr-IN": f"समजले, सुरुवातीचा थांबा '{ctx.origin_display}' नोंदवला आहे. तुम्हाला कोठे जायचे आहे ते सांगा.",
                        "hi-IN": f"समझ गया, प्रारंभिक स्टॉप '{ctx.origin_display}' सेट किया गया है। आपको कहाँ जाना है बताएं।",
                        "en-IN": f"Understood, starting stop set to '{ctx.origin_display}'. Where would you like to travel to?"
                    }.get(lang, f"Starting from {ctx.origin_display}. Where would you like to travel to?")
                    return self._response(msg, "ROUTE_QUERY", lang=lang)

            # Journey follow-up queries
            u_low = user_text.lower()
            if any(w in u_low for w in ["कुठे उतरायचं", "उतरायचं कुठे", "where to get off", "where to alight", "कहाँ उतरना है"]):
                if ctx.last_direct_routes:
                    dr = ctx.last_direct_routes[0]
                    msg = {
                        "mr-IN": f"तुम्ही '{dr['alighting_stop']['name']}' थांब्यावर उतरावे.",
                        "hi-IN": f"आप '{dr['alighting_stop']['name']}' स्टॉप पर उतरें।",
                        "en-IN": f"You should alight at '{dr['alighting_stop']['name']}' stop."
                    }.get(lang, f"Alight at {dr['alighting_stop']['name']}.")
                    return self._response(msg, "ROUTE_QUERY", lang=lang)

            if any(w in u_low for w in ["किती वेळ लागेल", "वेळ किती लागेल", "how much time", "how long will it take", "कितना समय लगेगा"]):
                dur = 25
                if ctx.last_direct_routes:
                    dur = ctx.last_direct_routes[0].get('duration_mins', 25)
                msg = {
                    "mr-IN": f"या प्रवासासाठी अंदाजे {dur} मिनिटे लागतील.",
                    "hi-IN": f"इस यात्रा में लगभग {dur} मिनट लगेंगे।",
                    "en-IN": f"This journey will take approximately {dur} minutes."
                }.get(lang, f"Journey will take approx. {dur} minutes.")
                return self._response(msg, "ROUTE_QUERY", lang=lang)

        # -------------------------------------------------------------
        # 7. HOSPITAL_LOOKUP Intent (Separate POI Context)
        # -------------------------------------------------------------
        if intent == "HOSPITAL_LOOKUP":
            norm = user_text.lower()
            # Clear previous route selection so route context doesn't interfere
            ctx.selected_route_id = None
            ctx.selected_rno = None

            # Hospital distance (Checked BEFORE hospital count so 'किती लांब' is not hijacked by 'किती')
            if any(w in norm for w in ["लांब", "अंतर", "distance", "how far", "दूर", "दूरी"]):
                nav_res = location_service.get_hospital_navigation(user_text)
                if not nav_res.get("resolved") and ctx.selected_hospital_id:
                    nav_res = location_service.get_hospital_navigation(ctx.selected_hospital_id)
                if not nav_res.get("resolved") and any(w in norm for w in ["सिव्हिल", "civil"]):
                    nav_res = location_service.get_hospital_navigation("civil")

                if nav_res.get("resolved"):
                    h_name = nav_res["name_mr" if lang == "mr-IN" else "name_en"]
                    p_stop = nav_res.get("primary_alighting_stop", {})
                    walk_m = p_stop.get("approx_walk_distance_m", 97)
                    stop_name = p_stop.get("name", "बस थांबा")
                    msg = {
                        "mr-IN": f"{h_name}: '{stop_name}' बस थांब्यापासून मुख्य प्रवेशद्वारापर्यंत अंदाजे {walk_m} मीटर चालण्याच्या अंतरावर आहे.",
                        "hi-IN": f"{h_name}: '{stop_name}' बस स्टॉप से अस्पताल के मुख्य गेट तक लगभग {walk_m} मीटर पैदल दूरी है।",
                        "en-IN": f"For {nav_res['name_en']}: The walking distance from '{stop_name}' bus stop to the hospital main entrance is approximately {walk_m} meters."
                    }.get(lang, f"Walking distance is approx. {walk_m} meters from {stop_name}.")
                    return self._response(msg, "HOSPITAL_LOOKUP", lang=lang)

            # Hospital count
            if any(w in norm for w in ["how many", "total", "संख्या", "count", "number of", "कितने", "टोटल", "सगळे", "एकूण"]) or ("किती" in norm and not any(w in norm for w in ["लांब", "अंतर", "दूर"])):
                total_count = len(location_service.hospital_entities)
                msg = {
                    "mr-IN": f"सिटीलिंक अधिकृत ट्रॅन्झिट नोंदणीत एकूण {total_count} प्रमुख पडताळलेली रुग्णालये समाविष्ट आहेत (उदा. बिटको रुग्णालय, जिल्हा सामान्य रुग्णालय, नामको कॅन्सर रुग्णालय). ही सिटीलिंक बस थांब्यांशी थेट जोडलेली प्रमुख रुग्णालये आहेत.",
                    "hi-IN": f"सिटीलिंक आधिकारिक ट्रांजिट रजिस्ट्री में कुल {total_count} सत्यापित अस्पताल शामिल हैं (जैसे बिटको अस्पताल, सिविल अस्पताल, नामको अस्पताल)। यह बस सेवा से जुड़े प्रमुख अस्पतालों की संख्या है।",
                    "en-IN": f"The CityLinc verified transit registry includes {total_count} verified physical hospital entities across Nashik (including Bitco Hospital, District Civil Hospital, NAMCO Hospital)."
                }.get(lang, f"There are {total_count} verified hospitals in the CityLinc registry.")
                return self._response(msg, "HOSPITAL_LOOKUP", lang=lang)

            # Nearby hospital lookup (e.g. 'नाशिक रोडचे जवळचे हॉस्पिटल कोणते आहे?')
            user_lat = ctx.current_location_lat
            user_lon = ctx.current_location_lon
            loc_label = ctx.current_location_display or ctx.current_location or "तुमचे ठिकाण"

            for canon_key, alias_list in sorted(LOCATION_ALIASES.items(), key=lambda item: max(len(al) for al in item[1]), reverse=True):
                if any(al in norm for al in alias_list):
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
                    return self._response(msg, "HOSPITAL_LOOKUP", lang=lang)

            # General hospital resolution
            nav_res = location_service.get_hospital_navigation(user_text)
            if nav_res.get("resolved"):
                ctx.selected_hospital_id = nav_res["hospital_id"]
                ctx.selected_hospital_name = nav_res["name_en"]
                h_name = nav_res["name_mr" if lang == "mr-IN" else "name_en"]
                p_stop = nav_res.get("primary_alighting_stop", {})
                stop_name = p_stop.get("name", "जवळच्या थांब्यावर")
                walk_m = p_stop.get("approx_walk_distance_m", 50)
                routes_sample = ", ".join(nav_res.get("direct_routes", [])[:6])

                msg = {
                    "mr-IN": f"{h_name} ला जाण्यासाठी '{stop_name}' थांब्यावर उतरावे (प्रवेशद्वारापासून अंदाजे {walk_m} मीटर चालणे). या थांब्यावरून धावणाऱ्या प्रमुख बसेस: {routes_sample}.",
                    "hi-IN": f"{h_name} जाने के लिए '{stop_name}' स्टॉप पर उतरें (अस्पताल के मुख्य गेट से लगभग {walk_m} मीटर पैदल)। मुख्य बसें: {routes_sample}।",
                    "en-IN": f"To reach {nav_res['name_en']}, alight at '{stop_name}' (approx. {walk_m}m walk to hospital main entrance). Passing routes: {routes_sample}."
                }.get(lang, f"Alight at {stop_name}.")
                return self._response(msg, "HOSPITAL_LOOKUP", lang=lang)

            prompt_msg = {
                "mr-IN": "तुम्हाला कोणत्या भागातून किंवा थांब्यावरून जवळचे रुग्णालय हवे आहे? कृपया तुमचे ठिकाण सांगा (उदा. नाशिक रोड, सीबीएस, सातपूर, केबीटी सर्कल).",
                "hi-IN": "आप किस क्षेत्र या स्टॉप के पास का अस्पताल जानना चाहते हैं? कृपया अपना स्थान बताएं (उदा. नाशिक रोड, सीबीएस, सातपुर)।",
                "en-IN": "Which area or bus stop are you currently near? Please specify your location (e.g., Nashik Road, CBS, Satpur) to find the nearest hospital."
            }.get(lang, "Please specify your location to find the nearest hospital.")
            return self._response(prompt_msg, "HOSPITAL_LOOKUP", lang=lang)

        # -------------------------------------------------------------
        # 8. STOP_LOOKUP Intent
        # -------------------------------------------------------------
        if intent == "STOP_LOOKUP":
            conf_stop = self._extract_confirmation_stop(user_text)
            if not ctx.selected_route_id and not ctx.selected_rno:
                msg = {
                    "mr-IN": "सध्या कोणताही बस मार्ग निवडलेला नाही. कृपया तुम्हाला कोणत्या मार्गाबद्दल किंवा ठिकाणाबद्दल विचारायचे आहे ते सांगा.",
                    "hi-IN": "वर्तमान में कोई बस रूट सक्रिय नहीं है। कृपया बताएं कि आप किस रूट या स्थान के बारे में जानना चाहते हैं।",
                    "en-IN": "There is no active bus route currently selected. Please specify which route or destination you would like to ask about."
                }.get(lang, "No active route currently selected.")
                return self._response(msg, "STOP_LOOKUP", lang=lang)

            active_rno = ctx.selected_rno or "निवडलेला मार्ग"
            active_rname = ctx.selected_route_name or ""
            target_stop_raw = conf_stop or user_text
            res_stop = route_engine.resolve_place(target_stop_raw)

            # Determine whether selected route serves the queried stop
            r_stops = set()
            if ctx.last_direct_routes:
                for dr in ctx.last_direct_routes:
                    if dr.get("rno") == ctx.selected_rno:
                        r_stops.add(dr["boarding_stop"]["station_id"])
                        r_stops.add(dr["alighting_stop"]["station_id"])
                        for s in dr.get("intermediate_stops", []):
                            r_stops.add(s["station_id"])
            if ctx.selected_route_id and ctx.selected_route_id in transit_graph.routes:
                for sid in transit_graph.routes[ctx.selected_route_id].station_ids:
                    r_stops.add(sid)

            corridor_dwarka_sids = {497, 1078, 1287, 1049}
            if res_stop.canonical_name == "dwarka":
                is_served = any(sid in r_stops for sid in corridor_dwarka_sids)
            else:
                is_served = any(sid in r_stops for sid in res_stop.matched_station_ids) if res_stop.matched_station_ids else False

            if is_served:
                msg = {
                    "mr-IN": f"होय, मार्ग {active_rno} हा '{res_stop.display_name}' थांब्यावरून जातो.",
                    "hi-IN": f"हाँ, रूट {active_rno} '{res_stop.display_name}' स्टॉप पर रुकता है।",
                    "en-IN": f"Yes, Route {active_rno} stops at '{res_stop.display_name}'."
                }.get(lang, f"Yes, Route {active_rno} serves {res_stop.display_name}.")
            else:
                msg = {
                    "mr-IN": f"नाही, मार्ग {active_rno} हा '{res_stop.display_name}' थांब्यावरून जात नाही.",
                    "hi-IN": f"नहीं, रूट {active_rno} '{res_stop.display_name}' स्टॉप पर नहीं रुकता है।",
                    "en-IN": f"No, Route {active_rno} does not serve '{res_stop.display_name}'."
                }.get(lang, f"No, Route {active_rno} does not serve {res_stop.display_name}.")
            return self._response(msg, "STOP_LOOKUP", lang=lang)

        # -------------------------------------------------------------
        # 9. TIMETABLE_QUERY Intent
        # -------------------------------------------------------------
        if intent == "TIMETABLE_QUERY":
            if ctx.selected_rno or ctx.selected_route_id:
                target_r = str(ctx.selected_route_id or ctx.selected_rno)
                tt_res = timetable_engine.query_route_departures(target_r, boarding_stop_id=ctx.boarding_stop_id, boarding_stop_name=ctx.boarding_stop_name, query_mode="next")
                if tt_res.success and tt_res.trips_count > 0:
                    reply = {"mr-IN": tt_res.message_mr, "hi-IN": tt_res.message_hi, "en-IN": tt_res.message_en}.get(lang, tt_res.message_en)
                    return self._response(reply, "TIMETABLE_QUERY", lang=lang)

            if ctx.origin and ctx.destination:
                tt_res = timetable_engine.get_next_bus_for_od(ctx.origin, ctx.destination)
                if tt_res.success and tt_res.trips_count > 0:
                    reply = {"mr-IN": tt_res.message_mr, "hi-IN": tt_res.message_hi, "en-IN": tt_res.message_en}.get(lang, tt_res.message_en)
                    return self._response(reply, "TIMETABLE_QUERY", lang=lang)

            ask_msg = {
                "mr-IN": "तुम्हाला कोणत्या मार्गाची किंवा कुठून कुठे जाणाऱ्या बसची वेळ जाणून घ्यायची आहे? कृपया सांगा.",
                "hi-IN": "आप किस रूट या कहाँ से कहाँ तक की बस का समय जानना चाहते हैं? कृपया बताएं।",
                "en-IN": "Which route or origin-destination would you like the next bus timing for? Please let me know."
            }.get(lang, "Please specify the route or origin and destination.")
            return self._response(ask_msg, "TIMETABLE_QUERY", lang=lang)

        # -------------------------------------------------------------
        # 10. BUS_PASS_QUERY & FARE_QUERY (With Mandatory Safety Notice - Defect 5 Fix)
        # -------------------------------------------------------------
        if intent in ("BUS_PASS_QUERY", "FARE_QUERY"):
            norm = user_text.lower()
            if any(w in norm for w in ["student", "विद्यार्थी", "छात्र"]):
                msg = {
                    "mr-IN": "सिटीलिंक विद्यार्थी पास (Student Pass): ३० दिवसांच्या मासिक पासवर ५०% सवलत आहे, आणि ९० दिवसांच्या त्रैमासिक पासवर ६६.६०% सवलत मिळते. (टीप: हे दर १ जानेवारी २०२३ च्या अधिकृत टप्प्यांवर आधारित असून सध्याचे अधिकृत दर पडताळणी प्रलंबित आहेत).",
                    "hi-IN": "सिटीलिंक छात्र पास (Student Pass): 30 दिनों के पास पर 50% छूट और 90 दिनों के पास पर 66.60% छूट मिलती है। (नोट: ये दरें 1 जनवरी 2023 की तालिका पर आधारित हैं और वर्तमान दर सत्यापन लंबित है।)",
                    "en-IN": "CityLinc Student Bus Pass: 50% concession on 30-Day monthly pass and 66.60% concession on 90-Day quarterly pass. (Note: Rates based on official Jan-2023 tariff; live official gazette verification is currently pending)."
                }.get(lang, "Student pass offers 50% discount for 30 days and 66.6% discount for 90 days.")
                res = self._response(msg, intent, lang=lang)
                ctx.last_response_text = msg
                return res

            if intent == "BUS_PASS_QUERY" or any(w in norm for w in ["पास", "pass"]):
                msg = {
                    "mr-IN": "सिटीलिंक बस पास दर: १ दिवसाचा ओपन पास ₹११०, ७ दिवस ₹५३०, ३० दिवसांचा मासिक पास ₹२,१००, त्रैमासिक पास (९० दिवस) ₹५,२५०, आणि अर्धवार्षिक पास (१८० दिवस) ₹९,४५०. (टीप: हे दर १ जानेवारी २०२३ च्या अधिकृत टप्प्यांवर आधारित अंदाजित असून सध्याचे दर पडताळणी प्रलंबित आहेत).",
                    "hi-IN": "सिटीलिंक बस पास दर: 1 दिन का पास ₹110, 7 दिन ₹530, 30 दिन का मासिक पास ₹2,100, 90 दिन ₹5,250, और 180 दिन ₹9,450 है। (नोट: ये दरें 1 जनवरी 2023 की तालिका पर आधारित हैं और वर्तमान दर सत्यापन लंबित है।)",
                    "en-IN": "CityLinc All-Route Bus Passes: 1-Day Open Pass is ₹110, 7-Day is ₹530, 30-Day pass is ₹2,100, 90-Day is ₹5,250, and 180-Day is ₹9,450. (Note: Rates based on official Jan-2023 tariff; live official gazette verification is currently pending)."
                }.get(lang, "1-Day pass is ₹110 and 30-Day pass is ₹2,100.")
                res = self._response(msg, intent, lang=lang)
                ctx.last_response_text = msg
                return res

            # Ticket fare query
            orig = ctx.origin_display or ctx.origin
            dest = ctx.destination_display or ctx.destination
            if orig and dest and ctx.last_fare_estimate:
                fare = ctx.last_fare_estimate.get("single_fare_inr", 20)
                dist = ctx.last_fare_estimate.get("distance_km", 10.0)
                msg = {
                    "mr-IN": f"{orig} ते {dest} ({dist} किमी) प्रवासासाठी अंदाजे तिकीट भाडे ₹{fare} आहे. (टीप: हे दर १ जानेवारी २०२३ च्या अधिकृत टप्प्यांवर आधारित असून सध्याचे दर पडताळणी प्रलंबित आहेत).",
                    "hi-IN": f"{orig} से {dest} ({dist} किमी) की यात्रा के लिए अनुमानित टिकट किराया ₹{fare} है। (नोट: ये दरें 1 जनवरी 2023 की तालिका पर आधारित हैं और वर्तमान दर सत्यापन लंबित है।)",
                    "en-IN": f"For travel between {orig} and {dest} ({dist} km), estimated adult fare is ₹{fare}. (Note: Rates based on official Jan-2023 tariff; live official gazette verification is currently pending)."
                }.get(lang, f"The estimated fare is ₹{fare}.")
                res = self._response(msg, intent, lang=lang)
                ctx.last_response_text = msg
                return res

            msg = {
                "mr-IN": "सिटीलिंक नाशिक बस तिकीट दर अंतरावर आधारित आहेत: किमान ₹१० (२ किमी पर्यंत). १ दिवसाचा ओपन पास ₹११० आणि ३० दिवसांचा मासिक पास ₹२,१०० आहे. (टीप: हे दर १ जानेवारी २०२३ च्या अधिकृत टप्प्यांवर आधारित असून सध्याचे दर पडताळणी प्रलंबित आहेत).",
                "hi-IN": "सिटीलिंक बस टिकट किराया दूरी पर आधारित है: न्यूनतम ₹10 (2 किमी तक)। 1 दिन का पास ₹110 और 30 दिन का पास ₹2,100 है। (नोट: ये दरें 1 जनवरी 2023 की तालिका पर आधारित हैं और वर्तमान दर सत्यापन लंबित है।)",
                "en-IN": "CityLinc fares are distance-based: minimum ₹10 (up to 2 km). 1-Day open pass is ₹110 and 30-Day pass is ₹2,100. (Note: Rates based on official Jan-2023 tariff; live official gazette verification is currently pending)."
            }.get(lang, "Bus fares are distance-based. 1-Day pass is ₹110 and 30-Day pass is ₹2,100.")
            res = self._response(msg, intent, lang=lang)
            ctx.last_response_text = msg
            return res

        # -------------------------------------------------------------
        # 11. CLARIFICATION Intent
        # -------------------------------------------------------------
        fallback_msg = {
            "mr-IN": "मला समजले नाही. कृपया तुम्हाला कुठून कुठे जायचे आहे ते सांगा (उदा. 'निमाणी ते सीबीएस' किंवा 'नाशिक रोडला जायचं आहे').",
            "hi-IN": "मुझे समझ नहीं आया। कृपया बताएं कि आपको कहाँ से कहाँ जाना है (जैसे 'निमाणी से सीबीएस' या 'नाशिक रोड जाना है')।",
            "en-IN": "I didn't quite catch that. Please let me know your origin and destination (e.g., 'From Nimani to CBS' or 'I want to go to Nashik Road')."
        }.get(lang, "Please specify your origin and destination.")
        res = self._response(fallback_msg, "CLARIFICATION", lang=lang)
        if intent != "REPEAT_QUERY":
            ctx.last_response_text = res.get("response_text")
        return res

    def _clean_token(self, t: str) -> str:
        val = t.strip()
        vocatives_patterns = [
            r'(?:^|[,\s])(?:रे\s*बाबा|बाबा|दादा|भावा|भाऊ|मित्रा|यार|भाई)(?:$|[,\s])',
            r'(?:^|[,\s])(?:सर|मॅडम)(?:$|[,\s])'
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
            val = val.strip(' ,.?!;:()"\'’‘[]{}')
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

        obliques = [
            ("नाक्या", "नाका"),
            ("फाट्या", "फाटा"),
            ("चौका", "चौक"),
            ("मंदिरा", "मंदिर"),
            ("स्थानका", "स्थानक"),
            ("सर्कलला", "सर्कल"),
            ("रोडला", "रोड"),
            ("पंचवडीला", "पंचवडी"),
            ("पंचवटीला", "पंचवटी"),
            ("हाशिकरोडला", "हाशिकरोड"),
            ("नाशिकरोडला", "नाशिक रोड"),
        ]
        for obl, nom in obliques:
            if val.endswith(obl):
                val = val[:-len(obl)] + nom
                break

        return val.strip(' ,.?!;:()"\'’‘[]{}')

    def _extract_confirmation_stop(self, text: str) -> Optional[str]:
        norm = text.strip()
        m_en = re.search(r'(?:does\s+(?:this|the)\s+bus|will\s+this\s+bus|is\s+this\s+bus)\s+(?:also\s+)?(?:go\s+through|pass\s+through|stop\s+at|serve|go\s+to|reach|cover)\s+([a-zA-Z0-9\s]+?)(?:\s*(?:also|too|\?|$))', norm, re.IGNORECASE)
        if m_en:
            return self._clean_token(m_en.group(1))

        m_mr = re.search(r'(?:ही\s+बस|ही\s+गाडी|हा\s+मार्ग|या\s+बसने|बस)\s+([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:वरून|पा(?:सू|सु)न|हून|मधून|ला|कडे|तिथून)?\s*(?:पण\s+)?(?:जाते\s+का|जाते\s+ना|जाईल\s+का|थांबते\s+का|थांबते\s+ना|येते\s+का|जाणार\s+का|होते\s+का|\?|$)', norm, re.IGNORECASE)
        if m_mr:
            token = self._clean_token(m_mr.group(1))
            for sfx in ["वरून", "पासून", "हून", "मधून", "ला", "कडे"]:
                if token.endswith(sfx):
                    token = token[:-len(sfx)].strip()
            if token and token not in ["पण", "का", "ना", "बस"]:
                return token

        m_hi = re.search(r'(?:क्या\s+यह\s+बस|क्या\s+ये\s+बस|ये\s+बस|यह\s+बस)\s+([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:से|पर|तक)?\s*(?:भी\s+)?(?:होकर\s+)?(?:जाती\s+है\s+क्या|जाती\s+है\s+ना|रुकती\s+है\s+क्या|रुकती\s+है\s+ना|जाएगी\s+क्या|\?|$)', norm, re.IGNORECASE)
        if m_hi:
            token = self._clean_token(m_hi.group(1))
            for sfx in ["से", "पर", "तक"]:
                if token.endswith(sfx):
                    token = token[:-len(sfx)].strip()
            if token and token not in ["भी", "क्या", "ना"]:
                return token

        m_rom = re.search(r'(?:ye\s+bus|yeh\s+bus|hi\s+bus)\s+([a-zA-Z0-9\s]+?)\s*(?:se|varun|houn|la)?\s*(?:pan\s+)?(?:jaati\s+hai|jaate\s+ka|jaate\s+na|jaayegi\s+kya|rukegi\s+kya)', norm, re.IGNORECASE)
        if m_rom:
            return self._clean_token(m_rom.group(1))

        return None

    def _extract_negation_correction(
        self,
        text: str,
        ctx: SessionTransitContext
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        t = text.strip()
        vocatives_patterns = [
            r'(?:^|[,\s])(?:रे\s*बाबा|बाबा|दादा|भावा|भाऊ|मित्रा|यार|भाई)(?:$|[,\s])',
            r'(?:^|[,\s])(?:सर|मॅडम)(?:$|[,\s])'
        ]
        t_voc = t
        for _ in range(2):
            for voc in vocatives_patterns:
                t_voc = re.sub(voc, ' ', t_voc, flags=re.IGNORECASE)
        t_voc = re.sub(r'\s+', ' ', t_voc).strip()

        m_lead = re.match(r'^(?:नाही|नको|नहीं|no)[,\s]+', t_voc, re.IGNORECASE)
        t_clean = t_voc[m_lead.end():].strip() if m_lead else t_voc

        m_corr = re.search(
            r'([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:नाही|नको|नहीं|not)[,\s]+(?:मला\s+|मुझे\s+)?([a-zA-Z0-9ऀ-ॿ\s]+)',
            t_voc,
            re.IGNORECASE
        )
        if m_corr:
            old_p = self._clean_token(m_corr.group(1))
            new_p = m_corr.group(2).strip()
            old_p = re.sub(r'^(?:नाही|नको|नहीं|no)[,\s]+', '', old_p, flags=re.IGNORECASE).strip()

            new_p_clean = re.sub(
                r'\s*(?:जायचे|जायचं|जायचेय|जायचंय|जाना\s*है|जाना|पोहोचायचे|to\s*go|निघायचे|आहे|का|\?|\.|$).*',
                '',
                new_p,
                flags=re.IGNORECASE
            ).strip()
            new_p_clean = self._clean_token(new_p_clean)

            new_res = route_engine.resolve_place(new_p_clean)
            if not new_res.matched_station_ids:
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

            canon_new = new_res.canonical_name.title() if new_res.canonical_name else new_p_clean

            dest_res = route_engine.resolve_place(old_p)
            ctx_dest_res = route_engine.resolve_place(ctx.destination or "")
            if (
                ctx.destination
                and (old_p.lower() in ctx.destination.lower() or dest_res.canonical_name == ctx_dest_res.canonical_name)
            ) or any(sfx in new_p for sfx in ["ला", "कडे", "पर्यंत", "स", "तक", "को", "to"]):
                return "dest", old_p, canon_new

            orig_res = route_engine.resolve_place(old_p)
            ctx_orig_res = route_engine.resolve_place(ctx.origin or "")
            if (
                ctx.origin
                and (old_p.lower() in ctx.origin.lower() or orig_res.canonical_name == ctx_orig_res.canonical_name)
            ) or any(sfx in new_p for sfx in ["वरून", "पासून", "हून", "कडून", "से", "from"]):
                return "origin", old_p, canon_new

            if ctx.destination:
                return "dest", old_p, canon_new
            return "origin", old_p, canon_new

        if m_lead:
            orig_cand = self._extract_single_origin(t_clean)
            if orig_cand:
                o_res = route_engine.resolve_place(orig_cand)
                return "origin", None, o_res.canonical_name.title() if o_res.canonical_name else orig_cand

            dest_cand = self._extract_single_destination(t_clean)
            if dest_cand:
                d_res = route_engine.resolve_place(dest_cand)
                return "dest", None, d_res.canonical_name.title() if d_res.canonical_name else dest_cand

        return None, None, None

    def _extract_single_destination(self, text: str) -> Optional[str]:
        norm = text.strip()
        if re.search(r'(?:from|पा(?:सू|सु)न|वरून|हून|कडून|से)', norm, re.IGNORECASE):
            return None

        m_mr = re.search(r'(?:आता\s+)?(?:मला\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)(?:ला|कडे|पर्यंत|स)\s*(?:जायचे|जायचं|जायचेय|जायचंय|पोहोचायचे|पोहोचायचं|निघायचे|निघायचं|कसे\s+जावे|जाता\s+येईल|जाऊ)?(?:\s+आहे)?(?:\.|$)', norm, re.IGNORECASE)
        if m_mr:
            cand = self._clean_token(m_mr.group(1))
            if cand and cand not in ["बस", "तिकीट", "पास", "हॉस्पिटल", "रुग्णालय", "कसे", "कुठे"]:
                return cand

        m_mr2 = re.search(r'(?:आता\s+)?(?:मला\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)\s+(?:जायचे|जायचं|जायचेय|जायचंय|पोहोचायचे|पोहोचायचं|निघायचे|निघायचं)(?:\s+आहे)?(?:\.|$)', norm, re.IGNORECASE)
        if m_mr2:
            cand = self._clean_token(m_mr2.group(1))
            if cand and cand not in ["बस", "तिकीट", "पास", "हॉस्पिटल", "रुग्णालय", "कसे", "कुठे"]:
                return cand

        m_hi = re.search(r'(?:अब\s+)?(?:मुझे\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:जाना\s+है|पहुंचना\s+है|ले\s+चलो|जाना)(?:\.|$)', norm, re.IGNORECASE)
        if m_hi:
            cand = self._clean_token(m_hi.group(1))
            if cand and cand not in ["बस", "टिकट", "पास", "अस्पताल"]:
                return cand

        m_en = re.search(r'(?:now\s+)?(?:take\s+me\s+to|i\s+want\s+to\s+go\s+to|how\s+to\s+reach|how\s+can\s+i\s+get\s+to|travel\s+to|go\s+to|to)\s+([a-zA-Z0-9\s]+?)(?:\s+please)?(?:\.|$)', norm, re.IGNORECASE)
        if m_en:
            cand = self._clean_token(m_en.group(1))
            if cand and cand not in ["bus", "ticket", "pass", "hospital"]:
                return cand

        return None

    def _extract_single_origin(self, text: str) -> Optional[str]:
        norm = text.strip()
        m_affix = re.search(r'(?:मला\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:वरून|पा(?:सू|सु)न|हून|कडून|से)(?:\s+(?:जायचे|जायचं|निघायचे|निघायचं|प्रवास|सुरू|जाना|travel).*)?$', norm, re.IGNORECASE)
        if m_affix:
            cand = self._clean_token(m_affix.group(1))
            if cand and cand not in ["बस", "तिकीट", "पास", "कसे", "कुठे"]:
                return cand

        m_mr = re.search(r'(?:आता\s+)?(?:मी|आम्ही)\s+(?:सध्या\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:जवळ|येथे|इथे|वरून|थेट)\s*(?:आहे|आहोत|निघतो|निघतोय|आलो|आलोय)?(?:\.|$)', norm, re.IGNORECASE)
        if m_mr:
            cand = self._clean_token(m_mr.group(1))
            if cand and cand not in ["बस", "तिकीट"]:
                return cand

        m_hi = re.search(r'(?:अब\s+)?(?:मैं|हम)\s+(?:अभी\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:के\s+पास|पर|में|से)\s*(?:हूँ|हैं)?(?:\.|$)', norm, re.IGNORECASE)
        if m_hi:
            cand = self._clean_token(m_hi.group(1))
            if cand and cand not in ["बस", "टिकट"]:
                return cand

        m_en = re.search(r'(?:now\s+)?(?:i\s+am|i\'m|we\s+are)\s+(?:currently\s+|now\s+)?(?:at|near|around|in|from)\s+([a-zA-Z0-9\s]+?)(?:\.|$)', norm, re.IGNORECASE)
        if m_en:
            cand = self._clean_token(m_en.group(1))
            if cand and cand not in ["bus", "ticket"]:
                return cand

        return None

    def _extract_origin_destination(self, text: str) -> Optional[Tuple[str, str]]:
        norm = text.strip()
        invalid_dest_particles = {"पण", "का", "ना", "तर", "की", "हो", "नाही", "also", "too", "bus", "बस", "गाडी", "रूट", "ते", "तो", "ती"}

        # Pattern 1: English inverted
        m_inv = re.search(r'(?:how\s+(?:can\s+i|to|do\s+i)?\s*)?(?:reach|get\s+to|go\s+to|travel\s+to)\s+([a-zA-Z\s]+?)\s+from\s+([a-zA-Z\s]+)', norm, re.IGNORECASE)
        if m_inv:
            orig = self._clean_token(m_inv.group(2))
            dest = self._clean_token(m_inv.group(1))
            if dest.lower() not in invalid_dest_particles and len(dest) >= 2 and len(orig) >= 2:
                return orig, dest

        # Pattern 2: English standard
        m_from_to = re.search(r'(?:travel\s+)?from\s+([a-zA-Z0-9ऀ-ॿ\s]+?)\s+to\s+([a-zA-Z0-9ऀ-ॿ\s]+)', norm, re.IGNORECASE)
        if m_from_to:
            orig = self._clean_token(m_from_to.group(1))
            dest = self._clean_token(m_from_to.group(2))
            if dest.lower() not in invalid_dest_particles and len(dest) >= 2 and len(orig) >= 2:
                return orig, dest

        # Pattern 3: Marathi: "[Orig] वरून / पासून / पासुन / हून / कडून (ते/to/टू) [Dest] (ला / कडे / पर्यंत / स) (trailing query/verb)"
        m_mr = re.search(
            r'([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:वरून|पा(?:सू|सु)न|हून|कडून)\s+(?:ते\s+|to\s+|टू\s+)?([a-zA-Z0-9ऀ-ॿ\s]+?)(?:ला|कडे|पर्यंत|स)?(?:\s+(?:जाण्यासाठी|जायचे|जायचं|जायचेय|जायचंय|जावे|जाणार|साठी|जा|पाहिजे|निघा|पोहोच|कसे|कोणती|कोणत्या|कोणता|बस|गाडी|उपलब्ध|आहे|जाते|का|\?|\.|$).*)?$',
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
        m_te = re.search(r'([a-zA-Z0-9ऀ-ॿ\s]+?)\s+(?:ते|to|टू)\s+([a-zA-Z0-9ऀ-ॿ\s]+?)(?:ला|कडे|पर्यंत|स)?(?:\s+(?:जाण्यासाठी|जायचे|जायचं|जायचेय|जायचंय|जावे|जाणार|साठी|पाहिजे|बस|गाडी|उपलब्ध|आहे|जाते|\?|\.|$).*)?$', norm, re.IGNORECASE)
        if m_te:
            orig = self._clean_token(m_te.group(1))
            dest = self._clean_token(m_te.group(2))
            if dest.lower() not in invalid_dest_particles and len(dest) >= 2 and len(orig) >= 2:
                return orig, dest

        # Pattern 5: Hindi: "[Orig] से [Dest] तक / को / के लिए"
        m_hi = re.search(r'([a-zA-Z0-9ऀ-ॿ\s]+?)\s+से\s+([a-zA-Z0-9ऀ-ॿ\s]+?)\s*(?:तक|को|के लिए|जाना|\.|$)', norm, re.IGNORECASE)
        if m_hi:
            orig = self._clean_token(m_hi.group(1))
            dest = self._clean_token(m_hi.group(2))
            if dest.lower() not in invalid_dest_particles and len(dest) >= 2 and len(orig) >= 2:
                return orig, dest

        return None


# Global singleton
context_engine = ContextEngine()
