"""
Sarvam Speech-to-Text (STT) Client.
Supports Saaras v4 model with multilingual auto-detection (mr-IN, hi-IN, en-IN).
"""
import io
import logging
from typing import Dict, Any, Optional
import httpx
from backend.app.core.config import settings
from backend.app.core.exceptions import SarvamAPIError

logger = logging.getLogger("citylinc.sarvam.stt")

class SarvamSTTClient:
    """Client for Sarvam Speech-to-Text API (Saaras v4 / v1)."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = settings.SARVAM_API_KEY if api_key is None else api_key
        self.base_url = (base_url or settings.SARVAM_BASE_URL).rstrip("/")
        self.endpoint = f"{self.base_url}/speech-to-text"
        self.model = settings.SARVAM_STT_MODEL

    async def transcribe(
        self,
        audio_bytes: bytes,
        language_code: str = "unknown",
        model: Optional[str] = None,
        filename: str = "audio.wav",
        mime_type: str = "audio/wav"
    ) -> Dict[str, Any]:
        """
        Sends audio bytes to Sarvam Speech-to-Text.
        Returns dictionary with 'transcript' and 'language_code'.
        """
        if not audio_bytes or len(audio_bytes) < 150:
            logger.warning(f"Audio payload too short ({len(audio_bytes) if audio_bytes else 0} bytes). Returning localized retry prompt.")
            return {
                "transcript": "",
                "language_code": language_code,
                "error": "audio_too_short",
                "user_message_mr": "तुमचा आवाज स्पष्ट ऐकू आला नाही. कृपया पुन्हा बोला.",
                "user_message_hi": "आपकी आवाज़ साफ़ सुनाई नहीं दी। कृपया दोबारा बोलें।",
                "user_message_en": "I couldn't hear you clearly. Please try again."
            }

        target_model = model or self.model

        # If no API key configured, provide structured simulation / test response
        if not self.api_key or len(self.api_key) < 5:
            logger.warning("SARVAM_API_KEY is not configured in backend/.env. Returning simulation/test response.")
            return {
                "transcript": "नमस्कार, सिटीलिंक नाशिक बस सेवेबद्दल माहिती हवी आहे",
                "language_code": "mr-IN",
                "is_simulation": True,
                "note": "Configure SARVAM_API_KEY in backend/.env for live cloud transcription."
            }

        headers = {
            "api-subscription-key": self.api_key
        }

        # 1. Container Signature Inspection for foolproof audio container detection
        detected_mime = None
        detected_filename = None
        if len(audio_bytes) >= 4:
            if audio_bytes[:4] == b"\x1a\x45\xdf\xa3":
                detected_mime = "audio/webm"
                detected_filename = "recording.webm"
            elif audio_bytes[:4] == b"RIFF" and len(audio_bytes) >= 12 and audio_bytes[8:12] == b"WAVE":
                detected_mime = "audio/wav"
                detected_filename = "recording.wav"
            elif audio_bytes[:4] == b"OggS":
                detected_mime = "audio/ogg"
                detected_filename = "recording.ogg"
            elif audio_bytes[:3] == b"ID3" or (len(audio_bytes) >= 2 and audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
                detected_mime = "audio/mpeg"
                detected_filename = "recording.mp3"
            elif len(audio_bytes) >= 8 and audio_bytes[4:8] == b"ftyp":
                detected_mime = "audio/mp4"
                detected_filename = "recording.mp4"

        # 2. Normalize multipart upload Content-Type and filename
        clean_mime = (mime_type or "").split(";")[0].strip().lower()
        if detected_mime:
            actual_filename = detected_filename
            clean_mime = detected_mime
        elif "webm" in clean_mime or "opus" in clean_mime:
            actual_filename = "recording.webm"
            clean_mime = "audio/webm"
        elif "mp4" in clean_mime or "m4a" in clean_mime or "aac" in clean_mime:
            actual_filename = "recording.mp4"
            clean_mime = "audio/mp4"
        elif "mp3" in clean_mime or "mpeg" in clean_mime:
            actual_filename = "recording.mp3"
            clean_mime = "audio/mpeg"
        elif "ogg" in clean_mime:
            actual_filename = "recording.ogg"
            clean_mime = "audio/ogg"
        elif "wav" in clean_mime:
            actual_filename = "recording.wav"
            clean_mime = "audio/wav"
        else:
            actual_filename = "recording.webm" if "webm" in filename else "recording.wav"
            clean_mime = "audio/webm" if "webm" in filename else "audio/wav"

        # Diagnostic log showing filename, MIME type and byte size before the Sarvam request
        logger.info(
            f"Preparing Sarvam STT request: filename='{actual_filename}', "
            f"content_type='{clean_mime}', byte_size={len(audio_bytes)} bytes, model='{target_model}'"
        )

        files = {
            "file": (actual_filename, audio_bytes, clean_mime)
        }
        data = {
            "model": target_model,
            "mode": "transcribe"
        }
        
        # If user explicitly selected a language or "unknown" for auto-detection
        if language_code and language_code != "auto":
            data["language_code"] = language_code
        else:
            data["language_code"] = "unknown"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.endpoint,
                    headers=headers,
                    files=files,
                    data=data
                )
                
                # Gracefully handle Sarvam 400 Bad Request ("Failed to read the file, please check the audio format.")
                # Do NOT retry the same unreadable file on 400. Return friendly localized prompt.
                if response.status_code == 400:
                    err_text = response.text
                    logger.warning(f"Sarvam STT returned 400 Bad Request for {actual_filename} ({len(audio_bytes)} bytes): {err_text}")
                    return {
                        "transcript": "",
                        "language_code": language_code,
                        "error": "unreadable_audio",
                        "sarvam_error": err_text,
                        "user_message_mr": "तुमचा आवाज स्पष्ट ऐकू आला नाही. कृपया पुन्हा बोला.",
                        "user_message_hi": "आपकी आवाज़ साफ़ सुनाई नहीं दी। कृपया दोबारा बोलें।",
                        "user_message_en": "I couldn't hear you clearly. Please try again."
                    }

                if response.status_code != 200:
                    err_msg = f"Sarvam STT failed with status {response.status_code}: {response.text}"
                    logger.error(err_msg)
                    return {
                        "transcript": "",
                        "language_code": language_code,
                        "error": "api_error",
                        "status_code": response.status_code,
                        "sarvam_error": response.text
                    }

                res_json = response.json()
                transcript = res_json.get("transcript", "").strip()
                detected_lang = res_json.get("language_code", language_code)
                
                transcript_len = len(transcript)
                word_count = len(transcript.split())
                logger.info(f"STT Transcript: Length: {transcript_len} chars, Words: {word_count} [Lang: {detected_lang}]")
                return {
                    "transcript": transcript,
                    "language_code": detected_lang,
                    "raw": res_json
                }

        except httpx.TimeoutException as e:
            logger.error(f"Timeout calling Sarvam STT: {e}")
            return {
                "transcript": "",
                "language_code": language_code,
                "error": "timeout",
                "details": str(e)
            }
        except httpx.RequestError as e:
            logger.error(f"Network error calling Sarvam STT: {e}")
            return {
                "transcript": "",
                "language_code": language_code,
                "error": "api_error",
                "details": str(e)
            }

# Global singleton
stt_client = SarvamSTTClient()

