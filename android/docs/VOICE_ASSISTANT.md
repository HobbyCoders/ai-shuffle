# Voice

The app has no voice code of its own. Voice calls in the Android app are the
bundled web page's voice mode, the same as on the website: the page opens the
worker's voice session and audio connection, and the phone streams microphone
audio to that worker (which talks to the AI provider you configured there).
The prompts and settings live on the server under Settings → Voice.

What the shell contributes (`ui/ChatActivity.kt`):

- the `RECORD_AUDIO` permission and the WebView microphone grant, scoped to the
  bundle's own origin, the current document and a focused, authorized chat;
- nothing else: there is no foreground service, notification, call screen or
  JavaScript bridge for voice, and the app never holds a voice credential.

A call therefore lives as long as the chat page: leaving the app pauses the
WebView and ends the call, and you start the next one from the page's own
voice button. The earlier native hands-free experiment (a WebRTC call to
OpenAI run by a foreground service) was removed; the phone tools it declared
remain available to the worker through the queued-command path
(`tools/ToolRegistry.kt`).
