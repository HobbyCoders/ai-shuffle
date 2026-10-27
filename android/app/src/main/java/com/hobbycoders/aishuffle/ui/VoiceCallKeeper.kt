package com.hobbycoders.aishuffle.ui

import android.content.Context
import android.content.SharedPreferences
import android.os.Build

/**
 * One chat's voice call, as the page reports it over `shuffleShell`
 * (`voice-call` and `voice-call-state`, see MobileUi). While a call is up and
 * the microphone is allowed, it runs [VoiceCallService] so the call survives
 * the lock screen and other apps; chat then leaves its WebView running in
 * onStop ([keepsRunningInBackground]). Main thread only.
 *
 * Android 14 lets an app start a microphone service only while it is visible,
 * so [keepAlive] does nothing unless [visible]; chat retries when it is on screen.
 * The call counts as kept alive only once the service is in the foreground
 * ([VoiceCallService.Owner.onServiceForeground]); a refusal or any stop the
 * keeper did not ask for clears it again and reports it to [keptAliveChanged].
 */
class VoiceCallKeeper(
    private val context: Context,
    private val serverId: String,
    private val workerName: String,
    private val visible: () -> Boolean,
    private val microphoneAllowed: () -> Boolean,
    /** Hands a notification command to the page; the caller checks the document is still the call's. */
    private val send: (MobileUi.VoiceCallCommand) -> Unit,
    /** The service went foreground (true) or no longer keeps the call alive (false). */
    private val keptAliveChanged: (Boolean) -> Unit = {},
) : VoiceCallService.Owner {
    /** The page said a call is up. */
    var active = false
        private set
    /** The chat document that owns the call. */
    var document = -1
        private set
    /** The call's foreground service is running in the foreground. */
    var keptAlive = false
        private set
    /** The service was asked for and has not answered yet. */
    var starting = false
        private set
    /**
     * The service came up for this call and then stopped or could not go foreground
     * ([onServiceStopped]); it is not started again for this call. A start Android
     * refused outright ([keepAlive] false) is not counted: chat retries it on resume.
     */
    private var serviceEnded = false
    private var state: MobileUi.ShellMessage.VoiceCallState? = null
    private var notificationsOffSent = false

    /** `voice-call` active: a call started in [document] (repeats are ignored). */
    fun started(document: Int) {
        if (active && this.document == document) { keepAlive(); return }
        if (active) ended()
        active = true; this.document = document; state = null; notificationsOffSent = false; serviceEnded = false
        keepAlive()
    }

    /** `voice-call-state`: ignored unless it is the active call's document. */
    fun update(document: Int, next: MobileUi.ShellMessage.VoiceCallState) {
        if (!active || this.document != document) return
        state = next
        // While starting too, so the notification shows the latest state when it appears.
        if (keptAlive || starting) VoiceCallService.update(context, this, call())
    }

    /** Posts the notification again (notifications were just allowed). */
    fun refresh() {
        if (active && keptAlive) VoiceCallService.update(context, this, call())
    }

    /** The call ended (the page said so, it navigated, or chat closed). */
    fun ended() {
        if (!active) return
        val wasKept = keptAlive
        val wasStarting = starting
        if (keptAlive || starting) VoiceCallService.stop(context, this)
        active = false; keptAlive = false; starting = false; document = -1; state = null
        // While starting, onStop also left chat running: chat re-pauses it either way.
        if (wasKept || wasStarting) keptAliveChanged(false)
    }

    /**
     * Asks for the foreground service if a call is up, chat is on screen and the
     * microphone is allowed. True when it was asked for now; the call is kept
     * alive only once the service reports it is in the foreground.
     */
    fun keepAlive(): Boolean {
        if (!active || keptAlive || starting || serviceEnded || !visible() || !microphoneAllowed()) return false
        starting = VoiceCallService.start(context, call(), this)
        return starting
    }

    /**
     * Whether onStop must leave the page (audio, socket, timers) running: the
     * service is foreground, or was asked for and has not answered (a refusal
     * then pauses chat through [keptAliveChanged]).
     */
    fun keepsRunningInBackground() = active && (keptAlive || starting)

    /**
     * Whether a microphone request from [document] may be granted while chat is
     * off screen: only the page that owns a call the service keeps in the foreground.
     */
    fun allowsBackgroundMicrophone(document: Int) = active && keptAlive && this.document == document

    /** Tells the page once per call that its notification cannot show. */
    fun notificationsOff() {
        if (!keptAlive || notificationsOffSent) return
        notificationsOffSent = true
        send(MobileUi.VoiceCallCommand.NOTIFICATIONS_OFF)
    }

    override fun onCommand(command: MobileUi.VoiceCallCommand) {
        if (active) send(command)
    }

    override fun onServiceForeground() {
        if (!active) return
        starting = false
        if (!keptAlive) { keptAlive = true; keptAliveChanged(true) }
    }

    override fun onServiceStopped() {
        val wasKept = keptAlive
        val wasStarting = starting
        keptAlive = false; starting = false
        if (active) serviceEnded = true
        if (wasKept || wasStarting) keptAliveChanged(false)
    }

    private fun call() = VoiceCallService.Call(serverId, workerName, state?.title, state?.status, state?.muted, state?.since)
}

/**
 * Android 13+ notification permission for the call notification: asked once,
 * only while chat is on screen, and recorded as asked only after the dialog
 * was really launched (a launch that never happened is asked again later).
 */
class CallNotificationAsk(private val prefs: SharedPreferences, private val sdk: Int = Build.VERSION.SDK_INT) {
    enum class Next { NOTHING, ASK, TELL_PAGE }

    fun next(keptAlive: Boolean, visible: Boolean, dialogOpen: Boolean, shown: Boolean, granted: Boolean): Next = when {
        !keptAlive || dialogOpen || shown -> Next.NOTHING
        sdk >= 33 && !granted && !prefs.getBoolean(ASKED, false) -> if (visible) Next.ASK else Next.NOTHING
        else -> Next.TELL_PAGE
    }

    fun launched() { prefs.edit().putBoolean(ASKED, true).apply() }

    companion object {
        const val PREFS = "voice_call"
        const val ASKED = "notifications_asked"
    }
}
