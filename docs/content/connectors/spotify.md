<!--
title: Spotify
description: Control Spotify playback through your AI Shuffle agent
order: 12
-->

# Spotify

Ask your agent to play songs, albums and playlists, skip, change the volume, search for music, manage your queue and save tracks.

## You need

- A Spotify account. Premium is needed for full playback control.
- A free Spotify developer account at [developer.spotify.com](https://developer.spotify.com/).
- Spotify open and playing on a device, such as the desktop app, your phone or [open.spotify.com](https://open.spotify.com). Your agent controls a device that is already playing; it cannot wake one up.

## Set up

### 1. Create a Spotify app

1. Sign in to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and choose **Create App**.
2. Enter a name, such as `AI Shuffle`, and a description.
3. Add a **Redirect URI** you control, such as `http://localhost:8888/callback`. You only use it in step 2.
4. Tick **Web API** and save.
5. Open the app's **Settings** and copy the **Client ID** and **Client Secret**.

### 2. Get a refresh token

AI Shuffle needs a refresh token from Spotify, which you get once. Follow Spotify's [authorization code guide](https://developer.spotify.com/documentation/web-api/tutorials/code-flow) with your client ID, secret and redirect URI, and ask for these scopes:

`user-read-playback-state`, `user-modify-playback-state`, `user-read-currently-playing`, `user-top-read`, `user-library-read`, `user-library-modify`, `playlist-read-private`, `playlist-read-collaborative`

At the end you get a `refresh_token`. Copy it.

### 3. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **Spotify**. Choose **Install Spotify** if asked.
2. Choose **Connect account** and enter the **Client ID**, **Client Secret** and **Refresh Token**.
3. Choose **Create Connector**, then turn on **Activate**.

Start a song on one of your devices, then try "What's playing?" or "Skip to the next track".

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `client_id` | Yes | From your Spotify app |
| `client_secret` | Yes | From your Spotify app |
| `refresh_token` | Yes | From step 2 |
| `poll_interval_seconds` | No | How often to check what is playing. Default `30` |
| `enabled_action_categories` | No | Limit the agent to some kinds of actions: `playback`, `read`, `devices`, `search`, `queue`, `playlists`, `library`. Empty allows all |

## What your agent can do

While Spotify is on, every chat can use it. See [How AI actions work](/docs/connectors#how-ai-actions-work). Nothing asks for approval.

- Play, pause, skip, go back, seek, set the volume, shuffle and repeat.
- Search for songs, albums, artists and playlists.
- Add to and view the queue.
- Say what is playing.
- Save tracks and show your top tracks and artists.
- List your devices and move playback between them.
- List your playlists and what is in them.

On Spotify Free, your agent can only shuffle playlists and has limited skips and playback control.

## Troubleshooting

**"No active device found".** Open Spotify on any device and start a song, then ask again.

**Activation fails.** Check the client ID and secret. The refresh token must come from the same Spotify app. If you made a new client secret or removed the app's access in your Spotify account, get a new refresh token (step 2).

**"Insufficient client scope", or some commands work and others do not.** The refresh token is missing scopes. Get a new one with every scope from step 2.

**Some requests fail during heavy use.** Spotify limits how many requests an app can make. Wait a moment and try again.
