"""
Settings crypto utilities - API key encryption, decryption, and masking.
"""

import logging
from typing import Optional

from app.db import database
from app.core import encryption

logger = logging.getLogger(__name__)


def get_decrypted_api_key(setting_name: str) -> Optional[str]:
    """
    Get an API key from the database and decrypt it if encrypted.
    Returns None if not set or if decryption fails.
    """
    value = database.get_system_setting(setting_name)
    if not value:
        return None

    # If encrypted, try to decrypt
    if encryption.is_encrypted(value):
        if not encryption.is_encryption_ready():
            logger.warning(f"Cannot decrypt {setting_name} - encryption key not loaded")
            return None
        try:
            return encryption.decrypt_value(value)
        except Exception as e:
            logger.error(f"Failed to decrypt {setting_name}: {e}")
            return None
    else:
        # Plaintext (legacy or migration pending)
        return value


def set_encrypted_api_key(setting_name: str, api_key: str) -> None:
    """
    Encrypt and store an API key in the database.
    Locked or unavailable server storage must never downgrade to plaintext.
    """
    encrypted = encryption.encrypt_value(api_key)
    database.set_system_setting(setting_name, encrypted)


def mask_api_key(key: str) -> str:
    """Mask an API key for display, showing first 3 and last 4 characters"""
    if not key or len(key) < 10:
        return ""
    return f"{key[:7]}...{key[-4:]}"
