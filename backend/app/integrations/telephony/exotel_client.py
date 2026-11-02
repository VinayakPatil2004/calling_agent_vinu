"""
Exotel / VoiceLink Telephony REST API Client.
Handles carrier-level call control, including remote call hangup when 180s duration expires.
"""
import logging
from typing import Optional, Dict, Any, Callable
import httpx

from backend.app.core.config import settings

logger = logging.getLogger("citylinc.telephony.exotel")


class ExotelVoiceLinkClient:
    """Client for Exotel / VoiceLink carrier REST APIs."""

    def __init__(
        self,
        account_sid: Optional[str] = None,
        api_key: Optional[str] = None,
        api_token: Optional[str] = None,
        subdomain: Optional[str] = None
    ):
        self.account_sid = account_sid or settings.EXOTEL_ACCOUNT_SID
        self.api_key = api_key or settings.EXOTEL_API_KEY
        self.api_token = api_token or settings.EXOTEL_API_TOKEN
        self.subdomain = subdomain or settings.EXOTEL_SUBDOMAIN
        self._mock_hangup_handler: Optional[Callable[[str], bool]] = None

    @property
    def is_configured(self) -> bool:
        """Returns True if Exotel REST API credentials are fully configured."""
        return bool(self.account_sid and self.api_key and self.api_token)

    def set_mock_hangup_handler(self, handler: Optional[Callable[[str], bool]]) -> None:
        """Sets a mock hangup handler for unit and sandbox testing."""
        self._mock_hangup_handler = handler

    async def hangup_call(self, call_sid: str) -> bool:
        """
        Issues carrier REST API request to immediately terminate an active call.
        API: POST https://<api_key>:<api_token>@api.exotel.com/v1/Accounts/<account_sid>/Calls/<call_sid>.json
        Data: Status=completed
        """
        if not call_sid:
            logger.error("[ExotelClient] Cannot hangup call: call_sid is empty")
            return False

        # Use mock handler if configured (e.g. during testing)
        if self._mock_hangup_handler:
            logger.info(f"[ExotelClient] Invoking mock hangup for call {call_sid}")
            return self._mock_hangup_handler(call_sid)

        if not self.is_configured:
            logger.warning(
                f"[ExotelClient] Exotel REST API credentials not configured (EXOTEL_ACCOUNT_SID/EXOTEL_API_KEY). "
                f"Cannot send outbound hangup to carrier for {call_sid}. Relying on WebSocket close."
            )
            return False

        url = f"https://{self.subdomain}/v1/Accounts/{self.account_sid}/Calls/{call_sid}.json"
        auth = (self.api_key, self.api_token)
        data = {"Status": "completed"}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, auth=auth, data=data)
                if resp.status_code in (200, 204):
                    logger.info(f"[ExotelClient] Successfully terminated carrier call {call_sid} via REST API")
                    return True
                else:
                    logger.error(
                        f"[ExotelClient] Failed to terminate carrier call {call_sid}: "
                        f"Status={resp.status_code} Response={resp.text}"
                    )
                    return False
        except Exception as e:
            logger.error(f"[ExotelClient] Exception during carrier call hangup for {call_sid}: {e}", exc_info=True)
            return False


# Global singleton instance
exotel_client = ExotelVoiceLinkClient()

