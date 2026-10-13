package com.hobbycoders.aishuffle.ui

/**
 * Marks the embedded chat WebView as the Android app: `AIShuffleAndroid/<version>`
 * in `navigator.userAgent`, for worker-side diagnostics and logs only. The UI
 * is not chosen from it: the app loads the worker's phone UI at `/mobile/` (see
 * MobileUi) and the worker's root UI is desktop-only.
 *
 * The token is appended to the WebView's default user agent rather than
 * replacing it, so the server still sees the real WebView/Chrome version.
 */
object WebUserAgent {
    const val PRODUCT = "AIShuffleAndroid"

    fun token(versionName: String?): String {
        val version = versionName?.trim().orEmpty().ifEmpty { "0" }
        return "$PRODUCT/${version.replace(Regex("\\s+"), "-")}"
    }

    /** Returns [base] with the app token appended once. */
    fun decorate(base: String?, versionName: String?): String {
        val current = base?.trim().orEmpty()
        if (current.contains("$PRODUCT/")) return current
        val token = token(versionName)
        return if (current.isEmpty()) token else "$current $token"
    }
}
