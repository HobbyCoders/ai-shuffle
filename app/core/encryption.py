"""
Encryption utilities for securing sensitive data at rest.

Uses Fernet symmetric encryption with keys derived from the admin password.
The encryption key is never stored - it's derived at runtime from the password.

Alternatively, set ADMIN_PASSWORD env var to auto-derive the key on startup.
"""

import base64
import hashlib
import os
import secrets
import logging
from typing import Optional, Tuple
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Global cache for the encryption key (only in memory, never persisted)
_encryption_key: Optional[bytes] = None
_key_salt: Optional[bytes] = None


def init_encryption_from_env(db_module) -> bool:
    """
    Initialize encryption key from ADMIN_PASSWORD environment variable.
    This allows headless operation without requiring admin login.

    Call this at app startup to enable API key decryption.

    Returns:
        True if encryption was initialized, False if env var not set
    """
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if not admin_password:
        return False

    set_encryption_key(admin_password, db_module)
    logger.info("Encryption key initialized from ADMIN_PASSWORD environment variable")
    return True


def get_or_create_salt(db_module) -> bytes:
    """
    Get the salt from database or create a new one.
    The salt is stored in the database but is useless without the password.
    """
    global _key_salt

    if _key_salt:
        return _key_salt

    salt_b64 = db_module.get_system_setting("encryption_salt")
    if salt_b64:
        _key_salt = base64.b64decode(salt_b64)
    else:
        # Generate new salt (only happens once, on first encryption setup)
        _key_salt = secrets.token_bytes(32)
        db_module.set_system_setting("encryption_salt", base64.b64encode(_key_salt).decode())
        logger.info("Generated new encryption salt")

    return _key_salt


def derive_key_from_password(password: str, salt: bytes) -> bytes:
    """
    Derive a Fernet-compatible encryption key from a password using PBKDF2.

    Args:
        password: The admin password
        salt: Random salt (stored in DB)

    Returns:
        32-byte key suitable for Fernet encryption
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600000,  # OWASP recommended minimum for PBKDF2-SHA256
    )
    key = kdf.derive(password.encode())
    # Fernet requires base64-encoded 32-byte key
    return base64.urlsafe_b64encode(key)


def set_encryption_key(password: str, db_module) -> None:
    """
    Set the encryption key in memory by deriving it from the admin password.
    Called when admin logs in successfully.
    """
    global _encryption_key
    salt = get_or_create_salt(db_module)
    _encryption_key = derive_key_from_password(password, salt)
    logger.info("Encryption key loaded into memory")


def clear_encryption_key() -> None:
    """Clear the encryption key from memory (e.g., on logout or shutdown)."""
    global _encryption_key
    _encryption_key = None
    logger.info("Encryption key cleared from memory")


def is_encryption_ready() -> bool:
    """Check if encryption key is available in memory."""
    return _encryption_key is not None


def encrypt_value(plaintext: str) -> str:
    """
    Encrypt a string value.

    Args:
        plaintext: The value to encrypt

    Returns:
        Base64-encoded encrypted value with 'enc:' prefix

    Raises:
        RuntimeError: If encryption key is not loaded
    """
    if not _encryption_key:
        raise RuntimeError("Encryption key not loaded. Admin must be logged in.")

    f = Fernet(_encryption_key)
    encrypted = f.encrypt(plaintext.encode())
    # Prefix with 'enc:' to identify encrypted values
    return f"enc:{base64.b64encode(encrypted).decode()}"


def decrypt_value(encrypted: str) -> str:
    """
    Decrypt a string value.

    Args:
        encrypted: The encrypted value (with 'enc:' prefix)

    Returns:
        Decrypted plaintext

    Raises:
        RuntimeError: If encryption key is not loaded
        ValueError: If decryption fails (wrong key or corrupted data)
    """
    if not _encryption_key:
        raise RuntimeError("Encryption key not loaded. Admin must be logged in.")

    if not encrypted.startswith("enc:"):
        # Not encrypted, return as-is (for migration compatibility)
        return encrypted

    try:
        encrypted_bytes = base64.b64decode(encrypted[4:])  # Remove 'enc:' prefix
        f = Fernet(_encryption_key)
        decrypted = f.decrypt(encrypted_bytes)
        return decrypted.decode()
    except InvalidToken:
        raise ValueError("Decryption failed - invalid key or corrupted data")


def is_encrypted(value: str) -> bool:
    """Check if a value is encrypted (has 'enc:' prefix)."""
    return value.startswith("enc:") if value else False


def re_encrypt_all_secrets(old_password: str, new_password: str, db_module) -> bool:
    """
    Re-encrypt all secrets with a new password.
    Called when admin changes their password.

    Args:
        old_password: The current admin password
        new_password: The new admin password
        db_module: Database module for reading/writing settings

    Returns:
        True if successful, False otherwise
    """
    global _encryption_key, _key_salt

    salt = get_or_create_salt(db_module)
    old_key = derive_key_from_password(old_password, salt)

    # List of settings that contain encrypted API keys
    sensitive_settings = [
        "openai_api_key",
        "gemini_api_key",
        "image_api_key",
        "video_api_key",
    ]

    # Collect all values to re-encrypt
    decrypted_values = {}

    for setting_name in sensitive_settings:
        value = db_module.get_system_setting(setting_name)
        if value:
            if is_encrypted(value):
                # Decrypt with old key
                try:
                    old_fernet = Fernet(old_key)
                    encrypted_bytes = base64.b64decode(value[4:])
                    decrypted = old_fernet.decrypt(encrypted_bytes).decode()
                    decrypted_values[setting_name] = decrypted
                except Exception as e:
                    logger.error(f"Failed to decrypt {setting_name}: {e}")
                    return False
            else:
                # Plaintext value (migration case)
                decrypted_values[setting_name] = value

    # Generate new key from new password (keep same salt for simplicity)
    new_key = derive_key_from_password(new_password, salt)
    new_fernet = Fernet(new_key)

    # Re-encrypt all values with new key
    for setting_name, plaintext in decrypted_values.items():
        encrypted = new_fernet.encrypt(plaintext.encode())
        encrypted_value = f"enc:{base64.b64encode(encrypted).decode()}"
        db_module.set_system_setting(setting_name, encrypted_value)
        logger.info(f"Re-encrypted {setting_name}")

    # Update in-memory key
    _encryption_key = new_key

    logger.info("Successfully re-encrypted all secrets with new password")
    return True


def encrypt_existing_plaintext_keys(db_module) -> int:
    """
    Encrypt any existing plaintext API keys in the database.
    Called during migration or when encryption is first enabled.

    Returns:
        Number of keys encrypted
    """
    if not _encryption_key:
        raise RuntimeError("Encryption key not loaded. Admin must be logged in.")

    sensitive_settings = [
        "openai_api_key",
        "gemini_api_key",
        "image_api_key",
        "video_api_key",
    ]

    count = 0
    for setting_name in sensitive_settings:
        value = db_module.get_system_setting(setting_name)
        if value and not is_encrypted(value):
            # This is a plaintext key, encrypt it
            encrypted = encrypt_value(value)
            db_module.set_system_setting(setting_name, encrypted)
            logger.info(f"Encrypted existing plaintext key: {setting_name}")
            count += 1

    return count
