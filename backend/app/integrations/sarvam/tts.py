"""
Sarvam Text-to-Speech (TTS) Client.
Supports Bulbul v3 model for Marathi (mr-IN), Hindi (hi-IN), and English (en-IN).
"""
import base64
import io
import math
import struct
import wave
import logging
from typing import Dict, Any, Optional
import httpx
from backend.app.core.config import settings
from backend.app.core.exceptions import SarvamAPIError

logger = logging.getLogger("citylinc.sarvam.tts")

def generate_chime_wav(duration_sec: float = 0.5, freq: float = 440.0) -> bytes:
    """Generates a soft, pleasant chime tone as fallback playable audio."""
    sample_rate = 16000
    n_samples = int(sample_rate * duration_sec)
    buffer = io.BytesIO()
    
    with wave.open(buffer, 'wb') as wav_file:
        wav_file.setnchannels(1)  # Mono
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        
        frames = bytearray()
        for i in range(n_samples):
            # Apply slight envelope decay
            decay = max(0.0, 1.0 - (i / n_samples))
            val = int(decay * 16000 * math.sin(2 * math.pi * freq * (i / sample_rate)))
            frames.extend(struct.pack('<h', val))
            
        wav_file.writeframes(frames)
        
    return buffer.getvalue()


class SarvamTTSClient:
    """Client for Sarvam Text-to-Speech API (Bulbul v3)."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or settings.SARVAM_API_KEY
        self.base_url = (base_url or settings.SARVAM_BASE_URL).rstrip("/")
        self.endpoint = f"{self.base_url}/text-to-speech"
        self.model = settings.SARVAM_TTS_MODEL

    async def synthesize(
        self,
        text: str,
        language_code: str = "mr-IN",
        speaker: Optional[str] = None,
        pace: float = 1.0,
        model: Optional[str] = None,
        output_audio_codec: str = "mp3"
    ) -> Dict[str, Any]:
        """
        Synthesizes speech from text using Sarvam Bulbul v3.
        Returns dict with 'audio_base64', 'format' ('audio/mp3' or 'audio/wav'), and 'language_code'.
        """
        clean_text = text.strip()
        if not clean_text:
            return {"audio_base64": "", "format": "audio/mp3", "error": "Empty text"}

        # Limit text length to Bulbul constraint
        if len(clean_text) > 2500:
            clean_text = clean_text[:2500]

        # Safeguard: Verify text language to ensure Marathi text is spoken in mr-IN, Hindi in hi-IN, English in en-IN
        from backend.app.services.language_detector import language_detector
        text_lang_check = language_detector.detect(clean_text, context_language=language_code)
        if text_lang_check.confidence >= 0.85 and text_lang_check.language != language_code:
            logger.warning(
                f"TTS language mismatch aligned: requested '{language_code}' but text is "
                f"'{text_lang_check.language}' ({text_lang_check.reason}). Using '{text_lang_check.language}' for synthesis."
            )
            language_code = text_lang_check.language

        target_model = model or self.model
        target_speaker = speaker or settings.SPEAKER_MAP.get(language_code, settings.SARVAM_TTS_SPEAKER)

        # Fallback to simulation tone if API key is not configured
        if not self.api_key or len(self.api_key) < 5:
            logger.warning("SARVAM_API_KEY is not configured in backend/.env. Returning simulation audio tone.")
            chime_bytes = generate_chime_wav(0.6, 523.25) # Note C5 chime
            b64_audio = base64.b64encode(chime_bytes).decode('ascii')
            return {
                "audio_base64": b64_audio,
                "format": "audio/wav",
                "language_code": language_code,
                "is_simulation": True,
                "note": "Configure SARVAM_API_KEY in backend/.env for live cloud voice synthesis."
            }

        headers = {
            "api-subscription-key": self.api_key,
            "Content-Type": "application/json"
        }

        payload = {
            "text": clean_text,
            "model": target_model,
            "language_code": language_code,
            "speaker": target_speaker,
            "pace": pace,
            "speech_sample_rate": 16000 if output_audio_codec == "wav" else 24000,
            "output_audio_codec": output_audio_codec
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.endpoint,
                    headers=headers,
                    json=payload
                )

                if response.status_code != 200:
                    err_msg = f"Sarvam TTS failed with status {response.status_code}: {response.text}"
                    logger.error(err_msg)
                    raise SarvamAPIError(err_msg, details={"status": response.status_code, "response": response.text})

                res_json = response.json()
                audios = res_json.get("audios", [])
                if not audios or not audios[0]:
                    raise SarvamAPIError("Sarvam TTS returned empty audio list")

                audio_b64 = audios[0]
                logger.info(f"Synthesized TTS audio: {len(audio_b64)} b64 chars for text '{clean_text[:40]}...'")
                
                return {
                    "audio_base64": audio_b64,
                    "format": f"audio/{output_audio_codec}",
                    "language_code": language_code,
                    "request_id": res_json.get("request_id")
                }

        except httpx.RequestError as e:
            logger.error(f"Network error calling Sarvam TTS: {e}")
            raise SarvamAPIError(f"Network connection error to Sarvam TTS: {e}")

# Global singleton
tts_client = SarvamTTSClient()

