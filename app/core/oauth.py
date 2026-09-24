"""
Direct OAuth implementation for Claude authentication.

This implements the OAuth 2.0 + PKCE flow directly against Anthropic's endpoints,
bypassing the need for PTY-based CLI interaction. Based on the approach used by
OpenCode (github.com/sst/opencode).

The flow:
1. Generate PKCE code verifier and challenge
2. Build authorization URL for claude.ai/oauth/authorize
3. User visits URL, authenticates, gets authorization code
4. Exchange code for tokens at console.anthropic.com/v1/oauth/token
5. Write tokens to ~/.claude/.credentials.json in CLI-compatible format
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Dict, Any, Tuple
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# Optional module-level hook fired AFTER any successful credentials write.
# Used by the multi-account feature to sync a rotated credentials file back into
# the active account's DB row. oauth.py stays dependency-free: the hook is
# registered from startup (see app.core.startup) and is invoked defensively —
# it must NEVER be able to fail a credential write or token refresh.
_post_write_hook: Optional[Callable[[], None]] = None


def set_post_write_hook(fn: Optional[Callable[[], None]]) -> None:
    """Register (or clear with None) the post-credentials-write hook."""
    global _post_write_hook
    _post_write_hook = fn

# OAuth configuration - these match Claude Code CLI's OAuth setup
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES = ["user:inference", "user:profile", "org:create_api_key"]

# Profile endpoint — the ONLY place that reports the subscription tier. The
# usage endpoint (see app.core.usage_limits) carries no plan field at all, so
# the account's "max"/"pro" plan has to come from here.
OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
# The CLI sends this beta header on the OAuth-scoped API endpoints; mirrored
# here (see the same constant in app.core.usage_limits).
OAUTH_BETA_HEADER = "oauth-2025-04-20"


def derive_plan_from_profile(data: Dict[str, Any]) -> Optional[str]:
    """Extract the subscription tier from an OAuth ``/profile`` response.

    The payload looks like::

        {"account": {"has_claude_max": true, "has_claude_pro": false, ...},
         "organization": {"organization_type": "claude_max", ...}}

    Preference order: the explicit ``has_claude_max`` / ``has_claude_pro``
    booleans, then the organization type with its ``claude_`` prefix stripped
    (so ``claude_enterprise`` reads as ``enterprise``). Returns None when
    nothing usable is present. Fully defensive — the shape is not contractual.
    """
    if not isinstance(data, dict):
        return None

    account = data.get("account")
    if isinstance(account, dict):
        if account.get("has_claude_max"):
            return "max"
        if account.get("has_claude_pro"):
            return "pro"

    organization = data.get("organization")
    if isinstance(organization, dict):
        org_type = organization.get("organization_type")
        if isinstance(org_type, str) and org_type.strip():
            org_type = org_type.strip()
            if org_type.startswith("claude_"):
                org_type = org_type[len("claude_"):]
            return org_type or None

    return None


@dataclass
class PKCEChallenge:
    """PKCE code verifier and challenge pair"""
    verifier: str
    challenge: str
    method: str = "S256"


@dataclass
class OAuthTokens:
    """OAuth token response"""
    access_token: str
    refresh_token: str
    expires_at: int  # Unix timestamp in milliseconds
    scopes: list
    # Best-effort account email captured from the token-exchange response, when
    # Anthropic includes it. Used only to label a newly added account; never
    # required and never allowed to fail the exchange.
    account_email: Optional[str] = None


class DirectOAuth:
    """
    Direct OAuth client for Claude authentication.

    This bypasses the CLI and directly implements the OAuth flow,
    then writes credentials in a format the Claude CLI/SDK can read.
    """

    def __init__(self, config_dir: Optional[Path] = None):
        """
        Initialize the OAuth client.

        Args:
            config_dir: Path to ~/.claude directory. Auto-detected if not provided.
        """
        if config_dir:
            self.config_dir = config_dir
        else:
            home = os.environ.get('HOME') or os.environ.get('USERPROFILE') or str(Path.home())
            self.config_dir = Path(home) / '.claude'

        # Store active PKCE challenges (keyed by state)
        self._pending_challenges: Dict[str, PKCEChallenge] = {}

        # Cache for credential validity check (to avoid disk I/O on every health check)
        self._cred_cache_valid: Optional[bool] = None
        self._cred_cache_time: float = 0
        self._cred_cache_ttl: float = 60  # Cache for 60 seconds

        # Serialise auto-refresh so concurrent callers don't fire duplicate
        # token-refresh requests against the OAuth endpoint.
        self._refresh_lock = asyncio.Lock()

    @staticmethod
    def generate_pkce_challenge() -> PKCEChallenge:
        """
        Generate a PKCE code verifier and challenge.

        The verifier is a random string, and the challenge is its SHA256 hash
        encoded as base64url (without padding).
        """
        # Generate a random 32-byte verifier, encode as base64url
        verifier_bytes = secrets.token_bytes(32)
        verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b'=').decode('ascii')

        # Create SHA256 hash of verifier
        challenge_hash = hashlib.sha256(verifier.encode('ascii')).digest()
        challenge = base64.urlsafe_b64encode(challenge_hash).rstrip(b'=').decode('ascii')

        return PKCEChallenge(verifier=verifier, challenge=challenge)

    @staticmethod
    def generate_state() -> str:
        """Generate a random state parameter for CSRF protection"""
        return secrets.token_urlsafe(16)

    def start_oauth_flow(self) -> Dict[str, Any]:
        """
        Start the OAuth flow by generating the authorization URL.

        Returns:
            Dict with:
                - oauth_url: The URL the user should visit
                - state: The state parameter (needed to complete the flow)
        """
        # Generate PKCE challenge
        pkce = self.generate_pkce_challenge()

        # For PKCE, we use the verifier as the state (like OpenCode does)
        # This simplifies the flow - the verifier is needed for token exchange
        state = pkce.verifier

        # Store the challenge for later verification (keyed by verifier)
        self._pending_challenges[state] = pkce

        # Build authorization URL
        # Note: "code=true" tells Claude.ai to show the code directly on the page
        # instead of doing a redirect (which would fail without a real callback server)
        params = {
            "code": "true",  # IMPORTANT: Show code on page instead of redirect
            "client_id": OAUTH_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": " ".join(OAUTH_SCOPES),
            "code_challenge": pkce.challenge,
            "code_challenge_method": pkce.method,
            "state": state,
        }

        oauth_url = f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

        logger.debug(f"Generated OAuth URL with state: {state[:8]}...")

        return {
            "oauth_url": oauth_url,
            "state": state,
        }

    async def exchange_code_for_tokens(
        self,
        code: str,
        state: str
    ) -> Tuple[Optional[OAuthTokens], Optional[str]]:
        """
        Exchange an authorization code for access/refresh tokens.

        Args:
            code: The authorization code from the OAuth callback (may contain #state suffix)
            state: The state parameter from the original request (this is the PKCE verifier)

        Returns:
            Tuple of (tokens, error_message)
        """
        # Retrieve and validate the PKCE challenge
        pkce = self._pending_challenges.pop(state, None)
        if not pkce:
            return None, "Invalid or expired state parameter. Please start the login process again."

        # The code from the OAuth callback might have #state appended
        # Split it to get just the code part (like OpenCode does)
        code_parts = code.split("#")
        actual_code = code_parts[0]
        code_state = code_parts[1] if len(code_parts) > 1 else None

        logger.debug(f"Code parts: code={actual_code[:20]}..., state_from_code={code_state}")

        # Prepare token exchange request
        payload = {
            "grant_type": "authorization_code",
            "code": actual_code,
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "code_verifier": pkce.verifier,
        }

        # Include state from code if present (some OAuth flows include it)
        if code_state:
            payload["state"] = code_state

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    OAUTH_TOKEN_URL,
                    json=payload,
                    headers=headers,
                )

                if response.status_code != 200:
                    error_text = response.text
                    logger.error(f"Token exchange failed: {response.status_code} - {error_text}")

                    # Try to extract error message from JSON response
                    try:
                        error_json = response.json()
                        error_msg = error_json.get("error_description") or error_json.get("error") or error_text
                    except (ValueError, KeyError):
                        error_msg = error_text

                    return None, f"Token exchange failed: {error_msg}"

                data = response.json()

                # Calculate expiration timestamp (in milliseconds, like the CLI does)
                expires_in = data.get("expires_in", 3600)  # Default 1 hour
                expires_at = int((time.time() + expires_in) * 1000)

                # Best-effort capture of the account email — never fail the
                # exchange if the shape isn't what we expect.
                account_email: Optional[str] = None
                try:
                    account_email = (data.get("account") or {}).get("email_address")
                except Exception:  # noqa: BLE001
                    account_email = None

                tokens = OAuthTokens(
                    access_token=data["access_token"],
                    refresh_token=data["refresh_token"],
                    expires_at=expires_at,
                    scopes=data.get("scope", "").split() or OAUTH_SCOPES,
                    account_email=account_email,
                )

                logger.debug("Successfully exchanged code for tokens")
                return tokens, None

        except httpx.TimeoutException:
            return None, "Token exchange timed out. Please try again."
        except httpx.RequestError as e:
            logger.error(f"Token exchange request error: {e}")
            return None, f"Network error during token exchange: {str(e)}"
        except Exception as e:
            logger.error(f"Token exchange error: {e}", exc_info=True)
            return None, f"Unexpected error: {str(e)}"

    def write_credentials_dict(self, oauth_dict: dict) -> bool:
        """
        Write a full ``claudeAiOauth`` object to ~/.claude/.credentials.json
        VERBATIM (no field synthesis), in CLI-compatible format.

        This is the low-level writer used by both ``write_credentials`` and the
        multi-account switch path. Writing the object verbatim matters for
        account switching: it must never degrade a stored credential (e.g. by
        clobbering a known ``subscriptionType`` back to None).

        Args:
            oauth_dict: The object to store under the ``claudeAiOauth`` key.

        Returns:
            True if successful, False otherwise
        """
        try:
            # Ensure config directory exists
            self.config_dir.mkdir(parents=True, exist_ok=True)

            credentials_file = self.config_dir / ".credentials.json"

            credentials = {"claudeAiOauth": oauth_dict}

            # Write with restricted permissions (like the CLI does)
            credentials_file.write_text(json.dumps(credentials))

            # Set file permissions to 600 (owner read/write only) on Unix
            if os.name != 'nt':  # Not Windows
                os.chmod(credentials_file, 0o600)

            # Login, refresh and account switches replace the token on disk.
            # Drop the previous validity result before a hook or status request
            # can read it, including a cached False from before the login.
            self.invalidate_credentials_cache()
            logger.debug(f"Wrote credentials to {credentials_file}")

            # Fire the optional post-write hook (multi-account sync-back). This
            # must NEVER be able to fail the write — it runs downstream of token
            # refresh, so a raising hook could otherwise break auth entirely.
            if _post_write_hook is not None:
                try:
                    _post_write_hook()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Post-write credentials hook failed (ignored): {e}")

            return True

        except Exception as e:
            logger.error(f"Failed to write credentials: {e}", exc_info=True)
            return False

    def write_credentials(self, tokens: OAuthTokens) -> bool:
        """
        Write tokens to ~/.claude/.credentials.json in CLI-compatible format.

        Args:
            tokens: The OAuth tokens to store

        Returns:
            True if successful, False otherwise
        """
        # Format matches what Claude CLI expects. subscriptionType is left None
        # here because a freshly exchanged/refreshed token doesn't tell us the
        # plan; the switch path preserves whatever was stored (see
        # write_credentials_dict).
        return self.write_credentials_dict({
            "accessToken": tokens.access_token,
            "refreshToken": tokens.refresh_token,
            "expiresAt": tokens.expires_at,
            "scopes": tokens.scopes,
            "subscriptionType": None,
        })

    def ensure_onboarding_complete(self) -> bool:
        """
        Mark onboarding as complete in settings.json to prevent CLI wizard.

        Returns:
            True if successful, False otherwise
        """
        try:
            settings_file = self.config_dir / "settings.json"

            # Read existing settings or start fresh
            if settings_file.exists():
                try:
                    settings_data = json.loads(settings_file.read_text())
                except json.JSONDecodeError:
                    settings_data = {}
            else:
                settings_data = {}

            # Set required flags
            settings_data["hasCompletedOnboarding"] = True
            if "theme" not in settings_data:
                settings_data["theme"] = "dark"

            # Write back
            settings_file.write_text(json.dumps(settings_data, indent=2))

            logger.debug("Marked onboarding as complete")
            return True

        except Exception as e:
            logger.warning(f"Could not update settings.json: {e}")
            return False

    async def complete_oauth_flow(
        self,
        code: str,
        state: str
    ) -> Dict[str, Any]:
        """
        Complete the OAuth flow: exchange code, write credentials, setup CLI.

        Args:
            code: The authorization code from the OAuth callback
            state: The state parameter from the original request

        Returns:
            Dict with success status and message
        """
        # Exchange code for tokens
        tokens, error = await self.exchange_code_for_tokens(code, state)

        if error:
            return {
                "success": False,
                "message": "Failed to exchange authorization code",
                "error": error,
            }

        # Write credentials file
        if not self.write_credentials(tokens):
            return {
                "success": False,
                "message": "Failed to save credentials",
                "error": "Could not write credentials file",
            }

        # Mark onboarding complete
        self.ensure_onboarding_complete()

        return {
            "success": True,
            "message": "Successfully authenticated with Claude Code",
            "authenticated": True,
        }

    def has_valid_credentials(self) -> bool:
        """Check if valid credentials exist (not expired)"""
        # Return cached result if still valid
        now = time.time()
        if self._cred_cache_valid is not None and (now - self._cred_cache_time) < self._cred_cache_ttl:
            return self._cred_cache_valid

        credentials_file = self.config_dir / ".credentials.json"

        if not credentials_file.exists():
            self._cred_cache_valid = False
            self._cred_cache_time = now
            return False

        try:
            data = json.loads(credentials_file.read_text())
            oauth_data = data.get("claudeAiOauth", {})

            if not oauth_data.get("accessToken"):
                self._cred_cache_valid = False
                self._cred_cache_time = now
                return False

            # Check expiration (with 5 minute buffer)
            expires_at = oauth_data.get("expiresAt", 0)
            now_ms = int(time.time() * 1000)

            if expires_at and expires_at < (now_ms + 300000):  # 5 min buffer
                logger.debug("Credentials exist but are expired or expiring soon")
                self._cred_cache_valid = False
                self._cred_cache_time = now
                return False

            self._cred_cache_valid = True
            self._cred_cache_time = now
            return True

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Invalid credentials file: {e}")
            self._cred_cache_valid = False
            self._cred_cache_time = now
            return False

    def invalidate_credentials_cache(self):
        """Invalidate the credentials cache (call after login/logout)"""
        self._cred_cache_valid = None
        self._cred_cache_time = 0

    async def refresh_tokens(self) -> Tuple[Optional[OAuthTokens], Optional[str]]:
        """
        Refresh the access token using the refresh token.

        Returns:
            Tuple of (new_tokens, error_message)
        """
        credentials_file = self.config_dir / ".credentials.json"

        try:
            data = json.loads(credentials_file.read_text())
            refresh_token = data.get("claudeAiOauth", {}).get("refreshToken")

            if not refresh_token:
                return None, "No refresh token found"

        except Exception as e:
            return None, f"Could not read credentials: {e}"

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    OAUTH_TOKEN_URL,
                    json=payload,
                    headers=headers,
                )

                if response.status_code != 200:
                    error_text = response.text
                    logger.error(f"Token refresh failed: {response.status_code} - {error_text}")
                    return None, f"Token refresh failed: {error_text}"

                data = response.json()

                expires_in = data.get("expires_in", 3600)
                expires_at = int((time.time() + expires_in) * 1000)

                tokens = OAuthTokens(
                    access_token=data["access_token"],
                    refresh_token=data.get("refresh_token", refresh_token),  # May or may not get new one
                    expires_at=expires_at,
                    scopes=data.get("scope", "").split() or OAUTH_SCOPES,
                )

                # Write updated credentials
                self.write_credentials(tokens)

                logger.debug("Successfully refreshed tokens")
                return tokens, None

        except Exception as e:
            logger.error(f"Token refresh error: {e}", exc_info=True)
            return None, f"Refresh error: {str(e)}"

    def refresh_oauth_dict(
        self, oauth_dict: Dict[str, Any]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Refresh a credential object using its OWN ``refreshToken``, WITHOUT
        touching the on-disk credentials file.

        This is the per-account counterpart of :meth:`refresh_tokens` (which is
        hardwired to the single on-disk active credential). Multi-account
        switching needs to renew a *stored, inactive* account's short-lived
        access token before activating it — reading/writing the shared file for
        that would clobber the currently active account. So this operates purely
        on the passed dict and returns a NEW dict; the caller persists it.

        Synchronous (uses ``httpx.Client``) so it can run inside the sync
        ``switch_account`` path (also invoked off the event loop via
        ``run_sync`` from auto-failover). Non-token fields present on the input
        (e.g. ``subscriptionType``) are preserved.

        Returns ``(new_oauth_dict, None)`` on success, or ``(None, error_code)``:
        ``"no_refresh_token"`` when the credential can't be renewed, otherwise a
        ``"refresh_failed: ..."`` / ``"refresh_error: ..."`` string. Never raises.
        """
        refresh_token = (oauth_dict or {}).get("refreshToken")
        if not refresh_token:
            return None, "no_refresh_token"

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(OAUTH_TOKEN_URL, json=payload, headers=headers)

            if response.status_code != 200:
                error_text = response.text
                logger.error(
                    f"Per-account token refresh failed: {response.status_code} - {error_text}"
                )
                return None, f"refresh_failed: {error_text}"

            data = response.json()
            expires_in = data.get("expires_in", 3600)
            expires_at = int((time.time() + expires_in) * 1000)

            new_dict: Dict[str, Any] = dict(oauth_dict or {})
            new_dict["accessToken"] = data["access_token"]
            # The endpoint may or may not rotate the refresh token.
            new_dict["refreshToken"] = data.get("refresh_token", refresh_token)
            new_dict["expiresAt"] = expires_at
            scopes = data.get("scope", "").split()
            if scopes:
                new_dict["scopes"] = scopes
            return new_dict, None

        except Exception as e:  # noqa: BLE001
            logger.error(f"Per-account token refresh error: {e}", exc_info=True)
            return None, f"refresh_error: {e}"

    def _read_oauth_data(self) -> Optional[Dict[str, Any]]:
        """Read the ``claudeAiOauth`` block from the credentials file, or None."""
        credentials_file = self.config_dir / ".credentials.json"
        if not credentials_file.exists():
            return None
        try:
            data = json.loads(credentials_file.read_text())
            return data.get("claudeAiOauth", {}) or {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read credentials for refresh check: {e}")
            return None

    def get_access_token(self) -> Optional[str]:
        """Return the current OAuth access token, or None if not logged in.

        Reads straight from the on-disk credential (no expiry check). Callers
        that need a *valid* token should await ``ensure_fresh_credentials()``
        first. NEVER log or expose the returned value.
        """
        oauth_data = self._read_oauth_data() or {}
        return oauth_data.get("accessToken") or None

    async def fetch_subscription_plan(self, token: str) -> Optional[str]:
        """Fetch the account's subscription tier from the OAuth profile endpoint.

        Returns ``"max"`` / ``"pro"`` / an organization-derived string, or None
        when the endpoint is unreachable, answers non-200, or reports nothing
        usable. Never raises — the caller treats None as "unknown", not as an
        error. Security: the token is only ever sent as a Bearer header and is
        NEVER logged.
        """
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": OAUTH_BETA_HEADER,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(OAUTH_PROFILE_URL, headers=headers)
            if resp.status_code != 200:
                logger.debug(
                    "OAuth profile fetch failed: HTTP %s (plan unknown)",
                    resp.status_code,
                )
                return None
            return derive_plan_from_profile(resp.json())
        except Exception as e:  # noqa: BLE001 — plan is advisory, never fatal
            logger.debug(f"OAuth profile fetch error (plan unknown): {e}")
            return None

    async def ensure_fresh_credentials(self, buffer_seconds: int = 600) -> bool:
        """Proactively refresh the access token when it is at/near expiry.

        The access token Anthropic issues is short-lived (well under 24h); the
        refresh token is long-lived. Without this, an idle access token simply
        expires and every request 401s ("Invalid authentication credentials")
        even though the stored refresh token could silently renew it. Call this
        before spawning/resuming an SDK subprocess (and on auth-status checks)
        so the on-disk credential stays valid.

        Returns True only if a refresh was actually performed (credentials
        rewritten). Returns False when no refresh is needed or possible — token
        still valid, no refresh token, or no OAuth credentials (e.g. API-key
        mode). Never raises; failures are logged and reported as False.
        """
        oauth_data = self._read_oauth_data()
        if not oauth_data or not oauth_data.get("accessToken"):
            return False  # No OAuth login (e.g. API-key mode) — nothing to refresh.
        if not oauth_data.get("refreshToken"):
            return False  # Can't renew without a refresh token — needs manual reauth.

        now_ms = int(time.time() * 1000)
        expires_at = oauth_data.get("expiresAt", 0) or 0
        if expires_at and expires_at > (now_ms + buffer_seconds * 1000):
            return False  # Still comfortably valid — no-op, no network call.

        async with self._refresh_lock:
            # Re-check under the lock: a concurrent caller may have just refreshed.
            oauth_data = self._read_oauth_data() or {}
            if not oauth_data.get("refreshToken"):
                return False
            now_ms = int(time.time() * 1000)
            expires_at = oauth_data.get("expiresAt", 0) or 0
            if expires_at and expires_at > (now_ms + buffer_seconds * 1000):
                return False

            tokens, error = await self.refresh_tokens()
            if error or not tokens:
                logger.warning(f"OAuth token auto-refresh failed: {error}")
                return False

            # refresh_tokens() already wrote the new credentials; drop the
            # validity cache so the next status check sees the fresh token.
            self.invalidate_credentials_cache()
            logger.info("Auto-refreshed Claude OAuth access token before expiry")
            return True

    def clear_pending_challenges(self):
        """Clear all pending PKCE challenges (e.g., on logout)"""
        self._pending_challenges.clear()

    def get_pending_states(self) -> list:
        """Get list of pending state parameters (for debugging)"""
        return list(self._pending_challenges.keys())


# Global instance
direct_oauth = DirectOAuth()
