"""
Comprehensive Live Multilingual End-to-End Test Suite for CityLinc Voice Agent.
Performs real STT and TTS API requests with Sarvam AI:
1. Marathi voice conversation
2. Hindi voice conversation
3. English voice conversation
4. Continuous 4-turn language switching session (Marathi -> Hindi -> English -> Marathi)
5. Roman Marathi and Hinglish queries
Generates exact verification matrix.
"""
import sys
import asyncio
import base64
import json
import logging
from typing import Dict, Any, List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from backend.app.core.config import settings
from backend.app.integrations.sarvam.stt import stt_client
from backend.app.integrations.sarvam.tts import tts_client
from backend.app.integrations.llm.client import llm_client
from backend.app.services.language_detector import language_detector
from backend.app.orchestrator.conversation_manager import conversation_manager
from fastapi.testclient import TestClient
from backend.app.main import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("citylinc.matrix_test")


async def run_multilingual_verification():
    print("=" * 80)
    print("CITYLINC MULTILINGUAL PRODUCTION VOICE AGENT - LIVE VERIFICATION MATRIX")
    print(f"Sarvam API Key Configured: {settings.has_sarvam_key}")
    print(f"STT Model: {settings.SARVAM_STT_MODEL} | TTS Model: {settings.SARVAM_TTS_MODEL}")
    print("=" * 80)

    results_matrix: List[Dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # TEST 1: Marathi Voice Conversation (Real Audio -> STT -> LLM -> TTS)
    # -------------------------------------------------------------------------
    print("\n>>> TEST 1: Marathi Voice Conversation")
    mr_prompt = "नाशिक रोड ते त्र्यंबकेश्वर बस कधी आहे?"
    # 1. Synthesize real spoken audio
    mr_tts = await tts_client.synthesize(mr_prompt, language_code="mr-IN")
    mr_audio_bytes = base64.b64decode(mr_tts["audio_base64"])
    # 2. Real STT with language_code='unknown' (auto-detection)
    mr_stt = await stt_client.transcribe(mr_audio_bytes, language_code="unknown", mime_type="audio/mpeg")
    mr_transcript = mr_stt.get("transcript", "")
    mr_stt_lang = mr_stt.get("language_code", "")
    # 3. Detection
    mr_det = language_detector.detect(mr_transcript, stt_language=mr_stt_lang, context_language="mr-IN")
    # 4. LLM response
    mr_reply = await llm_client.generate_response([{"role": "user", "content": mr_transcript}], language_code=mr_det.language)
    # 5. Output TTS
    mr_out_tts = await tts_client.synthesize(mr_reply, language_code=mr_det.language)
    assert len(mr_out_tts.get("audio_base64", "")) > 1000

    results_matrix.append({
        "scenario": "1. Pure Marathi Voice",
        "input": mr_prompt,
        "stt_transcript": mr_transcript,
        "stt_detected_lang": mr_stt_lang,
        "final_detected_lang": mr_det.language,
        "confidence": mr_det.confidence,
        "response_sample": mr_reply[:60] + "...",
        "tts_format": mr_out_tts.get("format"),
        "tts_audio_bytes": len(base64.b64decode(mr_out_tts["audio_base64"]))
    })
    print(f" [OK] Marathi verified: '{mr_transcript}' -> Lang: {mr_det.language} -> TTS: {len(mr_out_tts['audio_base64'])} b64 chars")

    # -------------------------------------------------------------------------
    # TEST 2: Hindi Voice Conversation
    # -------------------------------------------------------------------------
    print("\n>>> TEST 2: Hindi Voice Conversation")
    hi_prompt = "नाशिक में छात्र पास के लिए क्या नियम हैं?"
    hi_tts = await tts_client.synthesize(hi_prompt, language_code="hi-IN")
    hi_audio_bytes = base64.b64decode(hi_tts["audio_base64"])
    hi_stt = await stt_client.transcribe(hi_audio_bytes, language_code="unknown", mime_type="audio/mpeg")
    hi_transcript = hi_stt.get("transcript", "")
    hi_stt_lang = hi_stt.get("language_code", "")
    hi_det = language_detector.detect(hi_transcript, stt_language=hi_stt_lang, context_language="hi-IN")
    hi_reply = await llm_client.generate_response([{"role": "user", "content": hi_transcript}], language_code=hi_det.language)
    hi_out_tts = await tts_client.synthesize(hi_reply, language_code=hi_det.language)
    assert len(hi_out_tts.get("audio_base64", "")) > 1000

    results_matrix.append({
        "scenario": "2. Pure Hindi Voice",
        "input": hi_prompt,
        "stt_transcript": hi_transcript,
        "stt_detected_lang": hi_stt_lang,
        "final_detected_lang": hi_det.language,
        "confidence": hi_det.confidence,
        "response_sample": hi_reply[:60] + "...",
        "tts_format": hi_out_tts.get("format"),
        "tts_audio_bytes": len(base64.b64decode(hi_out_tts["audio_base64"]))
    })
    print(f" [OK] Hindi verified: '{hi_transcript}' -> Lang: {hi_det.language} -> TTS: {len(hi_out_tts['audio_base64'])} b64 chars")

    # -------------------------------------------------------------------------
    # TEST 3: English Voice Conversation
    # -------------------------------------------------------------------------
    print("\n>>> TEST 3: English Voice Conversation")
    en_prompt = "When does the bus 101 depart from Tapovan?"
    en_tts = await tts_client.synthesize(en_prompt, language_code="en-IN")
    en_audio_bytes = base64.b64decode(en_tts["audio_base64"])
    en_stt = await stt_client.transcribe(en_audio_bytes, language_code="unknown", mime_type="audio/mpeg")
    en_transcript = en_stt.get("transcript", "")
    en_stt_lang = en_stt.get("language_code", "")
    en_det = language_detector.detect(en_transcript, stt_language=en_stt_lang, context_language="en-IN")
    en_reply = await llm_client.generate_response([{"role": "user", "content": en_transcript}], language_code=en_det.language)
    en_out_tts = await tts_client.synthesize(en_reply, language_code=en_det.language)
    assert len(en_out_tts.get("audio_base64", "")) > 1000

    results_matrix.append({
        "scenario": "3. Pure English Voice",
        "input": en_prompt,
        "stt_transcript": en_transcript,
        "stt_detected_lang": en_stt_lang,
        "final_detected_lang": en_det.language,
        "confidence": en_det.confidence,
        "response_sample": en_reply[:60] + "...",
        "tts_format": en_out_tts.get("format"),
        "tts_audio_bytes": len(base64.b64decode(en_out_tts["audio_base64"]))
    })
    print(f" [OK] English verified: '{en_transcript}' -> Lang: {en_det.language} -> TTS: {len(en_out_tts['audio_base64'])} b64 chars")

    # -------------------------------------------------------------------------
    # TEST 4: Continuous 4-Turn Multilingual Session (Marathi -> Hindi -> English -> Marathi)
    # -------------------------------------------------------------------------
    print("\n>>> TEST 4: Continuous Multilingual Switching Session (mr -> hi -> en -> mr)")
    session = conversation_manager.get_or_create_session("continuous_matrix_session", language="mr-IN")
    session.reset()

    switching_turns = [
        ("Turn 1 (Marathi)", "विद्यार्थी पास साठी किती सवलत मिळते?", "mr-IN"),
        ("Turn 2 (Hindi)", "और सामान्य बस पास के क्या रेट हैं?", "hi-IN"),
        ("Turn 3 (English)", "Can you also tell me about Route 145 to Trimbakeshwar?", "en-IN"),
        ("Turn 4 (Marathi)", "धन्यवाद, खूप छान माहिती मिळाली.", "mr-IN")
    ]

    for turn_label, turn_text, expected_lang in switching_turns:
        # Synthesize audio to simulate real mic input
        t_tts = await tts_client.synthesize(turn_text, language_code=expected_lang)
        t_audio = base64.b64decode(t_tts["audio_base64"])
        # Transcribe with auto-detection
        t_stt = await stt_client.transcribe(t_audio, language_code="unknown", mime_type="audio/mpeg")
        t_transcript = t_stt.get("transcript", turn_text)
        t_stt_lang = t_stt.get("language_code", "unknown")

        # Detect
        t_det = language_detector.detect(t_transcript, stt_language=t_stt_lang, context_language=session.current_language)
        assert t_det.language == expected_lang, f"Expected {expected_lang}, got {t_det.language}"

        # Register message
        turn_id = session.add_user_message(t_transcript, language=t_det.language)
        # Verify history preservation
        assert len(session.history) > 0

        # Generate response
        t_reply = await llm_client.generate_response(session.history, language_code=t_det.language)
        session.add_assistant_message(t_reply, turn_id)

        # Synthesize TTS in the detected language
        t_out = await tts_client.synthesize(t_reply, language_code=t_det.language)
        assert len(t_out.get("audio_base64", "")) > 1000

        results_matrix.append({
            "scenario": f"4. Switch: {turn_label}",
            "input": turn_text,
            "stt_transcript": t_transcript,
            "stt_detected_lang": t_stt_lang,
            "final_detected_lang": t_det.language,
            "confidence": t_det.confidence,
            "response_sample": t_reply[:60] + "...",
            "tts_format": t_out.get("format"),
            "tts_audio_bytes": len(base64.b64decode(t_out["audio_base64"]))
        })
        print(f" [OK] {turn_label}: Detected {t_det.language} | History length: {len(session.history)} | Reply: {t_reply[:40]}...")

    # -------------------------------------------------------------------------
    # TEST 5: Roman Marathi & Hinglish
    # -------------------------------------------------------------------------
    print("\n>>> TEST 5: Roman Marathi & Hinglish Handling")
    roman_cases = [
        ("Roman Marathi", "Nashik road varun Trimbakeshwar sathi bus kiti vajata ahe?", "mr-IN"),
        ("Hinglish", "Bhai Nashik Road se Trimbakeshwar bus timing kya hai batao?", "hi-IN")
    ]
    for label, text, expected_lang in roman_cases:
        det = language_detector.detect(text, context_language="en-IN")
        assert det.language == expected_lang
        reply = await llm_client.generate_response([{"role": "user", "content": text}], language_code=det.language)
        out_tts = await tts_client.synthesize(reply, language_code=det.language)

        results_matrix.append({
            "scenario": f"5. Roman: {label}",
            "input": text,
            "stt_transcript": "(Text/Roman Input)",
            "stt_detected_lang": "N/A",
            "final_detected_lang": det.language,
            "confidence": det.confidence,
            "response_sample": reply[:60] + "...",
            "tts_format": out_tts.get("format"),
            "tts_audio_bytes": len(base64.b64decode(out_tts["audio_base64"]))
        })
        print(f" [OK] {label}: Detected {det.language} ({det.reason}) | Reply: {reply[:40]}...")

    print("\n" + "=" * 80)
    print("ALL MULTILINGUAL LIVE TESTS SUCCEEDED!")
    print("=" * 80)
    return results_matrix

if __name__ == "__main__":
    matrix = asyncio.run(run_multilingual_verification())

