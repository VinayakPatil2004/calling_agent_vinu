"""
Tests for CityLinc Voice Agent Final Small Improvements:
1. Graceful 3-Minute Call Ending (warning before limit, polite ending, hard 180s enforcement)
2. Concise and Relevant Responses (direct, essential info only, no lengthy intros or filler)
3. Repeat Previous Answer (Marathi, Hindi, English repetition without reparsing or slot mutation)
"""
import pytest
import asyncio
from unittest.mock import AsyncMock

from backend.app.services.conversation_context import context_engine
from backend.app.services.call_restriction_service import CallRestrictionService


class TestGracefulCallEnding:
    """Improvement 1: Graceful 3-minute call ending with pre-timeout notification."""

    @pytest.mark.asyncio
    async def test_graceful_warning_and_timeout_runner(self):
        """Verify on_warning_callback is called before timeout, followed by on_timeout_callback."""
        service = CallRestrictionService()
        call_id = "test_graceful_call_001"

        warning_fired = asyncio.Event()
        timeout_fired = asyncio.Event()

        async def _on_warning(cid: str):
            assert cid == call_id
            warning_fired.set()

        async def _on_timeout(cid: str):
            assert cid == call_id
            timeout_fired.set()

        # Schedule a fast 2-second timeout with 1-second warning lead
        service.schedule_call_timeout(
            provider_call_id=call_id,
            timeout_seconds=2,
            warning_lead_seconds=1,
            on_warning_callback=_on_warning,
            on_timeout_callback=_on_timeout
        )

        assert call_id in service._active_timeouts

        # Wait for warning to fire first (at ~1s)
        await asyncio.wait_for(warning_fired.wait(), timeout=1.8)
        assert warning_fired.is_set()
        assert not timeout_fired.is_set()

        # Wait for timeout to fire (at ~2s)
        await asyncio.wait_for(timeout_fired.wait(), timeout=1.8)
        assert timeout_fired.is_set()

    @pytest.mark.asyncio
    async def test_cancel_call_timeout(self):
        """Verify cancelling the call cancels both warning and timeout callbacks cleanly."""
        service = CallRestrictionService()
        call_id = "test_graceful_cancel_002"

        warning_called = False
        timeout_called = False

        async def _on_warning(cid: str):
            nonlocal warning_called
            warning_called = True

        async def _on_timeout(cid: str):
            nonlocal timeout_called
            timeout_called = True

        service.schedule_call_timeout(
            provider_call_id=call_id,
            timeout_seconds=2,
            warning_lead_seconds=1,
            on_warning_callback=_on_warning,
            on_timeout_callback=_on_timeout
        )

        # Cancel immediately
        cancelled = service.cancel_call_timeout(call_id)
        assert cancelled is True
        assert call_id not in service._active_timeouts

        # Wait to confirm neither fired
        await asyncio.sleep(1.5)
        assert not warning_called
        assert not timeout_called


class TestConciseAndRelevantResponses:
    """Improvement 2: Concise, natural responses without redundant details or filler."""

    def test_direct_route_query_is_concise_and_relevant(self):
        """Journey query gives essential route, stops, fare, and transfer without 60-char route names or filler notes."""
        sid = "concise_route_mr"
        context_engine.clear_context(sid)

        res = context_engine.process_turn(sid, "मला सीबीएसवरून पंचवटीला जायचे आहे", language="mr-IN")
        text = res["response_text"]

        # Essential info present
        assert "थेट बस उपलब्ध आहे" in text
        assert "मार्ग" in text
        assert "सीबीएस" in text or "CBS" in text or "Central Bus Station" in text
        assert "पंचवटी" in text or "Panchavati" in text
        assert "थांबे" in text
        assert "किमी" in text
        assert "भाडे" in text or "दर" in text

        # Redundant bracketed route names like '(Central Bus Station (CBS) To Panchavati Karanja Via Ravivar Karanja)' removed
        assert "Central Bus Station (CBS) To Panchavati" not in text
        # Redundant leg route names removed
        assert "(" not in text or "Central Bus Station (CBS)" not in text

    def test_hindi_direct_route_is_concise(self):
        """Hindi journey response is natural and concise."""
        sid = "concise_route_hi"
        context_engine.clear_context(sid)

        res = context_engine.process_turn(sid, "मुझे सीबीएस से पंचवटी जाना है", language="hi-IN")
        text = res["response_text"]

        assert "सीधी बस उपलब्ध है" in text
        assert "रूट" in text
        assert "स्टॉप" in text
        assert "किमी" in text
        assert "Central Bus Station" not in text

    def test_greeting_is_concise_and_natural(self):
        """Greetings are short and natural without lengthy intros."""
        sid = "concise_greeting"
        context_engine.clear_context(sid)

        res_mr = context_engine.process_turn(sid, "नमस्कार", language="mr-IN")
        assert len(res_mr["response_text"]) < 80
        assert "नमस्कार" in res_mr["response_text"]
        assert "मी आपली काय मदत करू शकतो?" in res_mr["response_text"]

        res_en = context_engine.process_turn(sid, "Hello", language="en-IN")
        assert len(res_en["response_text"]) < 80
        assert "Hello" in res_en["response_text"]

    def test_stop_confirmation_is_crisp(self):
        """Route confirmation answers directly and simply."""
        sid = "concise_conf"
        context_engine.clear_context(sid)

        # First establish route context
        context_engine.process_turn(sid, "मला सीबीएसवरून पंचवटीला जायचे आहे", language="mr-IN")

        # Now ask a confirmation question
        res = context_engine.process_turn(sid, "ही बस रविवार कारंजा वरून जाते का?", language="mr-IN")
        assert res["intent"] == "STOP_LOOKUP"
        assert res["response_text"].startswith("होय") or res["response_text"].startswith("नाही")
        assert len(res["response_text"].split("\n")) == 1  # Single concise sentence


