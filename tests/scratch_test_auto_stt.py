import sys
import asyncio
import base64

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from backend.app.integrations.sarvam.tts import tts_client
from backend.app.integrations.sarvam.stt import stt_client

async def test_mixed_stt():
    test_cases = [
        # Hinglish spoken
        ("hi-IN", "Bhai Nashik Road se Trimbakeshwar bus timing kya hai?"),
        # Marathi spoken in conversational style
        ("mr-IN", "Nashik road varun Trimbakeshwar sathi bus kiti vajata ahe?"),
        # Mixed English/Hindi
        ("en-IN", "Can you tell me student pass price for Nashik city bus?"),
        # Mixed Marathi with English words
        ("mr-IN", "मला स्टुडंट पास आणि बस टाईमिंग ची माहिती हवी आहे.")
    ]
    for orig_lang, text in test_cases:
        tts = await tts_client.synthesize(text, language_code=orig_lang)
        audio = base64.b64decode(tts["audio_base64"])
        print(f"\n--- Spoken input ({orig_lang}): '{text}' ---")
        
        res = await stt_client.transcribe(audio, language_code="unknown", mime_type="audio/mpeg")
        print(f"STT Transcript: '{res.get('transcript')}' | Sarvam Lang: {res.get('language_code')}")

if __name__ == "__main__":
    asyncio.run(test_mixed_stt())
