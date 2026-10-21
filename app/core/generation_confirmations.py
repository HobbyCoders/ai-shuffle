"""One-use confirmation receipts that survive the next turn's tool-server rebuild."""
import json
import re
import secrets
import time

from app.db.async_database import run_sync
from app.db.base import get_db

PREFIX = "network_generation_confirmation:"
TTL = 15 * 60


class Confirmations:
    async def issue(self, fingerprint):
        token = secrets.token_urlsafe(24)
        await run_sync(self._issue, token, fingerprint)
        return token

    @staticmethod
    def _issue(token, fingerprint):
        with get_db() as conn:
            conn.cursor().execute("INSERT INTO system_settings (key,value) VALUES (%s,%s)",
                                  (PREFIX + token, json.dumps({"fingerprint": fingerprint, "expires": time.time() + TTL})))

    async def consume(self, token, fingerprint):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
            return False
        return await run_sync(self._consume, token, fingerprint)

    @staticmethod
    def _consume(token, fingerprint):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM system_settings WHERE key=%s RETURNING value", (PREFIX + token,))
            row = cursor.fetchone()
        if not row:
            return False
        data = json.loads(row["value"])
        return data["expires"] > time.time() and secrets.compare_digest(data["fingerprint"], fingerprint)
