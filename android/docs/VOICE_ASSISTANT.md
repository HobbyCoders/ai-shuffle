# Voice assistant

Voice is a live GPT Realtime call funded by *your* AI Shuffle server (your
OpenAI key never leaves it). On the call the agent can dispatch AI Shuffle
worker agents and use the phone tools the app declares. There is no wake word,
quick-settings tile or assist gesture: you start every call yourself, from a
chat (which hands off that session) or from the worker list, and it stays
connected until you hang up or ask the agent to end it.

Lifecycle, recovery and timeouts are in
[native-voice-lifecycle.md](native-voice-lifecycle.md).

## How it works

```
the app                       your AI Shuffle server               OpenAI Realtime
  1. POST /voice/sessions  ──►  mints an ek_ client secret  ──►
  2. WebRTC: mic + audio + "oai-events" data channel  ◄──────────────────────┘
  3. POST /voice/sessions/{id}/call {call_id, client_tools, session_mode}
     → the server's sideband joins, declares dispatch_agent plus the phone
       tools, and leaves phone-tool servicing to the app
  4. The app answers phone-tool function calls locally on the data channel
```

- **The call** is the web UI's voice mode: the server mints a short-lived
  `ek_` client secret, the app does the SDP exchange with
  `api.openai.com/v1/realtime/calls` and reports the `call_id` so the
  server's sideband joins the same call (transcripts, cost tracking and agent
  dispatch keep working). The app always reports `session_mode: "chat"`.
- **The prompts live on the server.** Personality, ground rules and mode
  behaviour are edited in the web UI under Settings → Voice and apply to the
  next call.
- **Phone tools** are the same Android tools the worker can queue
  (`ToolRegistry`), plus `end_session`.

## Safety

Calls, text messages and calendar events are never completed by the agent:
the app opens the dialer, SMS app or calendar editor pre-filled, and you
finish the action in Android's own screen. Other actions (open apps, links,
navigation, alarms, timers) need an on-screen Allow in the app, and location
needs Android's location permission plus a separate per-request consent.
Optional app control runs through the AI Shuffle accessibility service only
for apps you select, with Allow once / Deny for every request.

## Setup

1. Enable **voice mode** on the server and configure an OpenAI API-key
   provider.
2. Start voice in the app and grant the microphone permission when asked.
3. Optionally review the phone tools in the worker's tool settings.

The app requests no phone, SMS, contacts, calendar, notification-access or
overlay permissions.

## Known limits

- Calls refresh their credentials on the same session before OpenAI's
  60-minute limit; the worker's call-duration cap still ends a call.
