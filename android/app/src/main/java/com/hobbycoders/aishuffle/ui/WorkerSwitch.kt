package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.account.PairingInput
import com.hobbycoders.aishuffle.model.Server
import java.net.URI
import java.net.URLDecoder

/**
 * The page's "Connected to" sheet asks the app to connect to another worker by
 * navigating the phone UI to `/mobile/?connect=<worker origin>` (advertised as
 * the [CAPABILITY] capability; page side: mobile/src/shell/worker-connection.ts).
 *
 * The request only names a worker. The app connects to it only when it is one
 * this install is already paired with, matched by exact HTTPS origin after
 * normalisation ([PairingInput.origin]); the URL never carries credentials, and
 * nothing here pairs a new worker. Anything else is refused and the app stays
 * on the current worker.
 */
internal object WorkerSwitch {
    const val CAPABILITY = "worker-switch"
    const val PARAM = "connect"
    /** An origin is short; a longer value is not one. */
    private const val MAX_VALUE = 512

    const val MALFORMED = "Could not switch workers: that address is not valid."
    const val UNKNOWN = "That worker is not paired with this phone. Pair it in Workers first."

    sealed class Decision {
        /** Not a switch request: the navigation is handled as before. */
        object NotASwitch : Decision()
        /** A switch request the app refuses; stay connected and show [message]. */
        data class Reject(val message: String) : Decision()
        /** The target is the worker already shown. */
        object AlreadyConnected : Decision()
        /** A paired account worker this phone holds no key for yet: authorize it in Workers. */
        data class NeedsAuthorization(val server: Server) : Decision()
        /** Open chat on this paired worker. */
        data class Open(val server: Server) : Decision()
    }

    /**
     * Whether `url` is a switch request on the current worker's phone UI: a
     * `/mobile/` URL with a `connect` query parameter. Such a URL is never loaded.
     */
    fun isRequest(base: String, url: String?): Boolean {
        if (!MobileUi.isUiUrl(base, url)) return false
        val query = runCatching { URI(url).rawQuery }.getOrNull() ?: return false
        return query.split('&').any { it.substringBefore('=') == PARAM }
    }

    /**
     * The normalised origin a switch request names, or null when the request is
     * malformed: anything but exactly `/mobile/?connect=<https origin>` on the
     * current worker (no other parameters, no fragment, no path, credentials,
     * query or fragment inside the origin).
     */
    fun requestedOrigin(base: String, url: String?): String? = runCatching {
        val uri = URI(url)
        require(MobileUi.isUiUrl(base, url) && uri.rawFragment == null)
        require(uri.rawPath == MobileUi.PATH || uri.rawPath == MobileUi.PATH.removeSuffix("/"))
        val pairs = uri.rawQuery.orEmpty().split('&')
        require(pairs.size == 1)
        val parts = pairs[0].split('=', limit = 2)
        require(parts.size == 2 && parts[0] == PARAM && parts[1].isNotEmpty() && parts[1].length <= MAX_VALUE)
        val value = URLDecoder.decode(parts[1], "UTF-8")
        require(value.none { it.isWhitespace() || it.isISOControl() })
        PairingInput.origin(value)
    }.getOrNull()

    /**
     * What to do with navigation `url` from the current worker `base`'s phone UI.
     * `paired` is every worker this install may open (current account or local
     * pairings), most recently used first; when several share the target origin,
     * the first that holds a key wins.
     */
    fun decide(base: String, url: String?, paired: List<Server>): Decision {
        if (!isRequest(base, url)) return Decision.NotASwitch
        val target = requestedOrigin(base, url) ?: return Decision.Reject(MALFORMED)
        if (originOf(base) == target) return Decision.AlreadyConnected
        val matches = paired.filter { originOf(it.baseUrl) == target }
        val server = matches.firstOrNull { !it.needsAccountAuthorization } ?: matches.firstOrNull()
            ?: return Decision.Reject(UNKNOWN)
        return if (server.needsAccountAuthorization) Decision.NeedsAuthorization(server) else Decision.Open(server)
    }

    private fun originOf(raw: String): String? = runCatching { PairingInput.origin(raw) }.getOrNull()
}
