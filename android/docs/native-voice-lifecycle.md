# Native voice lifecycle

The Android foreground microphone service owns one voice call. Leaving the
activity or rotating the phone keeps that call and its notification alive;
the notification and call screen both offer hangup. Returning after a failed
call does not start another paid session automatically. Retry is explicit.

Embedded chat hands off the selected session using `EXTRA_SESSION_ID`.
The native service authorizes with `SessionPurpose.NATIVE` and re-mints
`POST /api/v1/voice/sessions/{id}/secret` for that exact session. It never
falls back to creating a different session when a handoff fails. Native
credentials stay out of the embedded browser's cookie jar.

`VoiceCallRunner` owns setup, recovery, and refresh. Live means that WebRTC
media and the data channel are ready and the worker accepted its sideband
attachment. Missing call identifiers, attachment failures, or failed media
setup release the microphone and show an error. Cancellation closes any
partially opened peer; late SDP callbacks cannot revive it.

A temporary media disconnect gets a 12-second grace period. Calls refresh
their credentials every 50 minutes on the same session, using one peer at a
time. The UI shows reconnecting during the bounded replacement. A refresh
failure, including the worker's call-duration cap, ends the call rather than
starting a retry loop. Setup is bounded to 90 seconds; each voice HTTP call
has a 45-second total timeout.

Local worker identity, account membership, and microphone permission are
checked during setup and once a second during the call. The worker's
authenticated voice configuration is checked every 30 seconds after native
authorization succeeds, so server-side revocation or disabled voice is
observed while the activity is backgrounded. Account sign-out also retains
its existing immediate service-stop path. Every terminal path closes media
and attempts a bounded worker hangup.

`VoiceCallRunnerTest` uses fake peers and a fake backend, with no microphone,
network, account credentials, or provider bill. It covers media/attachment
readiness, missing identifiers, failed attachment, session identity,
revocation, transient recovery, timeout, refresh cap, and cancellation.
`VoiceLifecycleTest` covers the selected server/session intent and denied
microphone permission. Physical microphone quality and real provider/WebRTC
connectivity require a separate explicit device test.
