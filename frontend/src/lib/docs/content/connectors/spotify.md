<!--
title: Spotify
description: Control Spotify playback through Claude
order: 12
-->

# Spotify Connector

## Overview

The Spotify connector allows Claude to control music playback on your Spotify account through AI Shuffle. With this connector active, you can ask Claude to play songs, albums, or playlists, skip tracks, adjust volume, search for music, and manage your playback queue -- all through natural language conversation.

The connector uses the [Spotify Web API](https://developer.spotify.com/documentation/web-api) with OAuth 2.0 authentication. You will need to create a Spotify Developer application to obtain client credentials, after which the connector handles the OAuth authorization flow. Note that some playback control features require a Spotify Premium subscription.

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
   - **Redirect URI:** `http://localhost:8000/api/v1/connectors/spotify/callback` (this must match exactly -- adjust the host/port if your AI Shuffle backend runs elsewhere)
   - **APIs used:** Check **Web API**
4. Click **Save**

### 3. Get your client credentials

1. In the Spotify Developer Dashboard, click on your newly created app
2. Click **Settings**
3. Note down the following values:
   - **Client ID** -- A string like `1a2b3c4d5e6f7g8h9i0j`
   - **Client Secret** -- Click "View client secret" to reveal it. A string like `AbCdEfGhIjKlMnOpQrSt`

Keep these credentials secure. Do not share them or commit them to version control.

### 4. Create the Spotify connector in AI Shuffle

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Spotify",
    "connector_type": "spotify",
    "config": {
      "client_id": "1a2b3c4d5e6f7g8h9i0j",
      "client_secret": "AbCdEfGhIjKlMnOpQrSt"
    }
  }'
```

Or use the AI Shuffle UI: **Settings > Connectors > Add Connector > Spotify**.

### 5. Complete the OAuth authorization flow

After creating the connector, you will need to authorize AI Shuffle to access your Spotify account:

1. Activate the connector (see step 6) -- the first activation will initiate the OAuth flow
2. You will be redirected to Spotify's authorization page (or receive a URL to open in your browser)
3. Review the permissions requested and click **Agree**
4. You will be redirected back to AI Shuffle with an authorization code
5. The connector exchanges this code for access and refresh tokens automatically

### 6. Activate the connector

```bash
curl -X POST http://localhost:8000/api/v1/connectors/{connector_id}/activate \
  -H "Authorization: Bearer aih_your_key"
```

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

## Capabilities

The Spotify connector supports the following capabilities:

| Capability | Supported | Description |
|------------|-----------|-------------|
| `rich_text` | Yes | Responses include formatted track information (song name, artist, album, progress) |
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

### OAuth authorization fails

- **Redirect URI mismatch.** The most common cause. The redirect URI in your Spotify Developer app settings must exactly match `http://localhost:8000/api/v1/connectors/spotify/callback`. Check for trailing slashes, protocol (`http` vs `https`), and port number.
- **Client credentials wrong.** Double-check that the `client_id` and `client_secret` in your connector config match the values in the Spotify Developer Dashboard.
- **Browser cookies.** If the authorization page shows an error, try opening the authorization URL in an incognito/private browser window.

### "Insufficient client scope" error

The connector needs specific OAuth scopes to control playback. If you see this error, the token may have been created with insufficient permissions. Delete the connector, recreate it, and complete the OAuth flow again to ensure all required scopes are granted.

### Playback commands work but search does not (or vice versa)

- This typically indicates a partial OAuth scope grant. Recreate the connector and re-authorize to ensure all scopes are included.

### Token refresh fails

The connector automatically refreshes expired access tokens using the refresh token. If this fails:

1. Check that your Spotify Developer application is still active in the Developer Dashboard
2. Deactivate and reactivate the connector to trigger a fresh OAuth flow
3. If the issue persists, delete the connector and create a new one

### Rate limiting

Spotify's API has rate limits. If Claude is making many rapid requests (e.g., repeatedly checking what is playing), you may hit the rate limit. The connector handles rate limit responses by waiting and retrying, but if issues persist, space out your requests.
