"""
Security and validation utilities for CityLinc AI Voice Agent.
"""
import hmac
import hashlib
from typing import Optional
from backend.app.core.config import settings

def validate_api_key(provided_key: Optional[str]) -> bool:
    """Validates that a provided API key matches the configured backend key."""
    if not settings.has_sarvam_key or not provided_key:
        return False
    return hmac.compare_digest(provided_key.strip(), settings.SARVAM_API_KEY.strip())


def sanitize_input_text(text: str, max_chars: int = 1000) -> str:
    """Sanitizes user text prompt and truncates to safe length."""
    if not text:
        return ""
    # Strip dangerous control characters and limit length
    clean = "".join(ch for ch in text if ch.isprintable() or ch in ('\n', '\r', '\t'))
    return clean[:max_chars].strip()

