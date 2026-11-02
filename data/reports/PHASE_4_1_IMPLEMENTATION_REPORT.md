# CityLinc Voice Agent — Phase 4.1 Conversational Intelligence Implementation Report

**Workspace:** `D:\calling agent`  
**Execution Date:** November 1, 2026  
**Status:** COMPLETE (78 / 78 Tests Passing — 100% Pass Rate)

---

## 1. Executive Summary

Phase 4.1 resolves six critical conversational intelligence bugs uncovered during live multilingual testing in Marathi, Hindi, and English. The implementation fixes semantic collisions, conversational state amnesia, entity extraction failures, landmark routing gaps, Safari audio normalization issues, and basemap tile rendering:

1. **Marathi "पासून" / Bus Pass Collision:** Stripped Marathi ablative prepositions (`पासून` / `पासुन`) before checking standalone `पास` tokens, correctly routing origin queries to route search rather than bus pass pricing.
2. **Inverted English Route Query Syntax:** Extended regex entity extraction to handle natural English phrasing (`reach [Dest] from [Orig]`, `get to [Dest] from [Orig]`, `travel to [Dest] from [Orig]`), resolving origin and destination without inverted role confusion.
3. **Total Hospital Count Fallback vs Entity Count:** Created 4 distinct hospital intents (`hospital_count`, `nearby_hospital`, `hospital_distance`, `hospital_navigation`), returning the exact count (16 verified registry entities) with disclaimers instead of falsely navigating to District Civil Hospital.
4. **Multi-Turn Context Loss & Follow-ups:** Expanded `SessionTransitContext` with session-isolated current user locations, route selections, and timetable state, enabling seamless follow-up queries (`When is the next bus?`, `Which hospital is nearest from here?`).
5. **KBT Circle Landmark Resolution & Transfer Routing:** Mapped KBT Circle on Gangapur Road to boarding stops Thatte Nagar (122, 1108) and Vidya Vikas Circle (582, 1109), excluded lone generic tokens (`circle`, `chowk`, `stand`) from false matches, and enabled multi-leg transfer routing.
6. **Safari MP4 STT Normalization & Error Recovery:** Added foolproof container signature inspection (`ftyp`, `RIFF`, `OggS`, `ID3`, WebM), short payload protection (< 150 bytes), and localized user-friendly retry prompts.
7. **Basemap API Key Watermark Fix:** Replaced CARTO dark tiles (which required an API key and displayed diagonal watermarks) with zero-key multi-layer basemaps (OpenStreetMap Dark Theme default, Standard OSM, Esri Street Map, and Esri Satellite View), combined with cache-busting and no-cache headers.

---

## 2. Bug Fix Breakdown

### Bug 1: Marathi पासून / Bus Pass Collision
- **Root Cause:** In Marathi, `पासून` means "from". `citylinc_knowledge.py` and `conversation_context.py` checked `"पास" in text`, which caused any query with `पासून` (e.g. `मला CBS पासून नाशिक रोडला जायचंय`) to trigger bus pass pricing.
- **Fix:** Stripped `पासून` and `पासुन` before checking for standalone `पास` tokens.
- **Files Modified:**
  - `backend/app/services/citylinc_knowledge.py`
  - `backend/app/services/conversation_context.py`

### Bug 2: Inverted English Route Syntax
- **Root Cause:** Commuters asked `How can I reach Nashik Road from CBS?`. Existing regex patterns only recognized `from X to Y` and `X to Y`, causing destination and origin extraction to invert or fail.
- **Fix:** Added regex pattern support for `(?:reach|get to|go to|travel to)\s+(.+?)\s+from\s+(.+)` alongside standard patterns.
- **Files Modified:**
  - `backend/app/services/conversation_context.py`

### Bug 3: Hospital Entity vs Generic Query Ambiguity
- **Root Cause:** Queries asking `किती Hospitals आहेत टोटल?` matched the keyword `hospital` and fell back to the first entry in `LOCATION_ALIASES`, navigating the user to District Civil Hospital.
- **Fix:** Disambiguated into 4 distinct intents:
  - `hospital_count`: Returns exact count `len(location_service.hospital_entities)` (16 entities) with transit registry context.
  - `nearby_hospital`: Uses user coordinates or prompts for current location.
  - `hospital_distance`: Distinguishes walking distance from alighting bus stops (e.g., 97m).
  - `hospital_navigation`: Provides alighting stops and direct/transfer bus routing.
- **Files Modified:**
  - `backend/app/services/conversation_context.py`
  - `backend/app/services/location_service.py`

