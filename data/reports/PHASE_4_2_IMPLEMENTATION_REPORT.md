# CityLinc Voice Agent — Phase 4.2 Implementation & Regression Report
**Document ID:** `CITYLINC-PHASE-4.2-IMPL-REPORT`  
**Workspace:** `D:\calling agent`  
**Status:** COMPLETE (89 / 89 Tests Passed — 100%)  
**Date:** November 1, 2026  

---

## 1. Executive Summary

Phase 4.2 addressed critical conversation intelligence, routing accuracy, spatial hospital ranking, and multilingual number pronunciation issues observed during real-world voice interaction in Marathi, Hindi, and English.

All fixes were implemented in the existing codebase while strictly preserving:
- All GTFS raw datasets (`all_routes_stops.json` with 12,909 records, `all_route_timetables.csv` with 3,547 trips).
- Verified physical station coordinates (`center_lat`, `center_lon`) with zero synthetic offsets.
- Real-time bidirectional WebSocket contracts (`/ws/conversation`) and 450ms barge-in full-duplex behavior.
- Sarvam STT (`saaras:v4`) and Sarvam TTS (`bulbul:v3`) configurations.

### Test Execution Summary
- **Pre-existing tests:** 78 passed
- **New Phase 4.2 tests:** 11 passed
- **Total test suite:** **89 passed, 0 failed (100% pass rate in 33.62s)**

```
======================= 89 passed, 2 warnings in 33.62s =======================
```

---

## 2. Bug-by-Bug Root Cause Analysis & Fix Details

### Bug 1: Conversation Context Management & Stale Memory Elimination
- **Observed Problem:** When a user queried CBS -> Nashik Road (Route 202) followed by KBT Circle -> Nimani (Route 131/331), subsequent questions ("पुढची बस किती वाजता आहे?") incorrectly returned Route 202 from CBS. Additionally, unserviceable route searches left stale route IDs in memory.
- **Root Cause:**
  1. `route_search` failed to reset `ctx.selected_rno`, `ctx.selected_route_id`, and `ctx.boarding_stop_id` before evaluating a new origin-destination query.
  2. If a journey query had no direct routes and failed or was unserviceable, the handler lacked an explicit `else` termination, leaving stale route identifiers intact.
  3. `next_bus` prioritized internal database route IDs (`str(ctx.selected_route_id)`) over user-facing route numbers (`ctx.selected_rno`), causing variant mismatches.
- **Implemented Fix in `backend/app/services/conversation_context.py`:**
  - **State Reset:** On every new origin-destination detection, previous journey state (`selected_rno`, `selected_route_id`, `boarding_stop_id`, `boarding_stop_name`, `alighting_stop_id`, `alighting_stop_name`, `last_direct_routes`, `last_transfer_routes`) is cleanly cleared before executing search.
  - **Explicit Negative Resolution:** When no direct or transfer routes exist for a query, the engine returns a clear localized response (`"मार्ग आढळला नाही"`) and keeps route identifiers `None`.
  - **Active Journey Prioritization:** Follow-up `next_bus` queries resolve strictly against the active journey's route number and boarding stop. If no route is active, it prompts the user for clarification rather than defaulting to Route 202.

---

### Bug 2: Route Accuracy Engine (Topological Routing & Eponym Resolution)
- **Observed Problem:** User asking for "Anandavalli to Satpur" was falsely given Route 101. Route 101 runs between CBS and MVP/Adgaon and does not serve Anandavalli or Satpur. Lone generic tokens like "Circle" or "Road" also risked matching unintended stations.
- **Root Cause:**
  1. Anandavalli (`आनंदवल्ली`) and CCM (`सिटी सेंटर मॉल`) lacked explicit alias mappings in `route_engine.py` Pass 1 resolution.
  2. In Pass 2 (substring containment), queries without exact matches could match generic transit tokens (`circle`, `road`, `stand`, `nagar`).
  3. When place resolution returned no stations, route search failed silently and fell back to LLM, which hallucinated Route 101.
