# CityLinc — Final Targeted Intent & WebSocket Fixes Verification Report

**Date**: November 3, 2026  
**Workspace**: `D:\Calling Agent New`  
**Branch**: `Neha`  
**Overall Verdict**: **PASS**  

---

## 1. Executive Summary & Root Cause Analysis

A targeted audit and correction was performed following the November 3, 2026 voice test log review. Six confirmed issues were investigated and corrected under strict protection rules (no LLM, no architecture rebuild, preserved raw datasets, preserved 3 calls/day in IST and 180s limits).

### Issue 1: Nearby Bus Stop Intent
- **Observed Problem**: Queries such as *"मेरे पंचवटी के पास कौन सा बस स्टॉप है?"*, *"मेरे पास का सबसे नजदीक बस स्टॉप कहाँ है?"*, *"Najdik wala bus stop"*, *"माझ्या जवळचा बस स्टॉप कोणता आहे?"*, and *"पंचवटीजवळचे बस स्टॉप सांगा"* were misrouted to route queries, bus pass queries, or goodbye termination.
- **Root Cause**: Proximity patterns collided with pass tokens ("पास") and termination tokens ("stop"). Furthermore, no dedicated nearby-stop resolver handled landmark-anchored bus stop lookups or prompted the user for their location when missing.
- **Resolution**:
  - Implemented `NEARBY_STOP_PATTERNS` regex matching user proximity inquiries across Hindi, Marathi, and English.
  - If a landmark/locality is provided (e.g. Panchavati), the engine resolves verified stop names directly from `location_service` (e.g., Panchavati Karanja, Panchavati Depot Corner).
  - If user location is unavailable, the agent politely prompts for their locality/landmark without inventing location.

### Issue 2: Bus Pass vs Proximity Disambiguation
- **Observed Problem**: The Hindi word *"पास"* (proximity) frequently collided with *"बस पास"* (transit pass). Questions about where to get/make a pass ("पास कहाँ बनता है?", "पास कसा काढायचा?") were confused with fare/rate questions or proximity queries.
- **Root Cause**: Overbroad pass token matching treated proximity words as pass queries, while pass counter inquiries returned generic fare tables or unverified information.
- **Resolution**:
  - Separated proximity context (*"पास", "के पास", "जवळ"*) from bus-pass context (*"बस पास", "पास बनवाना", "पास निकालना", "मासिक पास"*).
  - Segregated application/counter queries from fare queries: when asked about pass application counters/offices, the agent provides a verified conservative response stating that official counter schedules and addresses are currently unavailable and directs callers to the CBS or Nimani main bus stand inquiry counter. No pass counters or opening hours are invented.
  - Preserved verified bus pass tariff estimates (1-day ₹110, monthly ₹2,100) for explicit rate questions.

### Issue 3: Emergency Intent — Dizziness / Medical Distress on Bus
- **Observed Problem**: Phrases such as *"बस में किसी को चक्कर आ रहे हैं।"*, *"यात्री को चक्कर आ रहे हैं।"*, *"बस में कोई बेहोश हो रहा है।"*, *"प्रवाशाला चक्कर येत आहेत."* were misclassified as journey queries prompting for origin and destination.
- **Root Cause**: Lack of an early emergency classification tier before transit routing logic.
- **Resolution**:
  - Added `EMERGENCY_PATTERNS` prioritizing dizziness (*"चक्कर"*), fainting/unconsciousness (*"बेहोश", "बेशुद्ध"*), vomiting, and chest pain before route planning.
  - Implemented concise, conservative emergency guidance in Hindi, Marathi, and English:
    1. Alert the conductor/driver immediately.
    2. Ask the driver to pull over safely if urgent help is needed.
    3. Call India's national emergency helpline **112** if the passenger is unconscious, has breathing difficulty, or has severe symptoms.
    4. Explicitly avoids recommending food, water, or medicine to an unconscious person.
    5. Never claims an ambulance or emergency service has been dispatched.

### Issue 4: Goodbye Intent False Positive ("Najdik wala bus stop")
- **Observed Problem**: The user query *"Najdik wala bus stop"* triggered a goodbye termination response.
- **Root Cause**: `TERMINATION_PATTERNS` included bare `"stop"` as a standalone token pattern `(?<![\wऀ-ॿ])stop(?![\wऀ-ॿ])`, which matched the word `"stop"` in any English or Hinglish transit query. Additionally, bare `"थांबवा"` was overly broad.
- **Resolution**:
  - Removed bare `"stop"` and `"थांबवा"` from `TERMINATION_PATTERNS`. Replaced with explicit call-ending phrases: `stop the call`, `end the call`, and anchored `^(?:exit|quit)$`.
  - Added a transit token immunity guard in `check_termination`: if an utterance contains transit tokens like `"bus"`, `"stop"`, `"थांबा"`, `"स्थानक"`, `"स्टॉप"`, `"नजदीक"`, `"जवळ"`, it cannot trigger goodbye unless explicit call termination verbs (`"कॉल बंद करा"`, `"stop the call"`, etc.) are present.

