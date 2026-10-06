package com.hobbycoders.aishuffle.ui

import android.os.Handler
import android.os.Looper
import android.widget.TextView

/**
 * A chat status message that goes away by itself (#1946). Chat uses one for a file
 * (image, media, linked file) that failed to load through WorkerAssetLoader, so one
 * failure during a worker restart does not leave a notice up: it clears when a later
 * file loads, after [DURATION_MS], or when the owner taps it. A [sticky] notice (one the
 * owner has to act on) waits for one of the others instead of timing out.
 *
 * Clearing removes only this notice: a newer status (a page failure, a download) that
 * has replaced it on the shared status view stays on screen.
 */
internal class StatusNotice(
    private val status: TextView,
    private val handler: Handler = Handler(Looper.getMainLooper()),
) {
    /** The notice this owns, or null once cleared. */
    var message: String? = null
        private set
    private val expire = Runnable { clear() }

    fun show(text: String, sticky: Boolean = false) {
        handler.removeCallbacks(expire)
        message = text
        status.text = text
        if (!sticky) handler.postDelayed(expire, DURATION_MS)
    }

    fun clear() {
        handler.removeCallbacks(expire)
        val shown = message ?: return
        message = null
        if (status.text.toString() == shown) status.text = ""
    }

    companion object {
        const val DURATION_MS = 8_000L
    }
}
