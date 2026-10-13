package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server
import org.json.JSONObject

/** Account workers must be revoked at the registry, not just evicted from its local cache. */
internal object AccountWorkerRemoval {
    /** Returns whether the hub still needs to clean up the managed remote connection. */
    suspend fun remove(
        server: Server,
        session: AccountSession?,
        accountMatches: () -> Boolean,
        forget: (String) -> Unit,
        revoke: suspend (AccountSession, String) -> JSONObject = { account, id ->
            HubClient.call(account.hub, "/v1/servers/$id", "DELETE", token = account.token)
        },
    ): Boolean {
        if (server.accountId == null) {
            forget(server.id)
            return false
        }
        require(session != null && server.accountId == session.accountId && server.hubUrl == session.hub) {
            "Sign in to this worker’s account before removing it."
        }
        val id = HubClient.segment(requireNotNull(server.hubServerId) { "This worker has no account registration." })
        check(accountMatches()) { "Account changed. Sign in again." }
        // On failure leave the cached connection intact so the user can retry.
        val result = revoke(session, id)
        check(accountMatches()) { "Account changed. Sign in again." }
        forget(server.id)
        return result.optBoolean("cleanup_pending", false)
    }
}