- **Implemented Fix in `backend/app/services/route_engine.py` & `backend/app/services/citylinc_knowledge.py`:**
  - **Pass 2 Generic Token Guard:** Substring containment matching strictly bypasses lone generic transit keywords (`if clean_q.lower() not in GENERIC_TRANSIT_TOKENS:`).
  - **Dedicated Landmark Mappings:**
    * **Anandavalli:** Mapped to verified stations `{427, 1105}` (`Aanandavalli (D)`, `Aanandavalli (U)`). Direct routes to Satpur: **0**. Valid transfer routes via Shalimar: **3** (Route 238 -> Route 145).
    * **City Centre Mall (CCM):** Mapped to `{392, 448, 449}` (`City Centre Mall`, `Chhatrapati Sambhaji Maharaj Chowk (U)`, `Chhatrapati Sambhaji Maharaj Chowk (D)`). Enables verified bidirectional travel to CBS (Routes 103, 128, 203, 303A, 328A).
    * **ABT / ABB Circle:** Mapped to `{195, 192, 676, 1008}` (`Mahatmanagar`, `Parijat Nagar`). Direct to CBS: Routes 102B, 302, 302A.
    * **Dwarka Circle:** Mapped to `{497, 1078, 1049, 1050, 1287}` (`Kathe Galli Signal`, `Nagji Hospital (U/D)`). Direct to CBS: Routes 104, 304, 201.

---

### Bug 3: Geodesic Hospital Ranking across All 7 Test Locations
- **Observed Problem:** The assistant repeatedly returned MVP Medical College Hospital for diverse Nashik locations (KBT Circle, CCM, CBS, etc.).
- **Root Cause:**
  1. User location phrases containing colloquial landmarks (e.g., CCM, आनंदवल्ली) failed to resolve coordinates, resulting in `user_lat = None`.
  2. Without user coordinates, the assistant fell back to LLM generation, which repeatedly hallucinated MVP Medical College Hospital.
  3. `LocationIntelligenceService` lacked a unified haversine distance helper method.
- **Implemented Fix in `backend/app/services/location_service.py` & `backend/app/services/conversation_context.py`:**
  - Added `haversine_distance_km(lat1, lon1, lat2, lon2)` using the great-circle trigonometric formula.
  - In `nearby_hospital`, `LOCATION_ALIASES` items are sorted by maximum alias length descending to match specific landmark phrases first.
  - Resolved station coordinates are dynamically cached in `ctx.current_location_lat` and `ctx.current_location_lon`.
  - **Verified Geodesic Ranking Results:**
    1. **KBT Circle** -> **District Civil Hospital** (2.06 km) / Hridayaksha (2.07 km)
    2. **ABT Circle** -> **ESIC Hospital, Satpur** (2.15 km)
    3. **Anandavalli** -> **Hridayaksha Cardiac Care Hospital** (1.62 km)
    4. **CCM** -> **District Civil Hospital** (1.65 km)
    5. **CBS** -> **District Civil Hospital** (0.59 km)
    6. **Satpur** -> **NMC Satpur Urban Dispensary** (0.36 km)
    7. **Nashik Road** -> **Bytco Municipal Hospital** (1.73 km)
    - *MVP Medical College Hospital is strictly NOT returned unless user is at Adgaon/MVP.*

---

### Bug 4: Multilingual Number Pronunciation & "आकडे मराठीत बोल"
- **Observed Problem:** Users asking "आकडे मराठीत बोल" were misinterpreted as searching for a bus route. Additionally, numbers in TTS speech (e.g. 101, 202, 331, 08:36, ₹20) were pronounced awkwardly as English digits or ASCII characters.
- **Root Cause:** No dedicated pronunciation intent classifier existed, and raw response strings containing Arabic digits (`101`, `08:36`, `₹20`) were passed directly to the TTS client.
- **Implemented Fix:**
  1. **New Module `backend/app/services/number_formatter.py`:**
     - Full word mappings (0-99 and 100-9999) for Marathi, Hindi, and English.
     - Natural route pronunciation with suffix letters:
       * 101 -> एकशे एक (MR) / एक सौ एक (HI) / One Hundred One (EN)
       * 102 -> एकशे दोन (MR)
       * 151 -> एकशे एकावन्न (MR)
       * 202 -> दोनशे दोन (MR) / दो सौ दो (HI) / Two Hundred Two (EN)
       * 300 -> तीनशे (MR) / तीन सौ (HI) / Three Hundred (EN)
       * 331 -> तीनशे एकतीस (MR) / तीन सौ इकतीस (HI) / Three Hundred Thirty-One (EN)
       * 331A -> तीनशे एकतीस ए (MR)
     - Natural timetable times:
       * 08:36 -> आठ वाजून छत्तीस मिनिटांनी (MR) / आठ बजकर छत्तीस मिनट पर (HI) / Eight Thirty Six (EN)
     - Currency:
       * ₹20 -> वीस रुपये (MR) / बीस रुपये (HI) / Twenty rupees (EN)
  2. **Dedicated Intent `number_pronunciation_preference`:**
     - Matches regex patterns including `आकडे मराठीत बोल`, `संख्या मराठीत सांगा`, `मराठीत आकडे बोल`, `speak numbers in marathi`, `speak numbers in english`.
     - Confirms preference deterministically without launching route queries.
  3. **Separation of Presentation & Acoustic Speech:**
     - UI and API transcripts retain clean numeric figures (`रूट 101`, `₹20`, `08:36`) for readability.
     - Sarvam TTS synthesizes `speech_text` generated via `format_text_for_speech()`.