### Issue 5: 180-Second WebSocket Close Race (`RuntimeError`)
- **Observed Problem**: Logs showed `RuntimeError: Cannot call "send" once a close message has been sent.` when the 180-second hard timeout triggered while an in-flight STT or TTS request was still finishing.
- **Root Cause**: Fast Starlette/FastAPI WebSocket transitions client state to closed upon `websocket.close()`. The ongoing coroutine in `websocket_conversation_endpoint` subsequently called `websocket.send_json()` without checking socket connection status.
- **Resolution**:
  - Added connection tracking variable `is_ws_closed = False`.
  - Implemented `safe_send_json(payload: dict) -> bool` and `safe_close(code, reason)` wrappers checking `is_ws_closed` and `websocket.client_state == WebSocketState.CONNECTED`, suppressing `RuntimeError` on race conditions.
  - Added `if is_ws_closed: continue/break` guards before STT, after STT, before context processing, before TTS, and after TTS.
  - Marked `is_ws_closed = True` in `WebSocketDisconnect`, error handler, and `finally` block to ensure complete idempotency.

### Issue 6: Route Result Validation (Panchavati to Nimani)
- **Observed Problem**: Querying Panchavati to Nimani resulted in Route 259C with boarding at *Panchavati Depot Corner (U)* and alighting at *Panchavati Karanja 1 (U)* (an inverted sub-corridor journey).
- **Root Cause**: Both Panchavati and Nimani mapped to all 6 station IDs `{691, 694, 704, 707, 956, 1194}` in `resolve_place`. Because Route 259C visited Depot Corner (691) before Karanja (707), the algorithm chose boarding at Depot Corner (Nimani) and alighting at Karanja (Panchavati).
- **Resolution**:
  - Disambiguated Panchavati vs Nimani sub-corridor clusters in `TransitRouteEngine.search_routes` without altering raw datasets:
    - Origin Panchavati $\rightarrow$ Karanja station set `{704, 707, 956}`.
    - Destination Nimani $\rightarrow$ Depot Corner station set `{691, 694, 1194}`.
    - Panchavati to Nimani now boards at *Panchavati Karanja 2 (U)* and alights at *Panchavati Depot Corner (U)*.
    - Nimani to Panchavati boards at *Panchavati Depot Corner (U)* and alights at *Panchavati Karanja 1 (U)*.

---

## 2. Files Changed

| File | Exact Changes |
| :--- | :--- |
| `backend/app/services/route_engine.py` | Added origin/dest cluster disambiguation for Panchavati vs Nimani sub-corridor stations in `search_routes`. Raw GTFS/CSV datasets untouched. |
| `backend/app/services/conversation_context.py` | 1. Added `NEARBY_STOP_PATTERNS` and `EMERGENCY_PATTERNS`.<br>2. Removed bare `"stop"` and `"थांबवा"` from `TERMINATION_PATTERNS` and added transit token immunity guard in `check_termination`.<br>3. Disambiguated `BUS_PASS_APPLICATION` (counter unavailable notice) vs `BUS_PASS_QUERY` (verified tariff rates) and proximity context.<br>4. Added emergency response handler with conservative 112 guidance.<br>5. Added nearby stop handler resolving locality stops or asking for landmark. |
| `backend/app/api/websocket_routes.py` | 1. Imported `WebSocketState` from `starlette.websockets`.<br>2. Added `safe_send_json()` and `safe_close()` wrappers with connection state checking and `RuntimeError` suppression.<br>3. Added `if is_ws_closed: continue` checkpoints across STT, context engine, and TTS pipelines.<br>4. Replaced all direct `websocket.send_json` and `websocket.close` calls with safe wrappers. |
| `tests/test_final_targeted_intent_websocket_fixes.py` | Added 30 targeted regression tests covering all 6 problem domains. |

---

## 3. Test & Verification Results

