"""
Production-grade Multilingual Language Detection Service for CityLinc Voice Agent.
Supports Marathi (mr-IN), Hindi (hi-IN), English (en-IN), Roman Marathi, Hinglish,
and code-switched Indian transit queries. Extensible for additional Indic languages.
"""
import re
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Set, Tuple

logger = logging.getLogger("citylinc.language_detector")

@dataclass
class LanguageDetectionResult:
    language: str              # e.g. "mr-IN", "hi-IN", "en-IN"
    confidence: float          # 0.0 to 1.0
    script: str                # "devanagari", "latin", "mixed", "numeric_only", "unknown"
    is_code_switched: bool     # True if mixed vocabulary (e.g. English nouns with Indic grammar)
    is_transliterated: bool    # True if Indic language written in Latin/Roman script
    reason: str                # Diagnostic description of classification


class MultilingualDetector:
    """
    Intelligent language detection combining acoustic cues from STT,
    Devanagari lexical analysis, Romanized phonetic markers (Roman Marathi & Hinglish),
    and conversation history context.
    """

    def __init__(self):
        # 1. Distinct Devanagari Marathi markers (particles, postpositions, auxiliaries)
        self.mr_devanagari_markers: Set[str] = {
            "आहे", "आहेत", "नाही", "नाहीये", "होते", "होती", "होता", "काय", "कधी", "कुठे",
            "कसे", "किती", "वाजता", "वरून", "कडून", "साठी", "पर्यंत", "पर्यन्त", "चा", "ची",
            "चे", "च्या", "द्या", "सांगा", "सांगाल", "हवी", "हवा", "हवे", "मला", "आम्हाला",
            "तुम्हाला", "माहिती", "सवलत", "तिकिट", "दर", "भाडे", "थांबा", "मार्ग", "वेळ",
            "येईल", "जाईल", "करतो", "करते", "करा", "होय", "बघा", "पाहिजे", "करायचे", "सुटेल",
            "गाडी", "सिटिलिंक", "सिटीलिंक", "विद्यार्थी", "आणि", "किंवा",
            # Additional Marathi verbal inflections & future forms
            "पासून", "पासुन", "कडे", "जायचं", "जायचे", "जायाचे", "करायचं", "द्यायचं", "द्यायचे",
            "देतील", "देऊ", "देणार", "करतील", "करू", "करणार", "जातील", "जाऊ", "जाणार",
            "येतील", "येऊ", "येणार", "थांबतील", "थांबेल", "थांबणार", "मिळेल", "मिळतील", "मिळणार",
            "सांगणार", "बोलणार"
        }

        # 2. Distinct Devanagari Hindi markers
        self.hi_devanagari_markers: Set[str] = {
            "है", "हैं", "नहीं", "था", "थी", "थे", "क्या", "कब", "कहाँ", "कहा", "कैसे",
            "कितना", "कितने", "कितनी", "बजे", "से", "के लिए", "तक", "का", "की", "के", "को",
            "में", "पर", "दीजिए", "बताइए", "बताओ", "चाहिए", "मुझे", "हमें", "आपको", "जानकारी",
            "छूट", "टिकट", "किराया", "समय", "स्टॉप", "रूट", "आएगी", "जाएगी", "करता", "करती",
            "करें", "हाँ", "होगा", "होगी", "बताएं", "दे दो", "मिलेगी", "चाहता", "चाहती",
            "छात्र", "और", "या",
            # Additional Hindi verbal inflections, modals & future forms
            "देंगे", "दूँगा", "दूंगा", "दोगे", "देना", "करेंगे", "करूंगा", "करूँगा", "करोगे", "करना",
            "करते", "जाएंगे", "जाऊंगा", "जाऊँगा", "जाओगे", "जाना", "आएंगे", "आऊंगा", "आऊँगा", "आओगे",
            "आना", "लेंगे", "लूंगा", "लूँगा", "लोगे", "लेना", "बोलेंगे", "रुकेंगे", "रुकना",
            "सकते", "सकता", "सकती", "सकेंगे", "सकेगा", "सकेगी", "दीजिएगा", "कीजिए", "कीजिएगा",
            "बताइएगा", "होंगे", "होते", "होती", "होता", "रहेंगे", "रहेगा", "रहेगी", "बंद", "चलती"
        }

        # 3. Roman Marathi transliteration markers
        self.mr_roman_markers: Set[str] = {
            "aahe", "ahe", "ahet", "nahi", "nahie", "kay", "kadhi", "kuthe", "kiti",
            "vajata", "varun", "kadun", "sathi", "cha", "chi", "che", "chya", "dya",
            "sanga", "sangal", "havi", "hava", "hve", "mala", "amhala", "tumhala",
            "mahiti", "thamba", "yeil", "jail", "pahije", "kiti", "baddal", "krupaya",
            "namaskar", "sutnar", "kuthoon"
        }

        # 4. Hinglish (Roman Hindi) transliteration markers
        self.hi_roman_markers: Set[str] = {
            "hai", "hain", "nahi", "kya", "kab", "kahan", "kaise", "kitna", "kitne",
            "kitni", "baje", "se", "chahiye", "batao", "bataiye", "dijiye", "milegi",
            "aayegi", "jaana", "mujhe", "humein", "aapko", "bhai", "kripya", "hoga",
            "hogi", "bataen", "de do", "chalta", "chalegi"
        }

        # 5. Common English functional and transit markers
        self.en_markers: Set[str] = {
            "when", "where", "what", "which", "how", "why", "who", "is", "are", "was",
            "were", "can", "could", "will", "would", "please", "tell", "give", "find",
            "show", "bus", "buses", "route", "routes", "timing", "timings", "schedule",
            "pass", "passes", "fare", "fares", "concession", "student", "ticket", "from",
            "to", "at", "for", "the", "i", "you", "me", "my", "we", "our", "station",
            "stop", "depot", "daily", "monthly", "price", "rate", "rates", "cost"
        }

    def detect_script(self, text: str) -> str:
        """Determines the primary script of the input text."""
        devanagari_count = len(re.findall(r'[\u0900-\u097F]', text))
        latin_count = len(re.findall(r'[a-zA-Z]', text))
        digit_count = len(re.findall(r'[0-9\u0966-\u096F]', text))

        if devanagari_count > 0 and latin_count > 0:
            return "mixed"
        elif devanagari_count > 0 and devanagari_count >= latin_count:
            return "devanagari"
        elif latin_count > 0:
            return "latin"
        elif digit_count > 0:
            return "numeric_only"
        return "unknown"

    def detect(
        self,
        text: str,
        stt_language: Optional[str] = None,
        context_language: str = "mr-IN",
        user_preference: Optional[str] = None
    ) -> LanguageDetectionResult:
        """
        Determines the language of an utterance based on text, STT output, and session context.
        """
        clean_text = text.strip()
        if not clean_text:
            return LanguageDetectionResult(
                language=context_language,
                confidence=0.5,
                script="unknown",
                is_code_switched=False,
                is_transliterated=False,
                reason="Empty input, defaulting to active context language"
            )

        script = self.detect_script(clean_text)
        # Split tokens preserving full Devanagari words with combining vowel signs
        tokens = [w.strip(" ,.?!;:()\"'’‘[]{}").lower() for w in clean_text.split() if w.strip(" ,.?!;:()\"'’‘[]{}")]

        # Check for purely numeric or stop-name only queries (e.g. "101", "१०१")
        is_pure_digits = bool(re.fullmatch(r'[\s0-9\u0966-\u096F]+', clean_text))
        if is_pure_digits or (len(tokens) == 1 and tokens[0].isdigit()):
            return LanguageDetectionResult(
                language=context_language,
                confidence=0.85,
                script="numeric_only",
                is_code_switched=False,
                is_transliterated=False,
                reason=f"Numeric route query '{clean_text}', preserving context language '{context_language}'"
            )

        # -------------------------------------------------------------
        # -------------------------------------------------------------
        # Branch 0: Explicit Language Switch Commands (P0 Priority)
        # -------------------------------------------------------------
        t_low = clean_text.lower()
        if any(re.search(pat, t_low, re.IGNORECASE) for pat in [
            r'(?:मुझसे\s+|माझ्यासोबत\s+)?हिंदी(?:में|मध्ये|त)?\s*(?:बात\s*करो|बोलिए|बोलो|बोल|सांगा|बताओ)',
            r'\b(?:speak|talk)\s+(?:with\s+me\s+)?(?:in\s+)?hindi\b',
            r'(?:हिंदी\s*बोलिए|हिंदी\s*बोलो|हिंदी\s*में\s*बात\s*करो|हिंदी\s*में\s*बोलो)',
            r'^(?:हिंदी|hindi)$'
        ]):
            return LanguageDetectionResult(
                language="hi-IN",
                confidence=1.0,
                script=script,
                is_code_switched=False,
                is_transliterated=False,
                reason="Explicit user command to switch to Hindi"
            )

        if any(re.search(pat, t_low, re.IGNORECASE) for pat in [
            r'(?:माझ्यासोबत\s+)?मराठी(?:मध्ये|त)?\s*(?:बोल|बोला|सांगा|करा)',
            r'\b(?:speak|talk)\s+(?:with\s+me\s+)?(?:in\s+)?marathi\b',
            r'(?:मराठी\s*बोला|मराठी\s*बोल|मराठी\s*भाषा\s*करा)',
            r'^(?:मराठी|marathi)$'
        ]):
            return LanguageDetectionResult(
                language="mr-IN",
                confidence=1.0,
                script=script,
                is_code_switched=False,
                is_transliterated=False,
                reason="Explicit user command to switch to Marathi"
            )

        if any(re.search(pat, t_low, re.IGNORECASE) for pat in [
            r'\b(?:speak|talk)\s+(?:with\s+me\s+)?(?:in\s+)?english\b',
            r'(?:इंग्रजीत|इंग्लिशमध्ये|इंग्रजीमध्ये)\s*(?:बोल|बोला|सांगा)',
            r'\bplease\s+(?:talk|speak)\s+in\s+english\b',
            r'^(?:इंग्रजी|इंग्लिश|english)$'
        ]):
            return LanguageDetectionResult(
                language="en-IN",
                confidence=1.0,
                script=script,
                is_code_switched=False,
                is_transliterated=False,
                reason="Explicit user command to switch to English"
            )

        # Branch A: Devanagari Script (Marathi vs Hindi)
        # -------------------------------------------------------------
        if script == "devanagari" or (script == "mixed" and len(re.findall(r'[\u0900-\u097F]', clean_text)) > len(re.findall(r'[a-zA-Z]', clean_text))):
            mr_hits = sum(1 for t in tokens if t in self.mr_devanagari_markers)
            hi_hits = sum(1 for t in tokens if t in self.hi_devanagari_markers)

            # Check common multi-word Hindi particles like "के लिए"
            if "के लिए" in clean_text:
                hi_hits += 2

            # Distinguish strong grammatical differentiators:
            # "आहे" / "आहेत" / "नाही" -> Strictly Marathi
            # "है" / "हैं" / "नहीं" -> Strictly Hindi
            if any(w in clean_text for w in ["आहे", "आहेत", "नाही", "नाहीये", "कधी", "कुठे", "वरून", "साठी", "पासून", "पासुन", "जायचं", "जायचे", "देतील", "करतील"]):
                mr_hits += 3
            if any(w in clean_text for w in ["है", "हैं", "नहीं", "कब", "कहाँ", "से", "बताइए", "चाहिए", "देंगे", "करेंगे", "जाएंगे", "सकते", "सकता", "सकती"]):
                hi_hits += 3

            # Acoustic STT weighting
            if stt_language == "mr-IN":
                mr_hits += 2
            elif stt_language == "hi-IN":
                hi_hits += 2

            if mr_hits > hi_hits:
                conf = min(0.98, 0.75 + (mr_hits - hi_hits) * 0.1)
                return LanguageDetectionResult(
                    language="mr-IN",
                    confidence=conf,
                    script="devanagari",
                    is_code_switched=(script == "mixed"),
                    is_transliterated=False,
                    reason=f"Devanagari Marathi markers match (mr={mr_hits}, hi={hi_hits})"
                )
            elif hi_hits > mr_hits:
                conf = min(0.98, 0.75 + (hi_hits - mr_hits) * 0.1)
                return LanguageDetectionResult(
                    language="hi-IN",
                    confidence=conf,
                    script="devanagari",
                    is_code_switched=(script == "mixed"),
                    is_transliterated=False,
                    reason=f"Devanagari Hindi markers match (hi={hi_hits}, mr={mr_hits})"
                )
            else:
                # Tie: use STT language if provided and Indic
                if stt_language in ("mr-IN", "hi-IN"):
                    return LanguageDetectionResult(
                        language=stt_language,
                        confidence=0.80,
                        script="devanagari",
                        is_code_switched=(script == "mixed"),
                        is_transliterated=False,
                        reason=f"Devanagari tie resolved by STT acoustic detection: {stt_language}"
                    )
                # Fallback to current context
                return LanguageDetectionResult(
                    language=context_language if context_language in ("mr-IN", "hi-IN") else "mr-IN",
                    confidence=0.70,
                    script="devanagari",
                    is_code_switched=(script == "mixed"),
                    is_transliterated=False,
                    reason="Devanagari neutral phrase, preserving session language"
                )

        # -------------------------------------------------------------
        # Branch B: Latin / Roman Script (English, Roman Marathi, Hinglish)
        # -------------------------------------------------------------
        if script in ("latin", "mixed"):
            en_hits = sum(1 for t in tokens if t in self.en_markers)
            mr_rom_hits = sum(1 for t in tokens if t in self.mr_roman_markers)
            hi_rom_hits = sum(1 for t in tokens if t in self.hi_roman_markers)

            # Check strong Roman Marathi signals (e.g. 'varun', 'sathi', 'kiti vajata', 'aahe')
            if any(term in clean_text.lower() for term in ["varun", "sathi", "kiti vajata", "aahe", "ahe", "sangal", "chi mahiti"]):
                mr_rom_hits += 3

            # Check strong Hinglish signals (e.g. 'kitne ka hai', 'batao', 'kab aayegi', 'milegi kya')
            if any(term in clean_text.lower() for term in ["kitne ka hai", "batao", "bataiye", "kab aayegi", "milegi kya", "chahiye"]):
                hi_rom_hits += 3

            # If acoustic STT indicated en-IN and English markers dominate:
            if stt_language == "en-IN":
                en_hits += 2

            # Determine dominant Roman language
            if mr_rom_hits > hi_rom_hits and mr_rom_hits >= 1:
                conf = min(0.95, 0.75 + mr_rom_hits * 0.1)
                return LanguageDetectionResult(
                    language="mr-IN",
                    confidence=conf,
                    script="latin",
                    is_code_switched=True,
                    is_transliterated=True,
                    reason=f"Roman Marathi detected (hits: {mr_rom_hits})"
                )
            elif hi_rom_hits > mr_rom_hits and hi_rom_hits >= 1:
                conf = min(0.95, 0.75 + hi_rom_hits * 0.1)
                return LanguageDetectionResult(
                    language="hi-IN",
                    confidence=conf,
                    script="latin",
                    is_code_switched=True,
                    is_transliterated=True,
                    reason=f"Hinglish detected (hits: {hi_rom_hits})"
                )
            elif en_hits > 0 or stt_language == "en-IN":
                conf = min(0.98, 0.75 + en_hits * 0.08)
                return LanguageDetectionResult(
                    language="en-IN",
                    confidence=conf,
                    script="latin",
                    is_code_switched=(mr_rom_hits > 0 or hi_rom_hits > 0),
                    is_transliterated=False,
                    reason=f"English vocabulary match (hits: {en_hits})"
                )

        # -------------------------------------------------------------
        # Branch C: Defer to STT Acoustic language if available
        # -------------------------------------------------------------
        if stt_language and stt_language in ("mr-IN", "hi-IN", "en-IN"):
            return LanguageDetectionResult(
                language=stt_language,
                confidence=0.85,
                script=script,
                is_code_switched=False,
                is_transliterated=False,
                reason=f"Resolved via Sarvam STT acoustic detection: {stt_language}"
            )

        # -------------------------------------------------------------
        # Branch D: Fallback to Context Language with Low Confidence
        # -------------------------------------------------------------
        return LanguageDetectionResult(
            language=context_language,
            confidence=0.55,
            script=script,
            is_code_switched=False,
            is_transliterated=False,
            reason="Ambiguous input, maintaining conversation context"
        )


# Global singleton
language_detector = MultilingualDetector()
