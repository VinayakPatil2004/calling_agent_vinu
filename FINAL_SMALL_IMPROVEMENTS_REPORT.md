# CityLinc Calling Agent — Final Small Voice Agent Improvements Report

**Date**: 2026-11-03  
**Status**: VERIFIED & COMPLETE (All 253 Automated Tests Passing)  
**Workspace**: `D:\Calling Agent New`  

---

## 1. Executive Summary

This engineering cycle implemented three focused, non-architectural voice agent improvements without modifying underlying STT/TTS models, VAD thresholds, audio capture lifecycles, database schemas, or raw datasets:

1. **Graceful 3-Minute Call Ending**:
   - Preserves strict 180-second hard duration enforcement and 3-call/day rate limiting.
   - Dispatches a polite, localized pre-timeout audio/event notification at $t = 160$s (20s before hard limit).
   - Allows an active response at $\ge 155$s to complete naturally with a polite closing suffix before termination.
   - Disconnects gracefully with code 4008 (`max_duration_exceeded`) in the user's active language (`mr-IN`, `hi-IN`, `en-IN`).

2. **Concise and Relevant Responses**:
   - Replaced verbose 65-character repetitive route names (`({top_r.route_name})` and `({leg.route_name})`) with clean, conversational route numbers and stops.
   - Streamlined greetings from lengthy paragraphs into crisp, direct conversational openers.
   - Preserved all essential journey information: boarding stop, alighting stop, distance, stop count, fare, and conductor verification disclaimer.
   - Retained strict backward compatibility with all existing test marker phrases.

3. **Repeat Previous Answer**:
   - Comprehensive regex coverage for Marathi, Hindi, and English repeat phrases.
   - Verbatim playback of the last completed assistant response (`last_response_text`).
   - Isolated from route pathfinding; never mutates origin/destination slots or triggers clarifying re-searches.
   - Fully operational after bus route queries, fare queries, pass inquiries, or conversational turns.

---

## 2. Modified Files & Exact Changes

### A. `backend/app/services/call_restriction_service.py`
- **Pre-Timeout Warning Callback**:
  - Added `_warning_callbacks: Dict[str, Callable[[str], Awaitable[None]]]` to `CallRestrictionService`.
  - Updated `schedule_call_timeout` with `warning_lead_seconds=20`, `on_warning_callback`, and `on_timeout_callback`.
  - Updated `_call_timeout_runner` to sleep `timeout_seconds - warning_lead_seconds` ($160$s), await `on_warning_callback`, and then sleep remaining $20$s before executing hard timeout.
  - Refactored `cancel_call_timeout` to safely cancel the background task while preserving callback associations during reschedules.
  - Cleaned up callbacks in `register_call_disconnect` upon normal call teardown.

### B. `backend/app/api/websocket_routes.py`
- **Watchdog & Graceful Closing**:
  - Recorded session `call_start_time = time.time()` and state flag `warning_sent = False`.
  - Defined `_send_graceful_closing_warning(call_id)` to dispatch WebSocket event `"call_warning"` and synthesized closing audio in `session.current_language`.
  - Registered watchdog in `call_restriction_service.schedule_call_timeout` for direct browser sessions and authenticated telephony sessions.
  - In turn processing loop (for both audio and text events): when `elapsed >= 155`s and warning has not been sent, appends localized polite closing message directly to the final answer so the turn concludes naturally before the 180s hard limit.
  - In `finally` block: ensured `cancel_call_timeout(session.session_id)` is invoked to eliminate orphaned background tasks.

### C. `backend/app/services/conversation_context.py`
- **Expanded `REPEAT_PATTERNS` Regex**:
  - Marathi: `"परत सांग"`, `"पुन्हा सांग"`, `"एकदा रिपीट कर"`, `"परत सांग मला"`, `"मला पुन्हा सांग"`, `"पुन्हा एकदा सांग"`, `"एकदा रिपीट करा"`, `"काय बोललात/म्हणालात"`.
  - Hindi: `"फिर से बताइए"`, `"दोबारा बोलिए"`, `"एक बार फिर से बताइए"`, `"दोबारा बताइए"`, `"क्या आप दोबारा बोल सकते हैं"`.
  - English: `"Repeat that"`, `"Can you say that again?"`, `"Can you please repeat again?"`, `"Could you please repeat that?"`, `"Say that again"`, `"Repeat please"`.
- **Concise Response Templates**:
  - Direct routes: Removed redundant 65-character bracketed route names (`({top_r.route_name})`). Boarding stop, alighting stop, distance, stop count, fare, and conductor verification disclaimer are delivered crisply.
  - Connecting journeys: Removed redundant bracketed route names from individual legs while preserving mandatory chronological connecting headers.
  - Greetings: Streamlined into natural conversational openers across all three languages.
- **Repeat Turn Handling**:
  - Evaluated at Step 2B (before route extraction, greetings, or clarifications).
  - Directly returns `ctx.last_response_text` verbatim without modifying context or slots.

---

## 3. Test Suite Verification

### A. Dedicated Improvements Test Suite (`tests/test_final_small_improvements.py`)
12 tests covering all three focal areas:
- `TestGracefulCallEnding::test_graceful_warning_and_timeout_runner`: **PASSED**
- `TestGracefulCallEnding::test_cancel_call_timeout`: **PASSED**
- `TestConciseAndRelevantResponses::test_direct_route_query_is_concise_and_relevant`: **PASSED**
- `TestConciseAndRelevantResponses::test_hindi_direct_route_is_concise`: **PASSED**
- `TestConciseAndRelevantResponses::test_greeting_is_concise_and_natural`: **PASSED**
- `TestConciseAndRelevantResponses::test_stop_confirmation_is_crisp`: **PASSED**
- `TestRepeatPreviousAnswer::test_marathi_repeat_patterns`: **PASSED**
- `TestRepeatPreviousAnswer::test_hindi_repeat_patterns`: **PASSED**
- `TestRepeatPreviousAnswer::test_english_repeat_patterns`: **PASSED**
- `TestRepeatPreviousAnswer::test_repeat_after_fare_query`: **PASSED**
- `TestRepeatPreviousAnswer::test_repeat_does_not_mutate_origin_destination_slots`: **PASSED**
- `TestRepeatPreviousAnswer::test_repeat_with_no_prior_response`: **PASSED**

### B. Frontend Audio Capture Lifecycle (`tests/test_app_capture_lifecycle.js`)
All 15 deterministic MediaRecorder requirements and 7 regression scenarios: **100% PASSED**.

### C. Full System Regression (`pytest tests/ -q`)
- Total tests executed: **253**
- Passed: **253**
- Failed: **0**
- Execution duration: **164.74s**

---

## 4. Remaining Issues & Risks

- None identified within project scope. All 253 tests pass cleanly.
- Production deployment was intentionally omitted per instructions.

