"""
TOTP (Time-based One-Time Password) service for Two-Factor Authentication
"""

import secrets
import hashlib
import base64
import io
import json
import logging
from typing import List, Tuple, Optional

import pyotp
import qrcode

logger = logging.getLogger(__name__)

# Application name shown in authenticator apps
APP_NAME = "AI Hub"


def generate_secret() -> str:
    """
    Generate a Base32-encoded TOTP secret.

    Returns:
        Base32-encoded secret suitable for TOTP
    """
    return pyotp.random_base32()


def get_totp_uri(secret: str, email: str, issuer: str = APP_NAME) -> str:
    """
    Generate an otpauth:// URI for use with authenticator apps.

    Args:
        secret: Base32-encoded TOTP secret
        email: User's email/username for identification
        issuer: Application name shown in authenticator

    Returns:
        otpauth:// URI string
    """
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer)


def generate_qr_code(uri: str) -> str:
    """
    Generate a QR code image as a base64-encoded data URI.

    Args:
        uri: The otpauth:// URI to encode

    Returns:
        Base64-encoded PNG image as a data URI
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(uri)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    # Convert to base64
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    img_base64 = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{img_base64}"


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    """
    Verify a 6-digit TOTP code.

    Args:
        secret: Base32-encoded TOTP secret
        code: 6-digit code from authenticator app
        valid_window: Number of time steps to check before/after current time
                     (default 1 = 30 seconds leeway each direction)

    Returns:
        True if code is valid, False otherwise
    """
    if not code or not secret:
        return False

    # Normalize code - remove spaces and dashes
    code = code.replace(" ", "").replace("-", "")

    # Must be 6 digits
    if len(code) != 6 or not code.isdigit():
        return False

    try:
        totp = pyotp.TOTP(secret)
        return totp.verify(code, valid_window=valid_window)
    except Exception as e:
        logger.error(f"TOTP verification error: {e}")
        return False


def generate_recovery_codes(count: int = 10) -> Tuple[List[str], List[str]]:
    """
    Generate backup/recovery codes for account recovery.

    These codes are one-time use and should be stored hashed.
    The plaintext codes are returned only once for the user to save.

    Args:
        count: Number of recovery codes to generate

    Returns:
        Tuple of (plaintext_codes, hashed_codes)
        - plaintext_codes: List of codes to show user (format: XXXX-XXXX-XXXX)
        - hashed_codes: List of SHA-256 hashed codes to store in database
    """
    plaintext_codes = []
    hashed_codes = []

    for _ in range(count):
        # Generate 12 random alphanumeric characters
        code_bytes = secrets.token_bytes(9)  # 9 bytes = 12 base64 chars
        code_raw = base64.b32encode(code_bytes).decode()[:12].upper()

        # Format as XXXX-XXXX-XXXX for readability
        formatted_code = f"{code_raw[:4]}-{code_raw[4:8]}-{code_raw[8:12]}"
        plaintext_codes.append(formatted_code)

        # Hash for storage (normalize first - remove dashes and lowercase)
        normalized = code_raw.lower()
        hashed = hashlib.sha256(normalized.encode()).hexdigest()
        hashed_codes.append(hashed)

    return plaintext_codes, hashed_codes


def verify_recovery_code(stored_hashes_json: str, code: str) -> Tuple[bool, Optional[str]]:
    """
    Verify a recovery code against stored hashes.

    Recovery codes are one-time use - if valid, the caller should
    remove the used code hash from storage.

    Args:
        stored_hashes_json: JSON array of hashed recovery codes
        code: Recovery code to verify (format: XXXX-XXXX-XXXX or XXXXXXXXXXXX)

    Returns:
        Tuple of (is_valid, used_hash)
        - is_valid: True if code matches one of the stored hashes
        - used_hash: The hash that was matched (to remove from storage), or None
    """
    if not stored_hashes_json or not code:
        return False, None

    try:
        stored_hashes = json.loads(stored_hashes_json)
        if not isinstance(stored_hashes, list):
            return False, None
    except json.JSONDecodeError:
        return False, None

    # Normalize input code - remove dashes/spaces, lowercase
    normalized_code = code.replace("-", "").replace(" ", "").lower()

    # Must be 12 alphanumeric characters
    if len(normalized_code) != 12 or not normalized_code.isalnum():
        return False, None

    # Hash the input code
    code_hash = hashlib.sha256(normalized_code.encode()).hexdigest()

    # Check against stored hashes
    if code_hash in stored_hashes:
        return True, code_hash

    return False, None


def remove_used_recovery_code(stored_hashes_json: str, used_hash: str) -> str:
    """
    Remove a used recovery code hash from the stored list.

    Args:
        stored_hashes_json: JSON array of hashed recovery codes
        used_hash: The hash to remove

    Returns:
        Updated JSON array string
    """
    try:
        stored_hashes = json.loads(stored_hashes_json)
        stored_hashes = [h for h in stored_hashes if h != used_hash]
        return json.dumps(stored_hashes)
    except Exception:
        return stored_hashes_json


def get_recovery_codes_count(stored_hashes_json: Optional[str]) -> int:
    """
    Get the number of remaining recovery codes.

    Args:
        stored_hashes_json: JSON array of hashed recovery codes

    Returns:
        Number of remaining codes
    """
    if not stored_hashes_json:
        return 0
    try:
        stored_hashes = json.loads(stored_hashes_json)
        return len(stored_hashes) if isinstance(stored_hashes, list) else 0
    except Exception:
        return 0
