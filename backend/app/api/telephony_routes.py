"""
Telephony Webhook and Lifecycle Routes for CityLinc Voice Agent.
Handles incoming call screening, daily rate limit enforcement (3 calls/day in IST),
answering events, and automatic 180-second duration termination.
Supports both JSON and Form-encoded payloads (Exotel / Twilio / Dograh standard).
"""
import logging
from typing import Optional, Dict, Any
from fastapi import APIRouter, Request, Header, HTTPException, Response
from pydantic import BaseModel

from backend.app.core.config import settings
from backend.app.services.phone_normalizer import normalize_mobile_number, mask_phone_number
from backend.app.services.call_restriction_service import (
    call_restriction_service,
    DAILY_LIMIT_EXCEEDED_MESSAGE_MR
)

logger = logging.getLogger("citylinc.telephony")

router = APIRouter(prefix="/api/telephony", tags=["Telephony"])


def _verify_webhook_auth(auth_header: Optional[str]) -> bool:
    """Verifies telephony provider authorization header if configured."""
    if not settings.TELEPHONY_AUTH_TOKEN:
        return True  # If no auth token configured, permit internal / open test webhooks
    if not auth_header:
        return False
    token = auth_header.replace("Bearer ", "").strip()
    return token == settings.TELEPHONY_AUTH_TOKEN


