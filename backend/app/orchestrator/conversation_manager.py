"""
Conversation Manager for CityLinc AI Voice Agent.
Handles multilingual session state, message history, language switching, and barge-in / interruption.
"""
import time
import uuid
import logging
from typing import Dict, List, Optional, Any
from backend.app.core.config import settings

logger = logging.getLogger("citylinc.orchestrator")

class ConversationSession:
    """Represents an active voice conversation session."""

    def __init__(self, session_id: str, default_language: str = "mr-IN"):
        self.session_id = session_id
        self.current_language = default_language
        self.has_explicit_language_preference: bool = False
        self.explicit_language: Optional[str] = None
        self.history: List[Dict[str, str]] = []
        self.current_turn_id: int = 0
        self.is_interrupted: bool = False
        self.consecutive_empty_count: int = 0
        self.last_turn_response: Dict[int, str] = {}
        self.created_at = time.time()
        self.last_active = time.time()

    def start_user_turn(self) -> int:
        """Atomically increments and reserves a new turn ID for an incoming user utterance."""
        self.current_turn_id += 1
        self.is_interrupted = False
        self.last_active = time.time()
        return self.current_turn_id

    def set_language(self, language: str, explicit: bool = False) -> None:
        """Sets the session language preference. If explicit=True, locks this preference until another explicit switch."""
        if language in settings.SUPPORTED_LANGUAGES:
            if explicit:
                self.has_explicit_language_preference = True
                self.explicit_language = language
                self.current_language = language
            elif not self.has_explicit_language_preference:
                self.current_language = language

    def add_user_message(self, text: str, language: Optional[str] = None, turn_id: Optional[int] = None) -> int:
        """Adds user message, updates language (if not explicitly locked), and ensures turn consistency."""
        if turn_id is not None:
            self.current_turn_id = max(self.current_turn_id, turn_id)
        else:
            self.current_turn_id += 1

        self.is_interrupted = False
        self.last_active = time.time()

        if language and language in settings.SUPPORTED_LANGUAGES:
            if not self.has_explicit_language_preference:
                self.current_language = language

        self.history.append({
            "role": "user",
            "content": text.strip()
        })

        # Limit history to last 20 messages to keep prompt focused
        if len(self.history) > 20:
            self.history = self.history[-20:]

        return self.current_turn_id

    def is_turn_valid(self, turn_id: int) -> bool:
        """Checks if a turn is still valid (not canceled or overtaken by newer turn)."""
        return (not self.is_interrupted) and (turn_id == self.current_turn_id)

    def add_assistant_message(self, text: str, turn_id: int) -> bool:
        """Appends assistant message if turn was not interrupted."""
        if not self.is_turn_valid(turn_id):
            logger.info(f"Skipping assistant message due to interruption/stale turn (turn {turn_id} vs current {self.current_turn_id})")
            return False

        self.history.append({
            "role": "assistant",
            "content": text.strip()
        })
        self.last_turn_response[turn_id] = text.strip()
        self.last_active = time.time()
        return True

    def interrupt(self, turn_id: Optional[int] = None) -> int:
        """Triggers barge-in: flags interruption and updates turn counter."""
        if turn_id is not None and turn_id < self.current_turn_id and self.is_interrupted:
            # Avoid redundant turn increments for already-interrupted turns
            logger.info(f"Session {self.session_id} already interrupted for turn {turn_id} (current: {self.current_turn_id})")
            return self.current_turn_id

        self.is_interrupted = True
        self.current_turn_id += 1
        logger.info(f"Session {self.session_id} interrupted! Advanced to turn {self.current_turn_id}")
        return self.current_turn_id

    def reset(self) -> None:
        """Resets history and counters."""
        self.history.clear()
        self.current_turn_id = 0
        self.is_interrupted = False
        self.consecutive_empty_count = 0
        self.has_explicit_language_preference = False
        self.explicit_language = None
        self.last_turn_response.clear()
        self.last_active = time.time()


class ConversationManager:
    """Manages active conversation sessions across the application."""

    def __init__(self):
        self.sessions: Dict[str, ConversationSession] = {}

    def get_or_create_session(self, session_id: Optional[str] = None, language: str = "mr-IN") -> ConversationSession:
        """Retrieves an existing session or creates a new one."""
        sid = session_id or str(uuid.uuid4())
        if sid not in self.sessions:
            self.sessions[sid] = ConversationSession(sid, default_language=language)
            logger.info(f"Created new session: {sid} [Default Language: {language}]")
        return self.sessions[sid]

    def remove_session(self, session_id: str) -> None:
        """Cleans up a session when WebSocket closes."""
        if session_id in self.sessions:
            del self.sessions[session_id]
            logger.info(f"Closed session: {session_id}")

# Global singleton
conversation_manager = ConversationManager()
