"""
Mobile Number Normalization and Validation Utility for CityLinc Voice Agent.
Normalizes Indian mobile numbers to canonical E.164 format (+91XXXXXXXXXX)
and provides privacy protection utilities (masking and SHA-256 hashing).
"""
import re
import hashlib
from typing import Tuple, Optional

# Regex for valid Indian mobile subscriber numbers (10 digits starting with 6, 7, 8, or 9)
INDIAN_MOBILE_REGEX = re.compile(r"^[6-9]\d{9}$")


def normalize_mobile_number(raw_number: Optional[str]) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Normalizes a provider-supplied mobile number to E.164 format (+91XXXXXXXXXX).
    
    Accepts:
    - 10-digit mobile number: '9876543210' -> '+919876543210'
    - 11-digit leading-zero number: '09876543210' -> '+919876543210'
    - 12-digit with 91 prefix: '919876543210' -> '+919876543210'
    - E.164 format: '+919876543210' -> '+919876543210'
    - Formatted strings: '+91 98765-43210', '(09876) 543210' -> '+919876543210'
    
    Returns:
        (is_valid: bool, normalized_e164: Optional[str], error_reason: Optional[str])
    """
    if not raw_number or not isinstance(raw_number, str):
        return False, None, "missing_or_empty_caller_id"

    # Strip whitespace, hyphens, brackets, dots
    cleaned = re.sub(r"[\s\-\(\)\.]", "", raw_number.strip())
    if not cleaned:
        return False, None, "empty_caller_id"

    # Handle leading +
    has_plus = cleaned.startswith("+")
    digits_only = cleaned[1:] if has_plus else cleaned

    if not digits_only.isdigit():
        return False, None, "non_numeric_characters_in_caller_id"

    subscriber_digits: Optional[str] = None

    if has_plus:
        # e.g. +919876543210
        if digits_only.startswith("91") and len(digits_only) == 12:
            subscriber_digits = digits_only[2:]
        else:
            return False, None, f"unsupported_country_code_or_length_{cleaned[:6]}"
    else:
        # No leading plus
        if len(digits_only) == 10:
            subscriber_digits = digits_only
        elif len(digits_only) == 11 and digits_only.startswith("0"):
            subscriber_digits = digits_only[1:]
        elif len(digits_only) == 12 and digits_only.startswith("91"):
            subscriber_digits = digits_only[2:]
        else:
            return False, None, f"invalid_number_length_{len(digits_only)}"

    # Validate 10-digit Indian mobile number
    if not INDIAN_MOBILE_REGEX.match(subscriber_digits):
        return False, None, f"invalid_indian_mobile_prefix_{subscriber_digits[0]}"

    canonical_e164 = f"+91{subscriber_digits}"
    return True, canonical_e164, None


def normalize_to_e164(raw_number: Optional[str]) -> str:
    """
    Convenience function that returns normalized E.164 string or raises ValueError.
    """
    is_valid, normalized, reason = normalize_mobile_number(raw_number)
    if not is_valid:
        raise ValueError(f"Invalid Indian mobile number: {reason}")
    return normalized


def is_valid_indian_mobile(raw_number: Optional[str]) -> bool:
    """
    Returns True if raw_number is a valid Indian mobile number.
    """
    is_valid, _, _ = normalize_mobile_number(raw_number)
    return is_valid


def mask_phone_number(phone: Optional[str]) -> str:
    """
    Masks a phone number for privacy in application logs.
    Example: '+919876543210' -> '+91XXXXXX3210'.
    """
    if not phone:
        return "<anonymous>"
    phone_str = str(phone).strip()
    if len(phone_str) < 7:
        return "<invalid_phone>"
    return f"{phone_str[:3]}XXXXXX{phone_str[-4:]}"


def hash_phone_number(raw_or_normalized_phone: str) -> str:
    """
    Generates deterministic SHA-256 hex digest of the normalized E.164 phone number.
    Normalizes the phone number first if valid, ensuring identical hashes for all valid formats.
    Used for privacy-preserving database storage and rate limit indexing.
    """
    if not raw_or_normalized_phone:
        return ""
    is_valid, normalized, _ = normalize_mobile_number(raw_or_normalized_phone)
    target = normalized if is_valid and normalized else raw_or_normalized_phone.strip()
    return hashlib.sha256(target.encode("utf-8")).hexdigest()
