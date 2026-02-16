"""SpotifyConnector — Spotify playback integration via the Web API."""

import asyncio
import base64
import logging
import time
from typing import Any, Dict, Optional

from app.core.connectors.base import (
    BaseConnector,
    ConnectorCapabilities,
    ConnectorStatus,
    IncomingMessage,
    OutgoingMessage,
    PermissionPrompt,
    IncomingMessageCallback,
    PermissionResponseCallback,
    chunk_text,
)

logger = logging.getLogger(__name__)

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False


class SpotifyConnector(BaseConnector):
    """
    Connector for Spotify using the Web API via httpx.

    Config keys:
        client_id (str): Spotify application client ID.
        client_secret (str): Spotify application client secret.
        refresh_token (str): OAuth2 refresh token for the user.
        poll_interval_seconds (str, optional): Seconds between playback
            polling checks. Defaults to "30".
    """

    MAX_MESSAGE_LENGTH = 4000
    CAPABILITIES = ConnectorCapabilities(is_messaging=False)

    def __init__(
        self,
        connector_id: str,
        connector_type: str,
        config: Dict[str, Any],
        on_incoming_message: IncomingMessageCallback,
        on_permission_response: PermissionResponseCallback,
    ):
        super().__init__(
            connector_id, connector_type, config,
            on_incoming_message, on_permission_response,
        )
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._http_client: Optional[Any] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._last_track_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _refresh_access_token(self) -> None:
        """Exchange the refresh token for a new access token."""
        client_id = self.config["client_id"]
        client_secret = self.config["client_secret"]
        refresh_token = self.config["refresh_token"]

        credentials = base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode()

        response = await self._http_client.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {credentials}"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        body = response.json()

        self._access_token = body["access_token"]
        self._token_expires_at = time.time() + body["expires_in"]
        logger.debug(
            "SpotifyConnector %s refreshed access token (expires in %ds)",
            self.connector_id, body["expires_in"],
        )

    async def _ensure_token(self) -> None:
        """Refresh the access token if it has expired or is about to."""
        if self._access_token is None or time.time() >= self._token_expires_at - 60:
            await self._refresh_access_token()

    async def _api_get(self, path: str) -> httpx.Response:
        """Make an authenticated GET request to the Spotify Web API."""
        await self._ensure_token()
        response = await self._http_client.get(
            f"https://api.spotify.com/v1{path}",
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        response.raise_for_status()
        return response

    async def _api_put(
        self, path: str, json: Optional[Dict[str, Any]] = None
    ) -> httpx.Response:
        """Make an authenticated PUT request to the Spotify Web API."""
        await self._ensure_token()
        response = await self._http_client.put(
            f"https://api.spotify.com/v1{path}",
            headers={"Authorization": f"Bearer {self._access_token}"},
            json=json,
        )
        response.raise_for_status()
        return response

    async def _api_post(
        self, path: str, json: Optional[Dict[str, Any]] = None
    ) -> httpx.Response:
        """Make an authenticated POST request to the Spotify Web API."""
        await self._ensure_token()
        response = await self._http_client.post(
            f"https://api.spotify.com/v1{path}",
            headers={"Authorization": f"Bearer {self._access_token}"},
            json=json,
        )
        response.raise_for_status()
        return response

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Create an HTTP client, authenticate, and start polling playback."""
        if not HTTPX_AVAILABLE:
            self._status = ConnectorStatus.ERROR
            self._status_message = "httpx is not installed"
            logger.error("Cannot start SpotifyConnector: httpx is not installed")
            return False

        try:
            self._status = ConnectorStatus.CONNECTING

            self._http_client = httpx.AsyncClient()
            await self._refresh_access_token()

            self._poll_task = asyncio.create_task(self._poll_playback())
            self._status = ConnectorStatus.CONNECTED
            self._status_message = None
            logger.info("SpotifyConnector %s connected", self.connector_id)
            return True

        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception("SpotifyConnector %s failed to connect", self.connector_id)
            return False

    async def disconnect(self) -> None:
        """Cancel the poll task and close the HTTP client."""
        try:
            if self._poll_task is not None and not self._poll_task.done():
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass
            if self._http_client is not None:
                await self._http_client.aclose()
                self._http_client = None
        except Exception as exc:
            logger.warning("Error during SpotifyConnector disconnect: %s", exc)
        finally:
            self._status = ConnectorStatus.DISCONNECTED
            self._status_message = None
            logger.info("SpotifyConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Playback polling
    # ------------------------------------------------------------------

    async def _poll_playback(self) -> None:
        """Periodically check playback state and emit on track change."""
        interval = int(self.config.get("poll_interval_seconds", "30"))
        consecutive_failures = 0
        max_consecutive_failures = 5
        try:
            while True:
                try:
                    response = await self._api_get("/me/player")
                    consecutive_failures = 0
                    if response.status_code == 200:
                        data = response.json()
                        item = data.get("item")
                        if item:
                            track_id = item.get("id")
                            if track_id and track_id != self._last_track_id:
                                self._last_track_id = track_id
                                track_name = item.get("name", "Unknown")
                                artists = ", ".join(
                                    a["name"] for a in item.get("artists", [])
                                )
                                album = item.get("album", {}).get(
                                    "name", "Unknown Album"
                                )
                                text = (
                                    f"Now playing: {track_name} by "
                                    f"{artists} ({album})"
                                )
                                msg = IncomingMessage(
                                    connector_id=self.connector_id,
                                    platform_chat_id="spotify",
                                    platform_user_id="spotify",
                                    platform_user_name="Spotify",
                                    text=text,
                                    platform_message_id=track_id,
                                )
                                await self._on_incoming_message(msg)
                except Exception as exc:
                    consecutive_failures += 1
                    logger.warning(
                        "SpotifyConnector %s poll error (%d/%d): %s",
                        self.connector_id, consecutive_failures, max_consecutive_failures, exc,
                    )
                    if consecutive_failures >= max_consecutive_failures:
                        self._status = ConnectorStatus.ERROR
                        self._status_message = f"Repeated poll failures: {exc}"
                        logger.error("SpotifyConnector %s entering ERROR state after %d consecutive failures", self.connector_id, consecutive_failures)
                        try:
                            from app.db import database
                            database.update_connector_status(self.connector_id, "error", error=str(exc))
                        except Exception:
                            logger.debug("Failed to update DB status", exc_info=True)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("SpotifyConnector %s polling cancelled", self.connector_id)

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Parse the message text as a playback command and execute it."""
        if self._http_client is None:
            logger.error("Cannot send message: HTTP client is not initialised")
            return False

        try:
            text = message.text.strip()
            lower = text.lower()

            # --- Simple playback controls ---
            if lower in ("play", "resume"):
                await self._api_put("/me/player/play")
                return True

            if lower == "pause":
                await self._api_put("/me/player/pause")
                return True

            if lower in ("next", "skip"):
                await self._api_post("/me/player/next")
                return True

            if lower in ("previous", "prev"):
                await self._api_post("/me/player/previous")
                return True

            # --- Volume ---
            if lower.startswith("volume "):
                level = lower.split("volume ", 1)[1].strip()
                volume_percent = max(0, min(100, int(level)))
                await self._api_put(
                    f"/me/player/volume?volume_percent={volume_percent}"
                )
                return True

            # --- Shuffle ---
            if lower.startswith("shuffle "):
                state_str = lower.split("shuffle ", 1)[1].strip()
                state = "true" if state_str == "on" else "false"
                await self._api_put(f"/me/player/shuffle?state={state}")
                return True

            # --- Play a specific query ---
            if lower.startswith("play "):
                query = text[5:].strip()
                search_resp = await self._api_get(
                    f"/search?q={query}&type=track&limit=1"
                )
                search_data = search_resp.json()
                tracks = search_data.get("tracks", {}).get("items", [])
                if not tracks:
                    await self._emit_response(
                        message.platform_chat_id,
                        f"No tracks found for: {query}",
                    )
                    return True
                track_uri = tracks[0]["uri"]
                await self._api_put("/me/player/play", json={"uris": [track_uri]})
                return True

            # --- Devices ---
            if lower == "devices":
                resp = await self._api_get("/me/player/devices")
                devices = resp.json().get("devices", [])
                if not devices:
                    await self._emit_response(
                        message.platform_chat_id, "No active devices found."
                    )
                    return True
                lines = ["Devices:"]
                for d in devices:
                    active = " (active)" if d.get("is_active") else ""
                    lines.append(
                        f"  - {d['name']} ({d['type']}){active}"
                    )
                await self._emit_response(
                    message.platform_chat_id, "\n".join(lines)
                )
                return True

            # --- Now playing ---
            if lower in ("now", "now playing"):
                resp = await self._api_get("/me/player/currently-playing")
                if resp.status_code == 204 or not resp.content:
                    await self._emit_response(
                        message.platform_chat_id, "Nothing is currently playing."
                    )
                    return True
                data = resp.json()
                item = data.get("item")
                if not item:
                    await self._emit_response(
                        message.platform_chat_id, "Nothing is currently playing."
                    )
                    return True
                track_name = item.get("name", "Unknown")
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                album = item.get("album", {}).get("name", "Unknown Album")
                progress_ms = data.get("progress_ms", 0)
                duration_ms = item.get("duration_ms", 0)
                progress_s = progress_ms // 1000
                duration_s = duration_ms // 1000
                info = (
                    f"Now playing: {track_name}\n"
                    f"Artist: {artists}\n"
                    f"Album: {album}\n"
                    f"Progress: {progress_s // 60}:{progress_s % 60:02d}"
                    f" / {duration_s // 60}:{duration_s % 60:02d}"
                )
                await self._emit_response(message.platform_chat_id, info)
                return True

            # --- Top tracks ---
            if lower == "top tracks":
                resp = await self._api_get("/me/top/tracks?limit=10")
                items = resp.json().get("items", [])
                if not items:
                    await self._emit_response(
                        message.platform_chat_id, "No top tracks found."
                    )
                    return True
                lines = ["Your Top Tracks:"]
                for i, t in enumerate(items, 1):
                    artists = ", ".join(a["name"] for a in t.get("artists", []))
                    lines.append(f"  {i}. {t['name']} - {artists}")
                await self._emit_response(
                    message.platform_chat_id, "\n".join(lines)
                )
                return True

            # --- Top artists ---
            if lower == "top artists":
                resp = await self._api_get("/me/top/artists?limit=10")
                items = resp.json().get("items", [])
                if not items:
                    await self._emit_response(
                        message.platform_chat_id, "No top artists found."
                    )
                    return True
                lines = ["Your Top Artists:"]
                for i, a in enumerate(items, 1):
                    genres = ", ".join(a.get("genres", [])[:3])
                    genre_info = f" ({genres})" if genres else ""
                    lines.append(f"  {i}. {a['name']}{genre_info}")
                await self._emit_response(
                    message.platform_chat_id, "\n".join(lines)
                )
                return True

            # --- Search ---
            if lower.startswith("search "):
                query = text[7:].strip()
                resp = await self._api_get(
                    f"/search?q={query}&type=track&limit=5"
                )
                tracks = resp.json().get("tracks", {}).get("items", [])
                if not tracks:
                    await self._emit_response(
                        message.platform_chat_id,
                        f"No results found for: {query}",
                    )
                    return True
                lines = [f"Search results for \"{query}\":"]
                for i, t in enumerate(tracks, 1):
                    artists = ", ".join(a["name"] for a in t.get("artists", []))
                    album = t.get("album", {}).get("name", "")
                    lines.append(f"  {i}. {t['name']} - {artists} ({album})")
                await self._emit_response(
                    message.platform_chat_id, "\n".join(lines)
                )
                return True

            # --- Unrecognized command ---
            return False

        except Exception as exc:
            logger.exception(
                "Failed to execute Spotify command '%s': %s",
                message.text, exc,
            )
            return False

    async def _emit_response(self, platform_chat_id: str, text: str) -> None:
        """Send a synthetic IncomingMessage back with the response text."""
        chunks = chunk_text(text, self.MAX_MESSAGE_LENGTH)
        for chunk in chunks:
            msg = IncomingMessage(
                connector_id=self.connector_id,
                platform_chat_id=platform_chat_id,
                platform_user_id="spotify",
                platform_user_name="Spotify",
                text=chunk,
            )
            await self._on_incoming_message(msg)

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Not supported for Spotify."""
        return False

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a notification as a synthetic IncomingMessage."""
        try:
            text = f"[{event_type}] {data}"
            await self._emit_response(platform_chat_id, text)
            return True
        except Exception as exc:
            logger.exception(
                "Failed to send Spotify notification: %s", exc,
            )
            return False

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        """Validate credentials by refreshing the token and calling /v1/me."""
        if not HTTPX_AVAILABLE:
            return {"success": False, "message": "httpx is not installed"}

        try:
            async with httpx.AsyncClient() as client:
                # Temporarily use this client for token refresh
                original_client = self._http_client
                self._http_client = client
                try:
                    await self._refresh_access_token()
                    response = await self._api_get("/me")
                    user_data = response.json()
                    display_name = user_data.get("display_name", "Unknown User")
                    return {
                        "success": True,
                        "message": f"Authenticated as {display_name}",
                    }
                finally:
                    self._http_client = original_client
        except Exception as exc:
            return {"success": False, "message": str(exc)}
