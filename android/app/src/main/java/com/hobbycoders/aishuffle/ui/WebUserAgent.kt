package com.hobbycoders.aishuffle.ui

/**
 * Marks the embedded chat WebView as the Android app so the web frontend
 * renders its mobile shell. The frontend looks for `AIShuffleAndroid/<version>`
 * in `navigator.userAgent` (see frontend/src/lib/utils/platform.ts); a plain
 * phone browser has no such token and gets the desktop shell.
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