class TestRepeatPreviousAnswer:
    """Improvement 3: Repeat previous answer across Marathi, Hindi, English."""

    def test_marathi_repeat_patterns(self):
        """Verify Marathi repeat variations replay previous response verbatim."""
        patterns = [
            "परत सांग",
            "पुन्हा सांग",
            "एकदा रिपीट कर",
            "परत सांग मला",
            "मला पुन्हा सांग",
            "पुन्हा एकदा सांग",
            "एकदा रिपीट करा"
        ]
        for idx, phrase in enumerate(patterns):
            sid = f"repeat_mr_{idx}"
            context_engine.clear_context(sid)

            # Turn 1: Initial query
            t1 = context_engine.process_turn(sid, "मला सीबीएसवरून पंचवटीला जायचे आहे", language="mr-IN", turn_id=1)
            orig_answer = t1["response_text"]

            # Turn 2: Repeat request
            t2 = context_engine.process_turn(sid, phrase, language="mr-IN", turn_id=2)
            assert t2["intent"] == "REPEAT_QUERY", f"Failed for phrase: '{phrase}'"
            assert t2["response_text"] == orig_answer

    def test_hindi_repeat_patterns(self):
        """Verify Hindi repeat variations replay previous response verbatim."""
        patterns = [
            "फिर से बताइए",
            "दोबारा बोलिए",
            "एक बार फिर से बताइए",
            "दोबारा बताइए"
        ]
        for idx, phrase in enumerate(patterns):
            sid = f"repeat_hi_{idx}"
            context_engine.clear_context(sid)

            # Turn 1: Switch to Hindi
            context_engine.process_turn(sid, "मुझसे हिंदी में बात करो", language="hi-IN", turn_id=1)

            # Turn 2: Route query
            t2 = context_engine.process_turn(sid, "मुझे सीबीएस से पंचवटी जाना है", language="hi-IN", turn_id=2)
            route_answer = t2["response_text"]

            # Turn 3: Repeat request
            t3 = context_engine.process_turn(sid, phrase, language="hi-IN", turn_id=3)
            assert t3["intent"] == "REPEAT_QUERY", f"Failed for phrase: '{phrase}'"
            assert t3["response_text"] == route_answer

    def test_english_repeat_patterns(self):
        """Verify English repeat variations replay previous response verbatim."""
        patterns = [
            "Repeat that",
            "Can you say that again?",
            "Can you please repeat again?",
            "Could you please repeat that?",
            "Say that again",
            "Repeat please"
        ]
        for idx, phrase in enumerate(patterns):
            sid = f"repeat_en_{idx}"
            context_engine.clear_context(sid)

            # Turn 1: Greeting
            t1 = context_engine.process_turn(sid, "Hello", language="en-IN", turn_id=1)
            greet_answer = t1["response_text"]

            # Turn 2: Repeat request
            t2 = context_engine.process_turn(sid, phrase, language="en-IN", turn_id=2)
            assert t2["intent"] == "REPEAT_QUERY", f"Failed for phrase: '{phrase}'"
            assert t2["response_text"] == greet_answer

    def test_repeat_after_fare_query(self):
        """Verify repeat query works properly after a fare query."""
        sid = "repeat_fare"
        context_engine.clear_context(sid)

        # Turn 1: Fare query
        t1 = context_engine.process_turn(sid, "सीबीएस ते नाशिक रोड भाडे किती आहे?", language="mr-IN", turn_id=1)
        assert t1["intent"] == "FARE_QUERY"
        fare_text = t1["response_text"]

        # Turn 2: Repeat request
        t2 = context_engine.process_turn(sid, "परत सांग", language="mr-IN", turn_id=2)
        assert t2["intent"] == "REPEAT_QUERY"
        assert t2["response_text"] == fare_text

    def test_repeat_does_not_mutate_origin_destination_slots(self):
        """Verify repeating does NOT get parsed as a new journey or change session slots."""
        sid = "repeat_slots_check"
        context_engine.clear_context(sid)

        # Establish route
        context_engine.process_turn(sid, "मला सीबीएसवरून पंचवटीला जायचे आहे", language="mr-IN", turn_id=1)
        ctx = context_engine.get_context(sid)
        assert ctx.origin == "सीबीएस"
        assert ctx.destination == "पंचवटी"

        # Ask repeat
        t2 = context_engine.process_turn(sid, "परत सांग मला", language="mr-IN", turn_id=2)
        assert t2["intent"] == "REPEAT_QUERY"

        # Check context slots are completely preserved
        ctx_after = context_engine.get_context(sid)
        assert ctx_after.origin == "सीबीएस"
        assert ctx_after.destination == "पंचवटी"
        assert ctx_after.selected_rno is not None

    def test_repeat_with_no_prior_response(self):
        """Verify graceful fallback if user immediately asks to repeat with no prior turns."""
        sid = "repeat_empty"
        context_engine.clear_context(sid)

        res = context_engine.process_turn(sid, "परत सांग", language="mr-IN")
        assert res["intent"] == "REPEAT_QUERY"
        assert "माफ करा" in res["response_text"] or "उपलब्ध नाही" in res["response_text"]
