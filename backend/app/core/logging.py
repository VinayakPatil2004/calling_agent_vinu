"""
Logging configuration for CityLinc AI Voice Agent.
Configures UTF-8 safe console logging on Windows.
"""
import logging
import sys

class SafeStreamWrapper:
    """Wraps a stream to prevent Windows cp1252 UnicodeEncodeErrors when logging Indic scripts."""
    def __init__(self, stream):
        self.stream = stream

    def write(self, msg):
        try:
            self.stream.write(msg)
        except (UnicodeEncodeError, TypeError):
            # Fallback to ascii representation with backslash escapes
            safe_msg = msg.encode('ascii', errors='backslashreplace').decode('ascii')
            self.stream.write(safe_msg)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configures structured console logging with utf-8 safety."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger("citylinc")
    if logger.handlers:
        return logger
        
    logger.setLevel(numeric_level)
    handler = logging.StreamHandler(SafeStreamWrapper(sys.stdout))
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

logger = setup_logging()
