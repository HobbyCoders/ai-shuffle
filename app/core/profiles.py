"""
Agent profiles utilities
"""

from typing import Dict, Any, Optional
from app.db import database


def get_profile_or_none(profile_id: str) -> Optional[Dict[str, Any]]:
    """Get a profile from database"""
    return database.get_profile(profile_id)
