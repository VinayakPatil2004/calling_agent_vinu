"""
Unit tests for the Multilingual Language Detection Service.
"""
import pytest
from backend.app.services.language_detector import language_detector

def test_language_detection_devanagari_marathi():
    res = language_detector.detect("नाशिक रोड ते त्र्यंबकेश्वर बस कधी आहे?", context_language="en-IN")
    assert res.language == "mr-IN"
    assert res.confidence >= 0.85
    assert res.script == "devanagari"

def test_language_detection_devanagari_hindi():
    res = language_detector.detect("नाशिक रोड से त्र्यंबकेश्वर बस कब मिलेगी?", context_language="en-IN")
    assert res.language == "hi-IN"
    assert res.confidence >= 0.85
    assert res.script == "devanagari"

def test_language_detection_english():
    res = language_detector.detect("When is the next bus from Nashik Road to Trimbakeshwar?", context_language="mr-IN")
    assert res.language == "en-IN"
    assert res.confidence >= 0.85
    assert res.script == "latin"

def test_language_detection_roman_marathi():
    res = language_detector.detect("Nashik road varun Trimbakeshwar sathi bus kiti vajata ahe?", context_language="en-IN")
    assert res.language == "mr-IN"
    assert res.confidence >= 0.80
    assert res.is_transliterated is True

def test_language_detection_roman_marathi_short():
    res = language_detector.detect("student pass chi mahiti dya", context_language="en-IN")
    assert res.language == "mr-IN"
    assert res.confidence >= 0.80

def test_language_detection_hinglish():
    res = language_detector.detect("Bhai Nashik Road se Trimbakeshwar bus timing kya hai?", context_language="en-IN")
    assert res.language == "hi-IN"
    assert res.confidence >= 0.80
    assert res.is_transliterated is True

def test_language_detection_hinglish_short():
    res = language_detector.detect("student pass kitne ka hai batao", context_language="en-IN")
    assert res.language == "hi-IN"
    assert res.confidence >= 0.80

def test_language_detection_code_switching():
    # Marathi with English loanwords
    res_mr = language_detector.detect("मला स्टुडंट पास आणि बस टाइमिंग सांगा", context_language="en-IN")
    assert res_mr.language == "mr-IN"

    # Hindi with English loanwords
    res_hi = language_detector.detect("मुझे स्टूडेंट पास और बस टाइमिंग बताओ", context_language="en-IN")
    assert res_hi.language == "hi-IN"

def test_language_continuity_numeric():
    # Preserves Hindi context
    res1 = language_detector.detect("101", context_language="hi-IN")
    assert res1.language == "hi-IN"

    # Preserves Marathi context
    res2 = language_detector.detect("104", context_language="mr-IN")
    assert res2.language == "mr-IN"

def test_stt_acoustic_fallback():
    # When text is neutral (like just proper noun), STT acoustic signal breaks tie
    res_mr = language_detector.detect("त्र्यंबकेश्वर", stt_language="mr-IN", context_language="en-IN")
    assert res_mr.language == "mr-IN"

    res_hi = language_detector.detect("त्र्यंबकेश्वर", stt_language="hi-IN", context_language="en-IN")
    assert res_hi.language == "hi-IN"

