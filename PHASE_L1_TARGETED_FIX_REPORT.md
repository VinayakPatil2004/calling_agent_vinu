# Phase L1: Targeted Final Browser Test Fixes — Release Report

**Workspace**: `D:\Calling Agent New`  
**Date**: November 3, 2026  
**Status**: Completed & Verified  

---

## 1. Executive Summary

This engineering cycle addressed the five confirmed defects identified during live browser voice conversation testing of the CityLinc calling agent:
1. **Marathi Stop Normalization**: Spoken phonetic slips `"पंचवडी"` and `"हाशिकरोड"` did not resolve to canonical CityLinc transit hubs.
2. **Hindi Response Consistency**: After switching to Hindi, acoustic STT metadata or unmapped Marathi entities caused the agent to leak Marathi responses or route clarification strings.
3. **Repeat Intent**: Natural repetition requests ("परत सांग मला", "Can you please repeat again?") were parsed as destination tokens, causing route lookup errors.
4. **Goodbye & Conversation Closing**: Closing phrases like "No, thank you" were misclassified as polite acknowledgements, prompting repeated follow-up questioning instead of safely ending the call.
5. **Fare Safety Notice**: Fare responses lacked an explicit caveat stating that tariff values reflect the January 2023 tariff slab and live gazette verification remains pending.

All five defects were reproduced, diagnosed at root cause, remediated with high-precision targeted fixes, and verified via automated regression suites without introducing LLMs or modifying raw data/audio capture infrastructure.

---

## 2. Root Cause Analysis & Targeted Remediations

### Defect 1: Marathi Stop Normalization
- **Root Cause**:
  - `LOCATION_ALIASES["panchavati"]` lacked the colloquial/phonetic variant `"पंचवडी"` (and inflected form `"पंचवडीला"`).
  - `LOCATION_ALIASES["nashik road"]` lacked acoustic variant `"हाशिकरोड"` (and inflected forms `"हाशिक रोड"`, `"हाशिकरोडला"`).
  - `route_engine.resolve_place` had no direct verified station clusters for Panchavati (`[691, 694, 704, 707, 956, 1194]`) or Nashik Road (`[529, 530, 787, 1440, 1683]`), resulting in empty station ID sets.
- **Remediation**:
  - In `backend/app/services/citylinc_knowledge.py`: Added `"पंचवडी"`, `"पंचवडीला"`, `"पंचवटीला"`, `"panchavadi"` to `LOCATION_ALIASES["panchavati"]`. Added `"हाशिकरोड"`, `"हाशिक रोड"`, `"हाशिकरोडला"` to `LOCATION_ALIASES["nashik road"]`.
  - In `backend/app/services/route_engine.py`: Added explicit verified clusters in `resolve_place` mapping `"पंचवडी"` to canonical Panchavati (`[691, 694, 704, 707, 956, 1194]`) and `"हाशिकरोड"` to canonical Nashik Road (`[529, 530, 787, 1440, 1683]`).
  - In `backend/app/services/conversation_context.py`: Added oblique case suffix normalizations in `_clean_token` (`"पंचवडीला"` -> `"पंचवडी"`, `"हाशिकरोडला"` -> `"हाशिकरोड"`).

---

### Defect 2: Hindi Response Consistency & Language Isolation
- **Root Cause**:
  - In `LOCATION_ALIASES["civil hospital"]`, only the Marathi conjunct `"सिव्हिल"` was registered; the Hindi spelling `"सिविल हॉस्पिटल"` was absent.
  - When the user requested `"मुझे सिविल हॉस्पिटल से नाशिक रोड जाना है।"` in Hindi, `search_routes` failed to resolve `"सिविल हॉस्पिटल"` and returned a hardcoded Marathi clarification string (`मला 'सिविल हॉस्पिटल' हा थांबा सापडला नाही...`). `_handle_route_search` returned this string directly without checking session language.
  - Furthermore, `ConversationSession` and `SessionTransitContext` lacked a sticky explicit language preference lock: whenever Sarvam STT returned acoustic metadata `mr-IN` or Devanagari stop names were parsed, the session language was silently reverted to Marathi.