---

### Bug 5: Timetable and Next Bus Accuracy
- **Observed Problem:** The agent returned stale Route 202 departures or struggled to query intermediate boarding stop ETAs accurately.
- **Root Cause:** Internal route IDs were passed without validating whether the route was active for the current origin-destination pair.
- **Implemented Fix in `backend/app/services/timetable_engine.py` & `backend/app/services/conversation_context.py`:**
  - Resolves departures for the specific boarding stop on the corridor.
  - If current day scheduled trips have completed (e.g. late night), clearly reports tomorrow morning's first departure time from that stop.
  - Respects Indian Standard Time (IST UTC+5:30) wall-clock calculations.

---

## 3. Verification & Regression Matrix

| Test ID | Test Category | Description | Status |
|---|---|---|---|
| 1 | Context Invalidation | Turn 1 CBS -> Nashik Rd, Turn 2 KBT -> Nimani, Turn 3 "पुढची बस" returns Route 131/331 (NOT 202) | **PASS** |
| 2 | Unserviceable Invalidation | Invalid journey search clears `selected_rno`; follow-up asks for clarification | **PASS** |
| 3 | Route Accuracy | Anandavalli -> Satpur returns 0 direct (no Route 101) & 3 valid Shalimar transfers | **PASS** |
| 4 | Bidirectional Routing | CCM <-> CBS, ABT Circle <-> CBS, Dwarka <-> CBS direct routes | **PASS** |
| 5 | Token Filtering | Lone generic tokens ('circle', 'road') return 0; 10 legitimate aliases resolve | **PASS** |
| 6 | Geodesic Ranking | Geodesic ranking for 7 locations matches ground truth (never defaults to MVP) | **PASS** |
| 7 | Direct Hospital Query | "आनंदवल्ली जवळचे हॉस्पिटल कोणते?" resolves Hridayaksha Cardiac Care (1.62 km) | **PASS** |
| 8 | Pronunciation Intent | "आकडे मराठीत बोल" & "speak numbers in marathi" handled as preference | **PASS** |
| 9 | Spoken Numbers | 101, 102, 151, 202, 300, 331, 331A correctly formatted in MR/HI/EN | **PASS** |
| 10 | Spoken Time & Fare | 08:36 -> आठ वाजून छत्तीस मिनिटांनी, ₹20 -> वीस रुपये | **PASS** |
| 11 | Full Speech Text | `format_text_for_speech` converts sentence digits to phonetically natural words | **PASS** |
| 12-89 | Full Regression Suite | All 78 previous tests (Barge-in, STT, TTS, Location API, Knowledge) | **PASS** |

---

## 4. Modified & Backed-Up Files Summary

| File | Backup File | Modifications |
|---|---|---|
| `backend/app/services/number_formatter.py` | *(New File)* | Centralized multilingual number, route, time, currency speech formatter. |
| `backend/app/services/citylinc_knowledge.py` | `.bak_phase4_2` | Added colloquial aliases for Anandavalli, CCM, ABT Circle, and Dwarka. |
| `backend/app/services/route_engine.py` | `.bak_phase4_2` | Generic token skip in Pass 2; explicit stop mappings for CCM, Anandavalli, ABT, Dwarka. |
| `backend/app/services/location_service.py` | `.bak_phase4_2` | Added `haversine_distance_km` method on `LocationIntelligenceService`. |
| `backend/app/services/conversation_context.py` | `.bak_phase4_2` | Implemented `number_pronunciation_preference`, context reset, geodesic hospital ranking, `_response`. |
| `backend/app/api/websocket_routes.py` | `.bak_phase4_2` | Applied `format_text_for_speech` to TTS synthesis inputs. |
| `tests/test_phase4_2_accuracy_and_pronunciation.py` | *(New File)* | 11 dedicated unit and integration tests. |

