# Voice

The app has no voice code of its own. Voice calls in the Android app are the
phone UI's own voice mode (the page the worker serves at `/mobile/`), the same as on the website: the page opens the
worker's voice session and audio connection, and the phone streams microphone
audio to that worker (which talks to the AI provider you configured there).
The prompts and settings live on the server under Settings → Voice.

What the shell contributes (`ui/ChatActivity.kt`):

- the `RECORD_AUDIO` permission and the WebView microphone grant, scoped to the
  worker's `/mobile/` page, the current document and a focused, authorized chat;
- headset routing while the page has a call (`ui/VoiceAudioRouting.kt`);
- keeping a call alive with the phone locked or in another app: a foreground
  service of type `microphone` (`ui/VoiceCallService.kt`, driven by
  `ui/VoiceCallKeeper.kt`) with an ongoing "Voice call" notification that has
  Mute/Unmute and Hang up and returns to the call when tapped. It starts only
  while chat is on screen and stops when the call ends. While it is in the
  foreground, leaving the app does not pause the WebView, and only the call's
  own page may reopen the microphone off screen (with Android's permission
  already granted, never a dialog). Details and the page messages are in
  `native-chat-capabilities.md`.

There is still no call screen or JavaScript bridge for voice, and the app
never holds a voice credential. Without a call, leaving the app pauses the
WebView as before; with an app build from before the call notification,
leaving the app ends the call. You start a call from the page's own voice
button.

The earlier native hands-free experiment (a WebRTC call to
OpenAI run by a foreground service) was removed; the phone tools it declared
remain available to the worker through the queued-command path
(`tools/ToolRegistry.kt`).
