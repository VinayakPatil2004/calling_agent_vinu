"""
Daily Call Limit and Duration Restriction Service for CityLinc Voice Agent.
Enforces business rules:
- Max 3 connected calls per user per day (Asia/Kolkata calendar day, resets at 12:00 AM IST).
- Max 180 seconds duration per connected call.
- Fourth call rejected with Marathi limit message.
- Fail-closed concurrency safety.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable, Awaitable
from datetime import datetime, timezone, timedelta

from backend.app.core.config import settings
from backend.app.services.phone_normalizer import (
    normalize_mobile_number,
    mask_phone_number,
    hash_phone_number
)
from backend.app.services.timezone_utils import (
    get_ist_business_date,
    get_utc_now,
    IST
)
from backend.app.database.call_storage import call_storage, CallStorageEngine

logger = logging.getLogger("citylinc.call_restriction")

# Approved Marathi business message for users who reached the 3-call daily limit
DAILY_LIMIT_EXCEEDED_MESSAGE_MR = (
    "आजची तुमची कॉल मर्यादा पूर्ण झाली आहे. तुम्ही आज तीन कॉल केले आहेत. कृपया उद्या पुन्हा संपर्क साधा."
)


@dataclass
class CallCheckResult:
    allowed: bool
    status: str  # 'allowed', 'rejected_daily_limit', 'invalid_caller', 'service_unavailable'
    connected_count: int
    remaining_calls: int
    normalized_phone: Optional[str] = None
    masked_phone: Optional[str] = None
    message: Optional[str] = None


@dataclass
class CallConnectResult:
    allowed: bool
    status: str  # 'connected', 'already_registered', 'rejected_daily_limit', 'service_unavailable'
    connected_count: int
    remaining_calls: int
    max_duration_seconds: int = 180
    message: Optional[str] = None


@dataclass
class CallDisconnectResult:
    success: bool
    status: str  # 'terminated', 'already_terminated', 'call_not_found', 'error'
    duration_seconds: float
    termination_reason: str


class CallRestrictionService:
    """Orchestrates daily rate limiting and server-side duration timers."""

    def __init__(self, storage: Optional[CallStorageEngine] = None):
        self.storage = storage or call_storage
        self.max_daily_calls = settings.MAX_CONNECTED_CALLS_PER_DAY
        self.max_duration_sec = settings.MAX_CALL_DURATION_SECONDS
        self._active_timeouts: Dict[str, asyncio.Task] = {}
        self._timeout_callbacks: Dict[str, Callable[[str], Awaitable[None]]] = {}
        self._warning_callbacks: Dict[str, Callable[[str], Awaitable[None]]] = {}

    def normalize_mobile(self, raw_phone: str):
        """Validates and normalizes phone number to E.164."""
        return normalize_mobile_number(raw_phone)

    def get_ist_date(self, dt: Optional[datetime] = None) -> str:
        """Returns canonical IST date string (YYYY-MM-DD)."""
        return get_ist_business_date(dt)

    def check_daily_call_limit(
        self,
        raw_phone: Optional[str] = None,
        ist_date: Optional[str] = None,
        caller_phone: Optional[str] = None
    ) -> CallCheckResult:
        """
        Pre-connection check: determines if another call is permitted for today.
        Does NOT increment the counter (increments occur ONLY on actual connection).
        
        Returns:
            CallCheckResult with allowed=True if connected_count < 3, else allowed=False.
        """
        effective_phone = raw_phone or caller_phone
        # 1. Validate caller identity
        is_valid, canonical_phone, err_reason = normalize_mobile_number(effective_phone)
        if not is_valid or not canonical_phone:
            logger.warning(f"[RateLimit] Inbound call rejected: Invalid caller identity ({err_reason})")
            return CallCheckResult(
                allowed=False,
                status="invalid_caller",
                connected_count=0,
                remaining_calls=0,
                message="Invalid or missing caller phone number."
            )

        masked = mask_phone_number(canonical_phone)
        caller_h = hash_phone_number(canonical_phone)
        effective_date = ist_date or get_ist_business_date()

        try:
            current_count = self.storage.get_connected_count(caller_h, effective_date)
            remaining = max(0, self.max_daily_calls - current_count)

            logger.info(
                f"[RateLimit] Pre-check for {masked} on IST {effective_date}: "
                f"Connected today = {current_count}/{self.max_daily_calls} | Remaining = {remaining}"
            )

            if current_count < self.max_daily_calls:
                return CallCheckResult(
                    allowed=True,
                    status="allowed",
                    connected_count=current_count,
                    remaining_calls=remaining,
                    normalized_phone=canonical_phone,
                    masked_phone=masked
                )
            else:
                logger.warning(
                    f"[RateLimit] Inbound call REJECTED for {masked}: "
                    f"Daily limit reached ({current_count}/{self.max_daily_calls}) on IST {effective_date}."
                )
                return CallCheckResult(
                    allowed=False,
                    status="rejected_daily_limit",
                    connected_count=current_count,
                    remaining_calls=0,
                    normalized_phone=canonical_phone,
                    masked_phone=masked,
                    message=DAILY_LIMIT_EXCEEDED_MESSAGE_MR
                )

        except Exception as e:
            logger.error(f"[RateLimit] Database error during pre-check for {masked}: {e}", exc_info=True)
            # Fail closed: reject on system error rather than allowing unlimited calls
            return CallCheckResult(
                allowed=False,
                status="service_unavailable",
                connected_count=self.max_daily_calls,
                remaining_calls=0,
                normalized_phone=canonical_phone,
                masked_phone=masked,
                message="Service temporarily unavailable."
            )

    def get_remaining_daily_calls(
        self,
        raw_phone: Optional[str] = None,
        ist_date: Optional[str] = None,
        caller_phone: Optional[str] = None
    ) -> int:
        """Returns the number of remaining connected calls allowed for this user today."""
        res = self.check_daily_call_limit(raw_phone=raw_phone, ist_date=ist_date, caller_phone=caller_phone)
        return res.remaining_calls

    async def register_connected_call(
        self,
        provider_call_id: str,
        raw_phone: Optional[str] = None,
        ist_date: Optional[str] = None,
        on_timeout_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        caller_phone: Optional[str] = None,
        on_warning_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        warning_lead_seconds: int = 20
    ) -> CallConnectResult:
        """
        Atomically registers an answered/connected call.
        Increments the daily counter if under the limit of 3.
        Schedules the 180-second duration timeout.
        
        Guarantees:
        - Atomic execution in SQLite WAL mode.
        - Idempotent: duplicate webhook deliveries for the same provider_call_id do not double-increment.
        - Concurrent fourth calls are rejected atomically.
        """
        if not provider_call_id:
            logger.error("[RateLimit] Cannot connect call: missing provider_call_id")
            return CallConnectResult(
                allowed=False,
                status="invalid_caller",
                connected_count=self.max_daily_calls,
                remaining_calls=0,
                message="Missing provider call identifier."
            )

        effective_phone = raw_phone or caller_phone
        is_valid, canonical_phone, err_reason = normalize_mobile_number(effective_phone)
        if not is_valid or not canonical_phone:
            logger.warning(f"[RateLimit] Connect failed for {provider_call_id}: {err_reason}")
            return CallConnectResult(
                allowed=False,
                status="invalid_caller",
                connected_count=0,
                remaining_calls=0,
                message="Invalid or missing caller phone number."
            )

        masked = mask_phone_number(canonical_phone)
        caller_h = hash_phone_number(canonical_phone)
        effective_date = ist_date or get_ist_business_date()

        # Atomically check and register connection in database
        try:
            allowed, count, status_code = self.storage.atomic_check_and_register_connected(
                provider_call_id=provider_call_id,
                caller_hash=caller_h,
                caller_masked=masked,
                ist_date=effective_date,
                max_limit=self.max_daily_calls,
                duration_limit_sec=self.max_duration_sec
            )
        except Exception as e:
            logger.error(f"[RateLimit] Database error during atomic connect for {masked}: {e}", exc_info=True)
            return CallConnectResult(
                allowed=False,
                status="service_unavailable",
                connected_count=self.max_daily_calls,
                remaining_calls=0,
                max_duration_seconds=self.max_duration_sec,
                message="Service temporarily unavailable."
            )

        remaining = max(0, self.max_daily_calls - count)

        if allowed:
            logger.info(
                f"[RateLimit] Call CONNECTED: id={provider_call_id} | caller={masked} | "
                f"Today: {count}/{self.max_daily_calls} | Remaining: {remaining} | Status: {status_code}"
            )
            # Schedule 180-second server-side duration enforcement with graceful pre-timeout notification
            self.schedule_call_timeout(
                provider_call_id,
                timeout_seconds=self.max_duration_sec,
                warning_lead_seconds=warning_lead_seconds,
                on_warning_callback=on_warning_callback,
                on_timeout_callback=on_timeout_callback
            )

            return CallConnectResult(
                allowed=True,
                status=status_code,
                connected_count=count,
                remaining_calls=remaining,
                max_duration_seconds=self.max_duration_sec
            )
        else:
            logger.warning(
                f"[RateLimit] Call REJECTED at connection: id={provider_call_id} | caller={masked} | "
                f"Count={count} | Status={status_code}"
            )
            return CallConnectResult(
                allowed=False,
                status=status_code,
                connected_count=count,
                remaining_calls=0,
                message=DAILY_LIMIT_EXCEEDED_MESSAGE_MR if status_code == "daily_limit_exceeded" else "Service unavailable."
            )

    def schedule_call_timeout(
        self,
        provider_call_id: str,
        timeout_seconds: int = 180,
        warning_lead_seconds: int = 20,
        on_warning_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        on_timeout_callback: Optional[Callable[[str], Awaitable[None]]] = None
    ) -> None:
        """
        Schedules a background asynchronous timer to automatically terminate the call at 180 seconds,
        with a natural pre-timeout notification triggered before the hard limit.
        """
        self.cancel_call_timeout(provider_call_id)
        if on_timeout_callback:
            self._timeout_callbacks[provider_call_id] = on_timeout_callback
        if on_warning_callback:
            self._warning_callbacks[provider_call_id] = on_warning_callback

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._call_timeout_runner(
                    provider_call_id,
                    timeout_seconds,
                    warning_lead_seconds=warning_lead_seconds
                )
            )
            self._active_timeouts[provider_call_id] = task
            logger.info(
                f"[CallDuration] Scheduled {timeout_seconds}s timeout "
                f"(graceful warning at {timeout_seconds - warning_lead_seconds}s) for call {provider_call_id}"
            )
        except RuntimeError:
            logger.debug(f"[CallDuration] No active event loop to schedule timeout for {provider_call_id}")

    def cancel_call_timeout(self, provider_call_id: str) -> bool:
        """Cancels a scheduled timeout task when the call disconnects normally or is rescheduled."""
        task = self._active_timeouts.pop(provider_call_id, None)
        if task and not task.done():
            task.cancel()
            logger.debug(f"[CallDuration] Cancelled pending timeout for call {provider_call_id}")
            return True
        return False

    async def _call_timeout_runner(
        self,
        provider_call_id: str,
        timeout_seconds: int,
        warning_lead_seconds: int = 20
    ) -> None:
        """Background coroutine that dispatches warning notification before enforcing hard limit."""
        try:
            warning_delay = max(0, timeout_seconds - warning_lead_seconds)
            if warning_delay > 0:
                await asyncio.sleep(warning_delay)
                # Dispatch graceful pre-timeout warning callback if registered
                warn_cb = self._warning_callbacks.pop(provider_call_id, None)
                if warn_cb:
                    try:
                        logger.info(
                            f"[CallDuration] Pre-timeout graceful warning triggered for {provider_call_id} "
                            f"({warning_lead_seconds}s before limit)"
                        )
                        await warn_cb(provider_call_id)
                    except Exception as we:
                        logger.debug(f"[CallDuration] Graceful warning callback exception for {provider_call_id}: {we}")
                # Sleep remaining duration up to the strict 180s hard limit
                await asyncio.sleep(warning_lead_seconds)
            else:
                await asyncio.sleep(timeout_seconds)

            logger.warning(
                f"[CallDuration] Call {provider_call_id} reached maximum allowed duration of {timeout_seconds}s! "
                f"Enforcing automatic termination."
            )
            await self.terminate_call_at_limit(provider_call_id)
        except asyncio.CancelledError:
            # Normal hangup cancelled the timer
            pass
        except Exception as e:
            logger.error(f"[CallDuration] Exception in timeout runner for {provider_call_id}: {e}", exc_info=True)
        finally:
            self._active_timeouts.pop(provider_call_id, None)
            self._warning_callbacks.pop(provider_call_id, None)

    async def terminate_call_at_limit(self, provider_call_id: str) -> None:
        """
        Enforces call termination when 180-second limit is reached.
        Marks call record in database, issues carrier hangup request, and executes termination callback.
        """
        success, duration, status = self.storage.record_call_termination(
            provider_call_id,
            termination_reason="max_duration_180s"
        )
        logger.info(
            f"[CallDuration] Call {provider_call_id} terminated at limit. "
            f"Duration: {duration:.1f}s | Status: {status}"
        )

        # 1. Attempt carrier-level REST hangup (if configured or mock enabled)
        try:
            from backend.app.integrations.telephony.exotel_client import exotel_client
            await exotel_client.hangup_call(provider_call_id)
        except Exception as e:
            logger.debug(f"[CallDuration] Carrier REST hangup call attempt: {e}")

        # 2. Execute local termination callback (WebSocket close / stream tear down)
        callback = self._timeout_callbacks.pop(provider_call_id, None)
        if callback:
            try:
                await callback(provider_call_id)
            except Exception as e:
                logger.error(f"[CallDuration] Error executing termination callback for {provider_call_id}: {e}")

    def register_call_disconnect(
        self,
        provider_call_id: str,
        termination_reason: str = "normal_hangup"
    ) -> CallDisconnectResult:
        """
        Handles normal call disconnect event from telephony provider or client.
        Cancels pending timeout and records actual duration.
        """
        self.cancel_call_timeout(provider_call_id)
        self._timeout_callbacks.pop(provider_call_id, None)
        self._warning_callbacks.pop(provider_call_id, None)

        success, duration, status = self.storage.record_call_termination(
            provider_call_id,
            termination_reason=termination_reason
        )
        logger.info(
            f"[CallDuration] Disconnect registered for {provider_call_id}: "
            f"Duration: {duration:.1f}s | Reason: {termination_reason} | Status: {status}"
        )
        return CallDisconnectResult(
            success=success,
            status=status,
            duration_seconds=duration,
            termination_reason=termination_reason
        )

    def recover_active_call_timeouts(self) -> int:
        """
        Crash-recovery routine invoked on server startup.
        Inspects active calls in database. If a call passed its 180s deadline while the server
        was restarting, immediately terminates it. Otherwise reschedules remaining seconds.
        """
        now_utc = get_utc_now()
        active_calls = self.storage.get_active_connected_calls()
        recovered_count = 0

        for call in active_calls:
            call_id = call["provider_call_id"]
            deadline_str = call.get("deadline_at")
            if not deadline_str:
                continue

            try:
                deadline_dt = datetime.fromisoformat(deadline_str)
                if deadline_dt.tzinfo is None:
                    deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)

                remaining_sec = (deadline_dt - now_utc).total_seconds()
                if remaining_sec <= 0:
                    logger.warning(f"[CrashRecovery] Call {call_id} expired during server downtime. Marking terminated.")
                    self.storage.record_call_termination(call_id, termination_reason="max_duration_180s")
                else:
                    logger.info(f"[CrashRecovery] Rescheduling timeout for {call_id} with remaining {remaining_sec:.1f}s")
                    self.schedule_call_timeout(call_id, int(remaining_sec))
                recovered_count += 1
            except Exception as e:
                logger.error(f"[CrashRecovery] Error evaluating active call {call_id}: {e}")

        return recovered_count


# Global singleton instance
call_restriction_service = CallRestrictionService()
