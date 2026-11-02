"""
Application configuration for CityLinc Multilingual Voice Agent.
Loads configuration from backend/.env and system environment.
"""
import os
from pathlib import Path
from typing import List
from dotenv import load_dotenv

# Base paths
APP_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = APP_DIR.parent
ROOT_DIR = BACKEND_DIR.parent

# Load .env files in priority order: backend/.env, then root .env
backend_env = BACKEND_DIR / ".env"
root_env = ROOT_DIR / ".env"

if backend_env.exists():
    load_dotenv(backend_env, override=True)
if root_env.exists():
    load_dotenv(root_env, override=False)

class Settings:
    """Runtime configuration settings."""
    PROJECT_NAME: str = "CityLinc Multilingual Voice Agent"
    VERSION: str = "1.0.0"
    
    # Network & Server
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
    
    # Sarvam AI Keys & Models
    SARVAM_API_KEY: str = os.getenv("SARVAM_API_KEY", "").strip()
    SARVAM_BASE_URL: str = os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai")
    SARVAM_STT_MODEL: str = os.getenv("SARVAM_STT_MODEL", "saaras:v4")
    SARVAM_TTS_MODEL: str = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
    SARVAM_TTS_SPEAKER: str = os.getenv("SARVAM_TTS_SPEAKER", "shubh")
    
    # Alternative LLM Provider Keys (Optional)
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
    
    # Supported Languages
    SUPPORTED_LANGUAGES: List[str] = ["mr-IN", "hi-IN", "en-IN"]
    DEFAULT_LANGUAGE: str = os.getenv("DEFAULT_LANGUAGE", "mr-IN")
    
    # Speaker mapping by language for natural tone
    SPEAKER_MAP = {
        "mr-IN": "shubh",
        "hi-IN": "shubh",
        "en-IN": "shubh"
    }
    
    # Data directory
    DATA_DIR: Path = ROOT_DIR / "data" / "raw"
    
    # Phase 4.5 User Call Restriction & Rate Limiting
    MAX_CONNECTED_CALLS_PER_DAY: int = int(os.getenv("MAX_CONNECTED_CALLS_PER_DAY", "3"))
    MAX_CALL_DURATION_SECONDS: int = int(os.getenv("MAX_CALL_DURATION_SECONDS", "180"))
    CALL_LIMIT_RESET_TIMEZONE: str = os.getenv("CALL_LIMIT_RESET_TIMEZONE", "Asia/Kolkata")
    TELEPHONY_AUTH_TOKEN: str = os.getenv("TELEPHONY_AUTH_TOKEN", "").strip()
    VOICELINK_AUTH_TOKEN: str = os.getenv("VOICELINK_AUTH_TOKEN", os.getenv("TELEPHONY_AUTH_TOKEN", "")).strip()
    VOICELINK_INBOUND_DID: str = os.getenv("VOICELINK_INBOUND_DID", "919484957050").strip()
    EXOTEL_ACCOUNT_SID: str = os.getenv("EXOTEL_ACCOUNT_SID", "").strip()
    EXOTEL_API_KEY: str = os.getenv("EXOTEL_API_KEY", "").strip()
    EXOTEL_API_TOKEN: str = os.getenv("EXOTEL_API_TOKEN", "").strip()
    EXOTEL_SUBDOMAIN: str = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com").strip()
    DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
    CALL_RECORDS_DB_PATH: Path = ROOT_DIR / "data" / "call_records.db"
    
    @property
    def has_sarvam_key(self) -> bool:
        """Returns True if a Sarvam API key is configured."""
        return bool(self.SARVAM_API_KEY and len(self.SARVAM_API_KEY) > 8)

    @property
    def has_any_llm_key(self) -> bool:
        """Returns True if any LLM API key is configured."""
        return bool(self.has_sarvam_key or self.OPENAI_API_KEY or self.GROQ_API_KEY)

settings = Settings()

