package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.account.PairingInput

/** Shared checks for native capabilities requested by the contained chat document. */
internal object ChatAccessPolicy {
    const val AUDIO_CAPTURE = "android.webkit.resource.AUDIO_CAPTURE"

    fun document(base: String, topLevel: String?, authorized: Boolean): Boolean =
        authorized && topLevel != null && PairingInput.sameOrigin(base, topLevel)

    fun microphone(base: String, topLevel: String?, origin: String, resources: Array<String>, authorized: Boolean): Boolean =
        document(base, topLevel, authorized) && PairingInput.sameOrigin(base, origin) &&
            resources.size == 1 && resources[0] == AUDIO_CAPTURE

    fun message(base: String, topLevel: String?, origin: String, mainFrame: Boolean, authorized: Boolean): Boolean =
        mainFrame && document(base, topLevel, authorized) && PairingInput.sameOrigin(base, origin)
}