- **Remediation**:
  - In `citylinc_knowledge.py`: Added Hindi variants `"सिविल हॉस्पिटल"`, `"सिविल"`, `"सिविल अस्पताल"` to `LOCATION_ALIASES["civil hospital"]`.
  - In `conversation_manager.py` & `conversation_context.py`: Added `has_explicit_language_preference` and `explicit_language` tracking. When a user explicitly requests Hindi (e.g., `"माझ्यासोबत हिंदीमध्ये बोल"`), the language is locked.
  - In `backend/app/api/websocket_routes.py`: When explicit preference is locked, STT requests use that language, dynamic acoustic/lexical overrides are prohibited unless an explicit switch command is issued, and TTS synthesis language is strictly bound to `response_lang`.
  - In `conversation_context.py`: Localized all route search fallback messages across Marathi, Hindi, and English.

---

### Defect 3: Repeat Intent
- **Root Cause**:
  - The conversation context engine had no `REPEAT_QUERY` intent. Spoken repetition requests ("परत सांग मला", "Can you please repeat again?") fell through to route token extraction, where words like `"सांग"` or `"repeat"` were parsed as destination stops, resulting in invalid route lookups.
- **Remediation**:
  - In `backend/app/services/conversation_context.py`:
    - Added `IntentStr("REPEAT_QUERY")`.
    - Added comprehensive regex patterns `REPEAT_PATTERNS` covering Marathi (`"परत सांग"`, `"पुन्हा सांग"`, `"एकदा रिपीट कर"`, `"परत सांग मला"`), Hindi (`"फिर से बताइए"`, `"दोबारा बोलिए"`, `"एक बार फिर से बोलिए"`), and English (`"repeat that"`, `"can you say that again"`, `"can you please repeat again"`).
    - Integrated `check_repeat` in `detect_intent` with high priority.
    - Wrapped `process_turn` to persistently record `ctx.last_response_text` across all response pathways without modifying journey slots or overwriting previous response history.
    - Replays the last substantive response in the caller's active language.

---

### Defect 4: Goodbye and Conversation Closing
- **Root Cause**:
  - Closing utterances such as `"No, thank you"` were intercepted by `POLITE_THANKS_PATTERNS`, repeatedly prompting `"Would you like any further assistance regarding bus routes, timetables, or passes?"`. Standalone closing phrases like `"Have a nice day"` fell through to `CLARIFICATION`.
- **Remediation**:
  - In `conversation_context.py`:
    - Updated `TERMINATION_PATTERNS` with precedence over `POLITE_THANKS_PATTERNS` to catch `"No"`, `"No, thank you"`, `"Have a nice day"`, `"Goodbye"`, Marathi equivalents (`"नाही धन्यवाद"`, `"काही नको"`, `"शुभ दिवस"`), and Hindi equivalents (`"नहीं शुक्रिया"`, `"कुछ नहीं चाहिए"`, `"आपका दिन शुभ हो"`).
    - Handled `GOODBYE` with a single polite closing phrase and entered safe termination state (`ctx.is_terminated = True`, `res["is_terminated"] = True`).
    - Eliminated repetitive follow-up prompting after termination.

---

### Defect 5: Fare Safety Notice
- **Root Cause**:
  - Ticket and pass fare responses provided fare numbers without consistently communicating that they reflect the January 2023 tariff slab and live official gazette verification remains pending.
- **Remediation**:
  - In `conversation_context.py`:
    - Updated `BUS_PASS_QUERY`, `FARE_QUERY`, and route search fare notes to append explicit disclaimers:
      - **Marathi**: `(टीप: हे दर १ जानेवारी २०२३ च्या अधिकृत टप्प्यांवर आधारित असून सध्याचे दर पडताळणी प्रलंबित आहेत.)`
      - **Hindi**: `(नोट: ये दरें 1 जनवरी 2023 की तालिका पर आधारित हैं और वर्तमान दर सत्यापन लंबित है।)`
      - **English**: `(Note: Rates are based on official Jan-2023 tariff slabs; live official gazette verification is currently pending.)`

---

## 3. Files Modified

| File Path | Description of Changes |
| :--- | :--- |
| `backend/app/services/citylinc_knowledge.py` | Added `"पंचवडी"` to Panchavati aliases, `"हाशिकरोड"` to Nashik Road aliases, and Hindi `"सिविल हॉस्पिटल"` to Civil Hospital aliases. |
| `backend/app/services/route_engine.py` | Added verified station clusters in `resolve_place` for Panchavati, Nashik Road, and Civil Hospital. |
| `backend/app/services/conversation_context.py` | Added `REPEAT_QUERY` intent, sticky explicit language locking, enhanced `TERMINATION_PATTERNS`, oblique suffix normalization, `process_turn` response wrapper, and mandatory fare safety disclaimers. |
| `backend/app/orchestrator/conversation_manager.py` | Added `has_explicit_language_preference` and `explicit_language` flags to `ConversationSession` with explicit override semantics. |
| `backend/app/api/websocket_routes.py` | Locked STT request language to explicit session language, prevented dynamic acoustic overrides when locked, and bound TTS synthesis language to generated text. |
| `tests/test_phase_l1_targeted_fixes.py` | Created dedicated test suite validating all 5 defects across 8 comprehensive tests. |

