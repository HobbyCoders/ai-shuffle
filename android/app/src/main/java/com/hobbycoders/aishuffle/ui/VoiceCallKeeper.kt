package com.hobbycoders.aishuffle.ui

import android.content.Context

/**
 * One chat's voice call, as the page reports it over `shuffleShell`
 * (`voice-call` and `voice-call-state`, see MobileUi). While a call is up and
 * the microphone is allowed, it runs [VoiceCallService] so the call survives
 * the lock screen and other apps; chat then leaves its WebView running in
 * onStop ([keepsRunningInBackground]). Main thread only.
 *
 * Android 14 lets an app start a microphone service only while it is visible,
 * so [keepAlive] does nothing unless [visible]; chat retries when it is on screen.
 */
class VoiceCallKeeper(
    private val context: Context,
    private val serverId: String,
    private val workerName: String,
    private val visible: () -> Boolean,
    private val microphoneAllowed: () -> Boolean,
    /** Hands a notification command to the page; the caller checks the document is still the call's. */
    private val send: (MobileUi.VoiceCallCommand) -> Unit,
) : VoiceCallService.Owner {
    /** The page said a call is up. */
    var active = false
        private set
    /** The chat document that owns the call. */
    var document = -1
        private set
    /** The call's foreground service is running. */
    var keptAlive = false
        private set
    private var state: MobileUi.ShellMessage.VoiceCallState? = null
    private var notificationsOffSent = false

    /** `voice-call` active: a call started in [document] (repeats are ignored). */
    fun started(document: Int) {
        if (active && this.document == document) { keepAlive(); return }
        if (active) ended()
        active = true; this.document = document; state = null; notificationsOffSent = false
        keepAlive()
    }

    /** `voice-call-state`: ignored unless it is the active call's document. */
    fun update(document: Int, next: MobileUi.ShellMessage.VoiceCallState) {
        if (!active || this.document != document) return
        state = next
        if (keptAlive) VoiceCallService.update(context, this, call())
    }

    /** Posts the notification again (notifications were just allowed). */
    fun refresh() {
        if (active && keptAlive) VoiceCallService.update(context, this, call())
    }

    /** The call ended (the page said so, it navigated, or chat closed). */
    fun ended() {
        if (!active) return
        if (keptAlive) VoiceCallService.stop(context, this)
        active = false; keptAlive = false; document = -1; state = null
    }

    /**
     * Starts the foreground service if a call is up, chat is on screen and the
     * microphone is allowed. True only when it started now.
     */
    fun keepAlive(): Boolean {
        if (!active || keptAlive || !visible() || !microphoneAllowed()) return false
        keptAlive = VoiceCallService.start(context, call(), this)
        return keptAlive
    }

    /** Whether onStop must leave the page (audio, socket, timers) running. */
    fun keepsRunningInBackground() = active && keptAlive

    /** Tells the page once per call that its notification cannot show. */
    fun notificationsOff() {
        if (!keptAlive || notificationsOffSent) return
        notificationsOffSent = true
        send(MobileUi.VoiceCallCommand.NOTIFICATIONS_OFF)
    }

    override fun onCommand(command: MobileUi.VoiceCallCommand) {
        if (active) send(command)
    }

    private fun call() = VoiceCallService.Call(serverId, workerName, state?.title, state?.status, state?.muted, state?.since)
}
