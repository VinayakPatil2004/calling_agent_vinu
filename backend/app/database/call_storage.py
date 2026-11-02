"""
Durable Call Storage Engine for CityLinc Voice Agent.
Provides atomic, ACID-compliant transactions with SQLite in WAL mode.
Guarantees race-free daily call counting and idempotent lifecycle auditing.
"""
import os
import sqlite3
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional, Dict, List, Any

from backend.app.core.config import settings
from backend.app.services.timezone_utils import get_utc_now, get_ist_business_date

logger = logging.getLogger("citylinc.database")


class CallStorageEngine:
    """Manages persistent call records and atomic daily limit counters."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or settings.CALL_RECORDS_DB_PATH
        self._ensure_db_initialized()

    def _get_connection(self) -> sqlite3.Connection:
        """Returns a configured SQLite connection with WAL mode and 30s busy timeout."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _ensure_db_initialized(self) -> None:
        """Creates the required tables and indexes if they do not exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        try:
            with conn:
                # 1. Daily call counters table
                conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_call_counters (
                    caller_hash TEXT NOT NULL,
                    ist_date TEXT NOT NULL,
                    connected_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (caller_hash, ist_date)
                );
                """)

                # 2. Complete call records & audit history table
                conn.execute("""
                CREATE TABLE IF NOT EXISTS call_records (
                    record_id TEXT PRIMARY KEY,
                    provider_call_id TEXT UNIQUE NOT NULL,
                    caller_hash TEXT NOT NULL,
                    caller_masked TEXT NOT NULL,
                    ist_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    connected_at TEXT,
                    disconnected_at TEXT,
                    deadline_at TEXT,
                    duration_seconds REAL DEFAULT 0.0,
                    termination_reason TEXT,
                    idempotency_key TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """)

                conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_call_records_lookup 
                ON call_records (caller_hash, ist_date, status);
                """)
                conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_call_records_active 
                ON call_records (status);
                """)
        finally:
            conn.close()

    def get_connected_count(self, caller_hash: str, ist_date: str) -> int:
        """Returns the current connected call count for a caller on a given IST date."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT connected_count FROM daily_call_counters WHERE caller_hash=? AND ist_date=?",
                (caller_hash, ist_date)
            )
            row = cur.fetchone()
            return int(row["connected_count"]) if row else 0
        finally:
            conn.close()

    def record_incoming_attempt(
        self,
        provider_call_id: str,
        caller_hash: str,
        caller_masked: str,
        ist_date: str
    ) -> bool:
        """
        Idempotently logs an incoming call attempt before connection.
        Does NOT increment the connected call counter.
        """
        now_iso = get_utc_now().isoformat()
        conn = self._get_connection()
        try:
            with conn:
                conn.execute("""
                INSERT OR IGNORE INTO call_records (
                    record_id, provider_call_id, caller_hash, caller_masked,
                    ist_date, status, idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'initiated', ?, ?, ?)
                """, (
                    str(uuid.uuid4()), provider_call_id, caller_hash, caller_masked,
                    ist_date, f"init_{provider_call_id}", now_iso, now_iso
                ))
            return True
        except Exception as e:
            logger.error(f"[CallStorage] Failed to log incoming attempt: {e}")
            return False
        finally:
            conn.close()

    def atomic_check_and_register_connected(
        self,
        provider_call_id: str,
        caller_hash: str,
        caller_masked: str,
        ist_date: str,
        max_limit: int = 3,
        duration_limit_sec: int = 180
    ) -> Tuple[bool, int, str]:
        """
        Atomically enforces the daily limit and registers a successfully connected call.
        
        Guarantees:
        - Atomic check and increment via BEGIN IMMEDIATE.
        - Strict limit of max_limit (3). Concurrent 4th calls will fail atomically.
        - Idempotency: duplicate webhooks for the same provider_call_id do not increment again.
        
        Returns:
            (allowed: bool, current_count: int, status_code: str)
            status_code in ('connected', 'already_registered', 'daily_limit_exceeded', 'database_error')
        """
        now_utc = get_utc_now()
        now_iso = now_utc.isoformat()
        deadline_iso = (now_utc + timedelta(seconds=duration_limit_sec)).isoformat()

        conn = self._get_connection()
        try:
            # BEGIN IMMEDIATE locks the database for writing immediately, preventing concurrent races
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # 1. Check idempotency: Has this provider call ID already been marked connected?
            cur.execute(
                "SELECT status, connected_at FROM call_records WHERE provider_call_id=?",
                (provider_call_id,)
            )
            existing_call = cur.fetchone()
            if existing_call and existing_call["status"] in ("connected", "completed", "timeout"):
                # Already counted in this call's lifetime!
                cur.execute(
                    "SELECT connected_count FROM daily_call_counters WHERE caller_hash=? AND ist_date=?",
                    (caller_hash, ist_date)
                )
                cnt_row = cur.fetchone()
                existing_cnt = cnt_row["connected_count"] if cnt_row else 1
                conn.rollback()
                return True, existing_cnt, "already_registered"

            # 2. Check current connected count for today in IST
            cur.execute(
                "SELECT connected_count FROM daily_call_counters WHERE caller_hash=? AND ist_date=?",
                (caller_hash, ist_date)
            )
            count_row = cur.fetchone()
            current_count = int(count_row["connected_count"]) if count_row else 0

            # 3. Enforce maximum limit (3)
            if current_count >= max_limit:
                # Limit reached! Record rejection in audit log
                if existing_call:
                    cur.execute("""
                    UPDATE call_records 
                    SET status='rejected_limit', termination_reason='daily_limit_exceeded', updated_at=?
                    WHERE provider_call_id=?
                    """, (now_iso, provider_call_id))
                else:
                    cur.execute("""
                    INSERT INTO call_records (
                        record_id, provider_call_id, caller_hash, caller_masked,
                        ist_date, status, termination_reason, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'rejected_limit', 'daily_limit_exceeded', ?, ?, ?)
                    """, (
                        str(uuid.uuid4()), provider_call_id, caller_hash, caller_masked,
                        ist_date, f"reject_{provider_call_id}", now_iso, now_iso
                    ))
                conn.commit()
                return False, current_count, "daily_limit_exceeded"

            # 4. Under limit: Increment count atomically
            new_count = current_count + 1
            cur.execute("""
            INSERT INTO daily_call_counters (caller_hash, ist_date, connected_count, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(caller_hash, ist_date) DO UPDATE SET
                connected_count = ?,
                updated_at = ?
            """, (caller_hash, ist_date, new_count, now_iso, new_count, now_iso))

            # 5. Record connected call with 180s deadline
            if existing_call:
                cur.execute("""
                UPDATE call_records 
                SET status='connected', connected_at=?, deadline_at=?, updated_at=?
                WHERE provider_call_id=?
                """, (now_iso, deadline_iso, now_iso, provider_call_id))
            else:
                cur.execute("""
                INSERT INTO call_records (
                    record_id, provider_call_id, caller_hash, caller_masked,
                    ist_date, status, connected_at, deadline_at, idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'connected', ?, ?, ?, ?, ?)
                """, (
                    str(uuid.uuid4()), provider_call_id, caller_hash, caller_masked,
                    ist_date, now_iso, deadline_iso, f"conn_{provider_call_id}", now_iso, now_iso
                ))

            conn.commit()
            return True, new_count, "connected"

        except Exception as e:
            conn.rollback()
            logger.error(f"[CallStorage] Atomic check/increment failure: {e}", exc_info=True)
            # Fail closed: reject on database failure to prevent unauthorized bypass
            return False, max_limit, "database_error"
        finally:
            conn.close()

    def record_call_termination(
        self,
        provider_call_id: str,
        termination_reason: str = "normal_hangup"
    ) -> Tuple[bool, float, str]:
        """
        Records the disconnect of a call.
        Calculates actual duration and ensures idempotency.
        
        Returns:
            (success: bool, actual_duration_seconds: float, status_code: str)
        """
        now_utc = get_utc_now()
        now_iso = now_utc.isoformat()

        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            cur.execute(
                "SELECT status, connected_at, duration_seconds FROM call_records WHERE provider_call_id=?",
                (provider_call_id,)
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return False, 0.0, "call_not_found"

            # Idempotency check: already terminated?
            if row["status"] in ("completed", "timeout"):
                conn.rollback()
                return True, float(row["duration_seconds"] or 0.0), "already_terminated"

            # Calculate actual duration
            duration = 0.0
            connected_at_str = row["connected_at"]
            if connected_at_str:
                try:
                    conn_dt = datetime.fromisoformat(connected_at_str)
                    if conn_dt.tzinfo is None:
                        conn_dt = conn_dt.replace(tzinfo=timezone.utc)
                    duration = max(0.0, (now_utc - conn_dt).total_seconds())
                except Exception:
                    pass

            final_status = "timeout" if termination_reason == "max_duration_180s" else "completed"

            cur.execute("""
            UPDATE call_records 
            SET status=?, disconnected_at=?, duration_seconds=?, termination_reason=?, updated_at=?
            WHERE provider_call_id=?
            """, (final_status, now_iso, duration, termination_reason, now_iso, provider_call_id))

            conn.commit()
            return True, duration, "terminated"

        except Exception as e:
            conn.rollback()
            logger.error(f"[CallStorage] Failed to record call termination: {e}")
            return False, 0.0, "database_error"
        finally:
            conn.close()

    def get_active_connected_calls(self) -> List[Dict[str, Any]]:
        """
        Retrieves all currently active connected calls.
        Used on server startup to resume or expire duration timers.
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT provider_call_id, connected_at, deadline_at FROM call_records WHERE status='connected'"
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_call_record(self, provider_call_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single call record by provider call ID for audit inspection."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM call_records WHERE provider_call_id=?", (provider_call_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


# Global singleton instance
call_storage = CallStorageEngine()