### Bug 4: Multi-Turn Conversation Context Amnesia
- **Root Cause:** When a user asked `When is the next bus?` after an initial route search, or asked for nearby hospitals after stating their location (`मी KBT Circle जवळ आहे`), the agent re-prompted for origin/destination.
- **Fix:** Structured multi-turn state in `SessionTransitContext` across turns:
  - Preserved `ctx.selected_rno`, `ctx.origin`, `ctx.destination` for timetable follow-ups.
  - Preserved `ctx.current_location`, `ctx.current_location_lat`, `ctx.current_location_lon` for location-aware queries.
- **Files Modified:**
  - `backend/app/services/conversation_context.py`

### Bug 5: KBT Circle Resolution & Single-Token Circle Matching
- **Root Cause:** KBT Circle is a prominent Nashik landmark but not an official GTFS station name. Queries matched generic token `"circle"`, mapping to 11 unrelated stations across Nashik.
- **Fix:**
  - Explicitly resolved `kbt circle` to Thatte Nagar (122, 1108) and Vidya Vikas Circle (582, 1109).
  - Added `GENERIC_TRANSIT_TOKENS` blacklist (`circle`, `stand`, `chowk`, `phata`, `road`, `depot`, `nagar`) preventing lone generic tokens from matching random transit stops.
- **Files Modified:**
  - `backend/app/services/route_engine.py`

### Bug 6: Safari MP4 STT Normalization & Error Recovery
- **Root Cause:** Safari outputs `audio/mp4` / AAC. STT defaulted unrecognized MIME types to `recording.wav`, causing Sarvam's FFmpeg decoder to fail with HTTP 400 (`Failed to read the file, please check the audio format.`). Additionally, empty/truncated audio crashed the connection.
- **Fix:**
  - Container signature detection (`ftyp`, `RIFF`, `OggS`, `ID3`, WebM).
  - Explicit MP4/M4A/AAC MIME type and filename normalization (`recording.mp4`).
  - Truncated audio protection (< 150 bytes) with localized retry messages (`पुन्हा बोला`).
- **Files Modified:**
  - `backend/app/integrations/sarvam/stt.py`
  - `backend/app/api/websocket_routes.py`

### Basemap API Key Watermark Fix
- **Root Cause:** CartoDB Dark Matter tiles now require an API key and displayed diagonal watermarks (`API KEY REQUIRED carto.com/basemaps/apikey`). Furthermore, browsers cached the old frontend script.
- **Fix:**
  - Configured zero-key multi-layer basemaps in Leaflet: OpenStreetMap Dark Theme (default via CSS matrix filter), Standard OSM, Esri Street Map, and Esri Satellite.
  - Added cache-busting query strings (`?v=4.2`) to `index.html`.
  - Added `Cache-Control: no-cache, no-store, must-revalidate` middleware in `main.py`.
- **Files Modified:**
  - `backend/app/static/app.js`
  - `backend/app/static/index.html`
  - `backend/app/main.py`

---

## 3. Test Suite Verification Summary

### Comprehensive Test Suite Results:
- **Total Test Files:** 9
- **Total Tests Executed:** 78
- **Tests Passed:** 78 (100%)
- **Tests Failed:** 0
- **Execution Time:** ~29.8s

| Test Suite | Tests | Result | Focus Area |
|---|---|---|---|
| `test_phase4_1_conversational_intelligence.py` | 18 | PASS | Bugs 1–6 (Ablative 'पासून', inverted English, hospital count, multi-turn, KBT Circle, Safari STT) |
| `test_phase4_intelligence.py` | 17 | PASS | Route Engine, Timetable Engine, Context Engine, Hospital & Landmark POIs |
| `test_location_intelligence.py` | 10 | PASS | Location APIs, Deduplication, Trajectory, Up/Down Platforms |
| `test_language_detector.py` | 10 | PASS | Multilingual script detection, Devanagari Marathi/Hindi, Roman Marathi, Hinglish |
| `test_voice_agent.py` | 10 | PASS | Voice pipeline, REST endpoints, WebSocket interactions, Barge-in |
| `test_complete_knowledge_network.py` | 7 | PASS | 315 routes, 1,187 stations, 3,547 timetable trips |
| `test_stt_mime_normalization.py` | 3 | PASS | WebM/Opus, WAV, MP3 multipart upload handling |
| `test_bargein_sync.py` | 2 | PASS | Acoustic barge-in synchronization and frame buffering |
| `test_live_browser_bargein.py` | 1 | PASS | End-to-end full duplex interruption emulation |

---

## 4. Verification Checkpoint

All files have been verified, regression tested, and are running in production on `http://localhost:8000`.

