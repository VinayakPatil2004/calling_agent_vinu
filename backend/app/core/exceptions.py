"""
Custom exceptions for CityLinc AI Voice Agent.
"""

class CityLincException(Exception):
    """Base exception for all CityLinc application errors."""
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SarvamAPIError(CityLincException):
    """Raised when an API call to Sarvam AI (STT/TTS/LLM) fails."""
    pass


class AudioProcessingError(CityLincException):
    """Raised when audio conversion, decoding, or encoding fails."""
    pass


class LLMError(CityLincException):
    """Raised when LLM text generation or parsing fails."""
    pass


class SessionError(CityLincException):
    """Raised when conversation session management fails."""
    pass

