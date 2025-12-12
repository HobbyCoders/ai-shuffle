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

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# OAuth configuration - these match Claude Code CLI's OAuth setup
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES = ["user:inference", "user:profile", "org:create_api_key"]


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

        logger.info(f"Generated OAuth URL with state: {state[:8]}...")

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
                    except:
                        error_msg = error_text

                    return None, f"Token exchange failed: {error_msg}"

                data = response.json()

                # Calculate expiration timestamp (in milliseconds, like the CLI does)
                expires_in = data.get("expires_in", 3600)  # Default 1 hour
                expires_at = int((time.time() + expires_in) * 1000)

                tokens = OAuthTokens(
                    access_token=data["access_token"],
                    refresh_token=data["refresh_token"],
                    expires_at=expires_at,
                    scopes=data.get("scope", "").split() or OAUTH_SCOPES,
                )

                logger.info("Successfully exchanged code for tokens")
                return tokens, None

        except httpx.TimeoutException:
            return None, "Token exchange timed out. Please try again."
        except httpx.RequestError as e:
            logger.error(f"Token exchange request error: {e}")
            return None, f"Network error during token exchange: {str(e)}"
        except Exception as e:
            logger.error(f"Token exchange error: {e}", exc_info=True)
            return None, f"Unexpected error: {str(e)}"

    def write_credentials(self, tokens: OAuthTokens) -> bool:
        """
        Write tokens to ~/.claude/.credentials.json in CLI-compatible format.

        Args:
            tokens: The OAuth tokens to store

        Returns:
            True if successful, False otherwise
        """
        try:
            # Ensure config directory exists
            self.config_dir.mkdir(parents=True, exist_ok=True)

            credentials_file = self.config_dir / ".credentials.json"

            # Format matches what Claude CLI expects
            credentials = {
                "claudeAiOauth": {
                    "accessToken": tokens.access_token,
                    "refreshToken": tokens.refresh_token,
                    "expiresAt": tokens.expires_at,
                    "scopes": tokens.scopes,
                    "subscriptionType": None,  # CLI sets this, we don't know it
                }
            }

            # Write with restricted permissions (like the CLI does)
            credentials_file.write_text(json.dumps(credentials))

            # Set file permissions to 600 (owner read/write only) on Unix
            if os.name != 'nt':  # Not Windows
                os.chmod(credentials_file, 0o600)

            logger.info(f"Wrote credentials to {credentials_file}")
            return True

        except Exception as e:
            logger.error(f"Failed to write credentials: {e}", exc_info=True)
            return False

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

            logger.info("Marked onboarding as complete")
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
        credentials_file = self.config_dir / ".credentials.json"

        if not credentials_file.exists():
            return False

        try:
            data = json.loads(credentials_file.read_text())
            oauth_data = data.get("claudeAiOauth", {})

            if not oauth_data.get("accessToken"):
                return False

            # Check expiration (with 5 minute buffer)
            expires_at = oauth_data.get("expiresAt", 0)
            now_ms = int(time.time() * 1000)

            if expires_at and expires_at < (now_ms + 300000):  # 5 min buffer
                logger.info("Credentials exist but are expired or expiring soon")
                return False

            return True

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Invalid credentials file: {e}")
            return False

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

                logger.info("Successfully refreshed tokens")
                return tokens, None

        except Exception as e:
            logger.error(f"Token refresh error: {e}", exc_info=True)
            return None, f"Refresh error: {str(e)}"

    def clear_pending_challenges(self):
        """Clear all pending PKCE challenges (e.g., on logout)"""
        self._pending_challenges.clear()

    def get_pending_states(self) -> list:
        """Get list of pending state parameters (for debugging)"""
        return list(self._pending_challenges.keys())


# Global instance
direct_oauth = DirectOAuth()