---

## 4. Verification & Regression Results

### 4.1 Mandatory Test Utterances

| Test Utterance | Expected Intent | Language | Canonical Stop / Behavior | Status |
| :--- | :--- | :--- | :--- | :--- |
| `"मला सीबीएसवरून पंचवडीला जायचे आहे."` | `ROUTE_QUERY` | `mr-IN` | Origin: CBS, Dest: Panchavati (`[691, 694, 704, 707, 956, 1194]`), Direct bus offered | **PASSED** |
| `"मला सीबीएसवरून हाशिकरोडला जायचे आहे."` | `ROUTE_QUERY` | `mr-IN` | Origin: CBS, Dest: Nashik Road (`[529, 530, 787, 1440, 1683]`), Direct bus offered | **PASSED** |
| `"माझ्यासोबत हिंदीमध्ये बोल."` | `LANGUAGE_CHANGE` | `hi-IN` | Switches session language to `hi-IN`, sets explicit lock | **PASSED** |
| `"मुझे सिविल हॉस्पिटल से नाशिक रोड जाना है।"` | `ROUTE_QUERY` | `hi-IN` | Civil Hospital to Nashik Road, pure Hindi response without Marathi leakage | **PASSED** |
| `"परत सांग मला."` | `REPEAT_QUERY` | `mr-IN` | Replays previous assistant response exactly, preserves slots | **PASSED** |
| `"Can you please repeat again?"` | `REPEAT_QUERY` | `en-IN` | Replays previous English response exactly, preserves slots | **PASSED** |
| `"No, thank you."` | `GOODBYE` | `en-IN` | Terminates session (`is_terminated=True`), no follow-up questioning | **PASSED** |
| `"Have a nice day."` | `GOODBYE` | `en-IN` | Friendly closing response, enters safe termination state | **PASSED** |
| `"बस पासचे दर काय आहेत?"` | `BUS_PASS_QUERY` | `mr-IN` | Displays pass rates with Jan-2023 tariff notice & pending verification | **PASSED** |

### 4.2 Targeted Fix Suite (`tests/test_phase_l1_targeted_fixes.py`)
- **8 passed in 8.70s** (100% pass rate).

### 4.3 Node.js Audio Capture Lifecycle Suite (`test_app_capture_lifecycle.js`)
- **15 Requirements (Req 1–15) & 7 Regression Scenarios (A–G) PASSED** (100% pass rate).

### 4.4 Full Regression Suite (`pytest tests/`)
- **241 passed in 149.46s** (100% pass rate across entire 241-test suite).
- All preceding phase tests (telephony, barge-in, route engine, multi-hop planner, timetable, knowledge graph) remain 100% green.

---

## 5. Strict Protection Checklist

- [x] **No LLM introduced**: The system remains strictly deterministic, rules- and timetable-driven.
- [x] **Raw datasets protected**: Zero modifications to files in `data/raw/` or station IDs.
- [x] **VAD, barge-in & audio capture intact**: Parameters (`0.046`, `160ms`, `2 frames`, FIFO queue depth 2) preserved.
- [x] **Sarvam models preserved**: STT `saaras:v4` and TTS `bulbul:v3` unchanged.
- [x] **Telephony & WebSocket contracts preserved**: VoiceLink PCMA and browser WebSocket interfaces maintained.
- [x] **No deployment performed**: All changes verified locally; server left undeployed as instructed.

---

## 6. Remaining Limitations & Recommendations

1. **Unregistered Dialectal Stops**: Colloquial abbreviations beyond Nashik's primary hubs (e.g. specialized factory gates or unlisted rural wadis) should be continuously registered in `LOCATION_ALIASES` as new audio transcripts are gathered.
2. **Sarvam Indic STT Phonetic Slips**: Severe acoustic noise in telephony environments can occasionally truncate final syllables (e.g. "नाशिक" as "हाशि"). The suffix-stripping and alias tolerance engine handles common variants, but extremely truncated single-syllable audio may prompt clarification.
3. **Official Live Gazette Verification**: Once the 2024–2026 CityLinc gazette tariff is officially published by NMPML/NMC, the Jan-2023 tariff tables in `fare_service.py` should be updated with verified final values.
