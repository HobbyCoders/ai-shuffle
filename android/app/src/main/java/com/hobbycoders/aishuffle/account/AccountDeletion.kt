package com.hobbycoders.aishuffle.account

/**
 * Google Play account deletion.
 *
 * The hub's `/delete-account` flow is the one deletion path. It asks for a
 * fresh sign-in on the hub's own origin (a hosted sign-in session token bound
 * to that origin, plus a `__Host-` cookie) before it deletes anything, so the
 * phone's stored access token alone can never delete an account. The app
 * opens that page in a Custom Tab from Settings > Account > Delete account,
 * then checks with the hub whether this phone's account is gone and signs the
 * phone out when it is. The same page is the public deletion URL for the Play
 * listing, reachable without the app.
 */
object AccountDeletion {
    /** The public web page, also the Play Console "delete account URL". */
    const val PUBLIC_URL = "${AccountStore.DEFAULT_HUB}/delete-account"

    /** The deletion page of the hub this account belongs to; only an HTTPS origin is accepted. */
    fun pageFor(hub: String): String = "${PairingInput.origin(hub)}/delete-account"

    /** ACCESS_ENDED: the account is gone, or this phone was revoked; either way it is signed out. */
    enum class Result { ACCESS_ENDED, STILL_ACTIVE }

    /**
     * After the browser returns: an authenticated call that fails with 401 or
     * 403 means the hub revoked this phone, which is what deletion does first.
     * A network failure is not proof of anything and is rethrown.
     */
    suspend fun check(probe: suspend () -> Unit): Result = try {
        probe()
        Result.STILL_ACTIVE
    } catch (e: HubFailure) {
        if (e.status == 401 || e.status == 403) Result.ACCESS_ENDED else throw e
    }
}