### A. Targeted Regression Suite (`tests/test_final_targeted_intent_websocket_fixes.py`)
- **Test execution**: `pytest tests/test_final_targeted_intent_websocket_fixes.py -v`
- **Results**: **30 passed in 9.56s (100% pass rate)**
  - *TestNearbyBusStopIntent* (5 tests):
    - "मेरे पंचवटी के पास कौन सा बस स्टॉप है?" $\rightarrow$ PASS
    - "पंचवटीजवळचे बस स्टॉप सांगा" $\rightarrow$ PASS
    - "मेरे पास का सबसे नजदीक बस स्टॉप कहाँ है?" $\rightarrow$ PASS
    - "Najdik wala bus stop" $\rightarrow$ PASS
    - "माझ्या जवळचा बस स्टॉप कोणता आहे?" $\rightarrow$ PASS
  - *TestBusPassDisambiguation* (5 tests):
    - "दत्ता मंदिर के पास कौन सा अस्पताल है?" $\rightarrow$ PASS (HOSPITAL_LOOKUP, not bus pass)
    - "बस पास का किराया कितना है?" $\rightarrow$ PASS (verified ₹110 / ₹2,100 rates)
    - "पास कहाँ बनता है?" (Hindi) $\rightarrow$ PASS (conservative uninvented counter notice)
    - "पास कसा काढायचा?" (Marathi) $\rightarrow$ PASS (conservative uninvented counter notice)
    - "Where can I get a bus pass?" (English) $\rightarrow$ PASS (conservative uninvented counter notice)
  - *TestEmergencyDizzinessIntent* (5 tests):
    - "बस में किसी को चक्कर आ रहे हैं।" $\rightarrow$ PASS (immediate conductor alert, 112 guidance)
    - "यात्री को चक्कर आ रहे हैं।" $\rightarrow$ PASS (immediate conductor alert, 112 guidance)
    - "बस में कोई बेहोश हो रहा है।" $\rightarrow$ PASS (immediate conductor alert, 112 guidance)
    - "प्रवाशाला चक्कर येत आहेत." $\rightarrow$ PASS (immediate conductor alert, 112 guidance)
    - "Someone is unconscious on the bus." $\rightarrow$ PASS (immediate conductor alert, 112 guidance)
  - *TestGoodbyeFalsePositive* (11 tests):
    - "Najdik wala bus stop" not goodbye $\rightarrow$ PASS
    - "पुढचा बस स्टॉप कोणता आहे" not goodbye $\rightarrow$ PASS
    - "या बसचा शेवटचा स्टॉप कोणता" not goodbye $\rightarrow$ PASS
    - "stop list saanga" not goodbye $\rightarrow$ PASS
    - "which stop comes next" not goodbye $\rightarrow$ PASS
    - Genuine farewells ("गुडबाय", "bye", "bye bye", "अलविदा", "कॉल बंद करा", "that's all thank you") $\rightarrow$ PASS
  - *TestWebSocketCloseRace* (2 tests):
    - `safe_send_json` suppresses `RuntimeError` after close $\rightarrow$ PASS
    - `safe_send_json` handles concurrency race $\rightarrow$ PASS
  - *TestPanchavatiToNimaniValidation* (2 tests):
    - Panchavati $\rightarrow$ Nimani: Boarding *Panchavati Karanja 2 (U)*, Alighting *Panchavati Depot Corner (U)* $\rightarrow$ PASS
    - Nimani $\rightarrow$ Panchavati: Boarding *Panchavati Depot Corner (U)*, Alighting *Panchavati Karanja 1 (U)* $\rightarrow$ PASS

### B. Previous Regression Suites
- `tests/test_final_targeted_corrections.py`: **15 passed in 3.15s**
- `tests/test_final_small_improvements.py`: **12 passed in 63.35s**
- `tests/test_phase_l1_targeted_fixes.py`: **8 passed in 9.83s**
- `tests/test_multi_hop_journey_planner.py`: **20 passed in 36.81s**
- `node tests/test_app_capture_lifecycle.js`: **All 15 deterministic requirements & 7 regression scenarios PASSED**
- **Full Pytest Suite**: **298 passed, 0 failed in 246.15s**

---

## 4. Protected Invariants Confirmation

| Invariant | Status | Verification Detail |
| :--- | :--- | :--- |
| **No Architecture Rebuild** | Preserved | Architecture unchanged; enhancements modularized in existing services. |
| **No LLM Introduction** | Preserved | Deterministic regex, graph algorithms, and rule engines only. |
| **STT / TTS Models Unchanged** | Preserved | Sarvam API clients and configs untouched. |
| **VAD / Barge-In / FIFO Unchanged** | Preserved | Frontend capture, VAD parameters, and FIFO queue intact (verified via test suite). |
| **Call Duration Limits** | Preserved | 160s warning and 180s hard limit strictly maintained. |
| **Caller Daily Limit** | Preserved | 3 connected calls per caller per IST day preserved in SQLite storage. |
| **Raw Datasets & Canonical IDs** | Preserved | No modifications to `data/raw/` files or canonical station IDs. |
| **No Unverified VoiceLink Claim** | Compliant | Simulated WebSocket/integration verification only; no claim of real inbound phone call made. |

---

## 5. Remaining Limitations

1. **Live Bus Pass Counter Addresses/Timings**: CityLinc does not publish an official real-time machine-readable feed of pass issuing counters or operating hours. The agent strictly complies with safety rules by directing passengers to the CBS or Nimani main inquiry desks rather than hallucinating addresses.
2. **Current Tariff Gazette Verification**: Pass tariff numbers are based on the Jan 2023 schedule. Live official gazette verification remains pending; disclaimers are preserved.
3. **Telephony Inbound Integration**: Testing was performed via the WebSocket and mock test suites. Full end-to-end VoiceLink telephony verification requires a live carrier call dialed to the configured phone number.

---

## 6. Verdict

**PASS** — All six issues have been resolved cleanly with 100% test pass rates across both targeted and full regression suites. System is stable and ready for inspection.
