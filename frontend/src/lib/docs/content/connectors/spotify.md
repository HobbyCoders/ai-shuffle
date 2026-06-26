<!--
title: Spotify
description: Control Spotify playback through Claude
order: 12
-->

# Spotify Connector

## Overview

The Spotify connector allows Claude to control music playback on your Spotify account through AI Shuffle. With this connector active, you can ask Claude to play songs, albums, or playlists, skip tracks, adjust volume, search for music, and manage your playback queue -- all through natural language conversation.

The connector uses the [Spotify Web API](https://developer.spotify.com/documentation/web-api) with OAuth 2.0. You will need to create a Spotify Developer application to obtain client credentials and a long-lived **refresh token**, which the connector uses to mint short-lived access tokens automatically. Note that some playback control features require a Spotify Premium subscription.

## Prerequisites

- A **Spotify account** (Premium recommended for full playback control)
- A **Spotify Developer account** at [developer.spotify.com](https://developer.spotify.com/)
- An active **Spotify playback device** (desktop app, mobile app, smart speaker, or web player) -- the API controls playback on an already-active device; it cannot start playback from scratch on an inactive device
- Network access from AI Shuffle to Spotify's API servers (`api.spotify.com`)

## Setup Guide

### 1. Create a Spotify Developer account

1. Go to [https://developer.spotify.com/](https://developer.spotify.com/)
2. Click **Log In** and sign in with your Spotify account
3. Accept the Developer Terms of Service if prompted

### 2. Create a Spotify application

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Click **Create App**
3. Fill in the application details:
   - **App name:** `AI Shuffle` (or any name you prefer)
   - **App description:** `AI Shuffle Spotify Connector`
   - **Website:** Can be left blank or set to your AI Shuffle URL
   - **Redirect URI:** Set any redirect URI you control (e.g. `http://localhost:8888/callback`). AI Shuffle does not expose a Spotify callback endpoint -- this URI is only used when you generate the refresh token out-of-band (see step 5), and it must match exactly during that step.
   - **APIs used:** Check **Web API**
4. Click **Save**

### 3. Get your client credentials

1. In the Spotify Developer Dashboard, click on your newly created app
2. Click **Settings**
3. Note down the following values:
   - **Client ID** -- A string like `1a2b3c4d5e6f7g8h9i0j`
   - **Client Secret** -- Click "View client secret" to reveal it. A string like `AbCdEfGhIjKlMnOpQrSt`

Keep these credentials secure. Do not share them or commit them to version control.

### 4. Obtain a refresh token

AI Shuffle does **not** host an OAuth callback for Spotify. Instead, the connector uses a pre-obtained **refresh token** to mint short-lived access tokens on demand. You generate this refresh token once, out-of-band, and paste it into the connector config.

Run the standard Spotify [Authorization Code flow](https://developer.spotify.com/documentation/web-api/tutorials/code-flow) once to get a refresh token. You can use Spotify's own tutorial, a small local script, or a community helper. At a high level:

1. Send the user to `https://accounts.spotify.com/authorize` with your `client_id`, your registered redirect URI, and the scopes you need (for full playback and library control: `user-read-playback-state`, `user-modify-playback-state`, `user-read-currently-playing`, `user-top-read`, `user-library-read`, `user-library-modify`).
2. After you click **Agree**, Spotify redirects to your redirect URI with a `?code=...` query parameter.
3. Exchange that authorization code at `https://accounts.spotify.com/api/token` (`grant_type=authorization_code`) using your `client_id` and `client_secret`.
4. The token response includes a `refresh_token`. Save this value -- it is what AI Shuffle needs.

The refresh token is long-lived and does not expire under normal use. The connector exchanges it for fresh access tokens automatically.

### 5. Create the Spotify connector in AI Shuffle

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Spotify",
    "connector_type": "spotify",
    "config": {
      "client_id": "1a2b3c4d5e6f7g8h9i0j",
      "client_secret": "AbCdEfGhIjKlMnOpQrSt",
      "refresh_token": "AQD...your_refresh_token"
    }
  }'
```

Or use the AI Shuffle UI: **Settings > Connectors > Add Connector > Spotify**.

### 6. Activate the connector

```bash
curl -X POST http://localhost:8000/api/v1/connectors/{connector_id}/activate \
  -H "Authorization: Bearer aih_your_key"
```

On activation the connector immediately exchanges your refresh token for an access token and begins polling playback.

### 7. Test playback control

Make sure Spotify is actively playing on one of your devices (open the Spotify app and start a song), then try:

- "What song is currently playing?"
- "Skip to the next track"
- "Play 'Bohemian Rhapsody' by Queen"
- "Set volume to 60%"
- "Add this song to my Liked Songs"

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `client_id` | string | Yes | The Client ID from your Spotify Developer application |
| `client_secret` | string | Yes | The Client Secret from your Spotify Developer application |
| `refresh_token` | string | Yes | OAuth2 refresh token obtained from the authorization code flow (see step 4) |
| `poll_interval_seconds` | string | No | Seconds between playback polling checks. Defaults to `"30"` |

## Capabilities

The Spotify connector supports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `rich_text` | No | Spotify is a non-messaging connector; responses are plain text |
| `typing_indicator` | No | Not applicable for music control |
| `reactions` | No | Not applicable for music control |
| `file_upload` | No | Not supported |
| `threads` | No | Not supported |
| `buttons` | No | Not supported |

### What Claude can do with Spotify

- **Playback control** -- Play, pause, skip, previous, seek, set volume, toggle shuffle and repeat
- **Search** -- Find songs, albums, artists, and playlists by name
- **Queue management** -- Add songs to the playback queue, view upcoming tracks
- **Now playing** -- Show the currently playing track with artist, album, and progress
- **Library management** -- Save tracks to your library, check if tracks are saved
- **Device control** -- List available playback devices and transfer playback between them
- **Playlist browsing** -- List your playlists and view their contents

### Spotify Premium vs Free

| Feature | Premium | Free |
|---------|---------|------|
| Play specific songs | Yes | Shuffle only |
| Skip tracks | Unlimited | Limited |
| Playback control (pause, seek) | Yes | Limited |
| Set volume | Yes | Yes |
| View now playing | Yes | Yes |
| Search | Yes | Yes |

## Troubleshooting

### "No active device found" error

The Spotify Web API can only control playback on a device that is already active. To fix this:

1. Open the Spotify app on any device (desktop, phone, smart speaker, or web player at [open.spotify.com](https://open.spotify.com))
2. Start playing any song (even briefly)
3. Now Claude can control playback on that device

The API cannot "wake up" a device that is not running Spotify or not currently active.

### Activation fails / authentication error

- **Client credentials wrong.** Double-check that the `client_id` and `client_secret` in your connector config match the values in the Spotify Developer Dashboard.
- **Refresh token wrong or revoked.** The `refresh_token` must come from an authorization code flow for the *same* `client_id`/`client_secret`. If you regenerated your client secret, rotated the token, or revoked AI Shuffle's access in your Spotify account settings, the refresh token will no longer work -- generate a new one (step 4) and update the connector config.

### "Insufficient client scope" error

The refresh token must have been granted the scopes the connector uses to control playback. If you see this error, the token was generated with insufficient permissions. Re-run the authorization code flow (step 4) requesting all required scopes, then update the connector config with the new refresh token.

### Playback commands work but search does not (or vice versa)

- This typically indicates a partial scope grant on your refresh token. Re-run the authorization code flow including all required scopes, then update the connector config with the new refresh token.

### Token refresh fails

The connector automatically exchanges your refresh token for new access tokens. If this fails:

1. Check that your Spotify Developer application is still active in the Developer Dashboard
2. Confirm the `refresh_token` in the connector config is current and was issued for this app's credentials
3. If the issue persists, generate a fresh refresh token (step 4) and update the connector config

### Rate limiting

Spotify's API has rate limits. If Claude is making many rapid requests (e.g., repeatedly checking what is playing), you may hit the rate limit. The connector handles rate limit responses by waiting and retrying, but if issues persist, space out your requests.
