# Voice assistant

Talk to your AI Shuffle server hands-free, like a "Hey Google" you own. The
app spots a wake phrase fully offline, then opens a live GPT Realtime voice
call funded by *your* server (your OpenAI key never leaves it), on which the
agent can both dispatch AI Shuffle worker agents **and** act on the phone
itself: place calls, send texts, read notifications, open apps, navigate, set
alarms/timers, and read or create calendar events.

## Two modes

- **Command mode (wake word)** — built for driving / no-hands use. Say the
  wake phrase, wait for the ready beep, say the task ("call my love"). The
  agent does it — reading risky actions back for a spoken yes — then **hangs
  itself up** (via a client-side `end_session` tool). Placing a real phone
  call always ends the agent session immediately so the dialer gets the mic.
  A 30-second idle watchdog guarantees the session never lingers if you go
  silent.
- **Chat mode (button, QS tile, assist gesture)** — a normal conversation
  that stays connected until you hang up or say goodbye.

## How it works

```
Vosk (offline, on-device)          your AI Shuffle server              OpenAI Realtime
┌────────────────────────┐   1   ┌──────────────────────────┐   2   ┌───────────────┐
│ WakeWordService hears  │ ────► │ POST /voice/sessions      │ ────► │ mints ek_     │
│ the wake phrase        │       │ (aim_ key → web session)  │       │ client secret │
└────────────────────────┘       └──────────────────────────┘       └───────────────┘
        3. WebRTC: mic + audio + "oai-events" data channel ◄──────────────┘
        4. POST /voice/sessions/{id}/call {call_id, client_tools, client_instructions}
           → the server's sideband joins, declares dispatch_agent + the PHONE tools,
             and leaves phone-tool servicing to the app
        5. The app answers phone-tool function calls locally on the data channel
```

- **Wake word** — Vosk small English model (~40 MB, downloaded once from
  alphacephei.com into app storage). The recognizer is grammar-constrained to
  the wake phrase, so it can't transcribe anything else; no audio leaves the
  device until the phrase is heard. The phrase is whatever you type in
  settings (default "hey shuffle").
- **The call** — identical to the web UI's voice mode: the server mints a
  short-lived `ek_` client secret; the app does the SDP exchange with
  `api.openai.com/v1/realtime/calls` and reports the `call_id` back so the
  server's sideband bridge joins the same call (transcripts, cost tracking,
  agent dispatch all keep working).
- **Phone tools** — declared via the new `client_tools` field on
  `POST /api/v1/voice/sessions/{id}/call` (requires the matching ai-shuffle
  server update). The sideband merges them into its `session.update` and
  skips servicing them; the app executes them natively.
- **The agent's prompts live on the server** — the app only reports the
  session mode (`command`/`chat`) on attach. Personality, ground rules, and
  the per-mode behavior text are edited in the AI Shuffle web UI under
  Settings → Voice (three prompts, each with a built-in default and a
  reset button). Changes apply to the next call — nothing to reinstall.

## Safety

Calling, texting, and creating calendar events are **confirm-gated in code**:
the first tool call returns a `needs_confirmation` preview, the agent must
read it back ("Text Mom: 'be there in 10' — send it?"), and only a second
call with `confirmed=true` — after your spoken yes — executes. A mishearing
can never text anyone directly.

## Setup

1. Update the ai-shuffle server to a build containing the `client_tools`
   support, enable **voice mode** in its settings and configure an OpenAI
   API-key provider.
2. In the app: ⋮ menu → **Voice assistant**.
3. Grant the permissions you want the agent to have (each tool degrades
   gracefully without its permission): microphone, phone, contacts, SMS,
   calendar; notification access for "what did I miss?"; and ideally
   **display over other apps** so the call screen can open from the wake word
   (it also exempts the mic service from background-start limits).
4. Download the offline model, set your wake phrase, flip the switch.
5. Or skip the wake word entirely: use the **AI Shuffle Talk** quick-settings
   tile, the "Start a voice session now" button, or set AI Shuffle as your
   device assist app to trigger it with the power-button/corner gesture.

## Known limits

- Wake-word listening is English-acoustic (the small en-us model); short
  non-English phrases often still work if they map to English-ish sounds.
- The realtime call reconnect at OpenAI's 60-minute session wall is not yet
  implemented in the app (the web UI has it); very long calls end and can be
  restarted by voice or tap.
- "Check emails" is served through notification access (email notification
  previews) — there is no direct mailbox integration.