async def _extract_webhook_payload(request: Request) -> Dict[str, Any]:
    """Extracts payload regardless of JSON or Form encoding."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            return await request.json()
        except Exception:
            return {}
    else:
        try:
            form = await request.form()
            return dict(form)
        except Exception:
            return {}


@router.post("/inbound")
async def inbound_call_webhook(
    request: Request,
    authorization: Optional[str] = Header(None)
):
    """
    Inbound call screening endpoint.
    Called by telephony provider when an incoming call arrives (ringing state).
    Enforces daily limit of 3 connected calls per user per day in IST.
    
    Returns:
        JSON response with action: 'allow' or 'reject'.
    """
    if not _verify_webhook_auth(authorization):
        logger.warning("[Telephony] Unauthorized inbound webhook attempt.")
        raise HTTPException(status_code=401, detail="Unauthorized telephony webhook.")

    payload = await _extract_webhook_payload(request)
    
    # Standardize caller ID from various provider conventions (From, Caller, caller_id, phone)
    raw_caller = (
        payload.get("From") or 
        payload.get("Caller") or 
        payload.get("caller_id") or 
        payload.get("phone") or 
        payload.get("FromNumber")
    )
    provider_call_id = (
        payload.get("CallSid") or 
        payload.get("call_id") or 
        payload.get("CallId") or 
        payload.get("sid")
    )

    if not provider_call_id:
        logger.error("[Telephony] Missing CallSid / call_id in inbound webhook.")
        return {
            "action": "reject",
            "reason": "missing_provider_call_id",
            "status": "invalid_request"
        }

    # Pre-connection check against daily limit of 3 calls
    check_result = call_restriction_service.check_daily_call_limit(raw_caller)

    # Log incoming attempt in audit history without incrementing count
    if check_result.normalized_phone:
        from backend.app.services.phone_normalizer import hash_phone_number
        from backend.app.services.timezone_utils import get_ist_business_date
        call_restriction_service.storage.record_incoming_attempt(
            provider_call_id=provider_call_id,
            caller_hash=hash_phone_number(check_result.normalized_phone),
            caller_masked=check_result.masked_phone or "",
            ist_date=get_ist_business_date()
        )

    if check_result.allowed:
        logger.info(
            f"[Telephony] Inbound call ALLOWED: id={provider_call_id} | "
            f"caller={check_result.masked_phone} | Remaining: {check_result.remaining_calls}"
        )
        # Check if provider requested TwiML / XML
        accept_header = request.headers.get("accept", "").lower()
        if "xml" in accept_header or "text/xml" in accept_header:
            xml_resp = (
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Response>\n'
                f'    <Connect maxDuration="180"/>\n'
                f'</Response>'
            )
            return Response(content=xml_resp, media_type="application/xml")

        return {
            "action": "connect",
            "status": "allowed",
            "provider_call_id": provider_call_id,
            "caller": check_result.masked_phone,
            "connected_today": check_result.connected_count,
            "remaining_calls": check_result.remaining_calls,
            "max_duration_seconds": 180
        }
    else:
        logger.warning(
            f"[Telephony] Inbound call REJECTED: id={provider_call_id} | "
            f"caller={check_result.masked_phone} | Status={check_result.status}"
        )
        accept_header = request.headers.get("accept", "").lower()
        if "xml" in accept_header or "text/xml" in accept_header:
            xml_resp = (
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Response>\n'
                f'    <Say language="mr-IN">{DAILY_LIMIT_EXCEEDED_MESSAGE_MR}</Say>\n'
                f'    <Reject reason="busy"/>\n'
                f'</Response>'
            )
            return Response(content=xml_resp, media_type="application/xml")

        return {
            "action": "reject",
            "status": check_result.status,
            "provider_call_id": provider_call_id,
            "caller": check_result.masked_phone,
            "connected_today": check_result.connected_count,
            "remaining_calls": 0,
            "reason": "busy",
            "message": check_result.message or DAILY_LIMIT_EXCEEDED_MESSAGE_MR
        }


@router.post("/status")
async def call_status_webhook(
    request: Request,
    authorization: Optional[str] = Header(None)
):
    """
    Call status callback endpoint.
    Receives lifecycle updates: 'answered', 'connected', 'completed', 'busy', 'failed'.
    - 'answered' / 'connected': Atomically increments count and starts 180-second duration timer.
    - 'completed' / 'disconnected': Cancels timer and records final call duration.
    - 'busy' / 'failed': Ignored from count (missed/failed calls do NOT increment).
    """
    if not _verify_webhook_auth(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized telephony webhook.")

    payload = await _extract_webhook_payload(request)
    provider_call_id = payload.get("CallSid") or payload.get("call_id") or payload.get("CallId") or payload.get("sid")
    raw_caller = payload.get("From") or payload.get("Caller") or payload.get("caller_id") or payload.get("phone")
    call_status = (payload.get("CallStatus") or payload.get("status") or payload.get("call_status") or payload.get("event") or "").lower()

    if not provider_call_id:
        return {"status": "ignored", "reason": "missing_provider_call_id"}

    logger.info(f"[Telephony] Status update for {provider_call_id}: status='{call_status}'")

    # 1. Answered / Connected -> Atomically register connection
    if call_status in ("answered", "in-progress", "connected"):
        conn_res = await call_restriction_service.register_connected_call(
            provider_call_id=provider_call_id,
            raw_phone=raw_caller or ""
        )
        return {
            "status": "processed",
            "event": "connected",
            "allowed": conn_res.allowed,
            "count": conn_res.connected_count,
            "remaining": conn_res.remaining_calls,
            "max_duration_seconds": conn_res.max_duration_seconds
        }

    # 2. Completed / Disconnected -> Register disconnect and duration
    elif call_status in ("completed", "canceled", "disconnected"):
        reason = payload.get("HangupReason") or payload.get("termination_reason") or "normal_hangup"
        disc_res = call_restriction_service.register_call_disconnect(
            provider_call_id=provider_call_id,
            termination_reason=str(reason)
        )
        return {
            "status": "processed",
            "event": "disconnected",
            "duration_seconds": disc_res.duration_seconds,
            "termination_reason": disc_res.termination_reason
        }

    # 3. Missed, Busy, Failed -> Do NOT increment counter
    elif call_status in ("busy", "failed", "no-answer"):
        logger.info(f"[Telephony] Call {provider_call_id} ended without connection ({call_status}). Counter not incremented.")
        call_restriction_service.register_call_disconnect(
            provider_call_id=provider_call_id,
            termination_reason=f"unconnected_{call_status}"
        )
        return {
            "status": "processed",
            "event": "unconnected",
            "call_status": call_status,
            "counter_incremented": False
        }

    return {"status": "acknowledged", "event": call_status}


@router.get("/usage")
async def check_caller_usage(phone: str):
    """
    Informational endpoint to inspect daily call usage and remaining quota for a caller.
    """
    check = call_restriction_service.check_daily_call_limit(phone)
    return {
        "caller": check.masked_phone,
        "is_valid": check.status != "invalid_caller",
        "allowed": check.allowed,
        "connected_today": check.connected_count,
        "remaining_calls": check.remaining_calls,
        "max_daily_limit": settings.MAX_CONNECTED_CALLS_PER_DAY,
        "max_call_duration_seconds": settings.MAX_CALL_DURATION_SECONDS,
        "reset_timezone": settings.CALL_LIMIT_RESET_TIMEZONE
    }
