package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server

/** The registry proves membership, never a new destination for a saved credential. */
internal object AccountWorkerCache {
    /** Registry membership remains visible while this phone obtains its own worker key. */
    fun listed(saved: List<Server>, session: AccountSession, serverId: String, name: String, rawUrl: String): Server =
        updated(saved, session, serverId, name, rawUrl) ?: Server(
            "account:${HubClient.segment(serverId)}", name, PairingInput.origin(rawUrl), "", session.deviceId,
            accountId = session.accountId, hubUrl = session.hub, hubServerId = serverId)

    fun updated(saved: List<Server>, session: AccountSession, serverId: String, name: String, rawUrl: String): Server? {
        PairingInput.origin(rawUrl) // Validate registry data, but never adopt it with an existing key.
        val id = HubClient.segment(serverId)
        val cached = saved.firstOrNull { it.accountId == session.accountId && it.hubUrl == session.hub &&
            it.hubServerId == id && it.deviceId == session.deviceId && hasKey(it) } ?: return null
        return cached.copy(name = name, baseUrl = PairingInput.origin(cached.baseUrl))
    }

    /** Reconcile against storage at commit time, not the registry request's snapshot.
     * A concurrent renewal/repair wins, including its pinned endpoint and origin-
     * bound Access credentials. A removed connection must not regain a stale key.
     */
    fun reconcile(saved: List<Server>, listed: List<Server>): List<Server> = listed.map { row ->
        val current = saved.firstOrNull { it.id == row.id && it.accountId == row.accountId &&
            it.hubUrl == row.hubUrl && it.hubServerId == row.hubServerId && it.deviceId == row.deviceId && hasKey(it) }
        if (current != null) current.copy(name = row.name, baseUrl = PairingInput.origin(current.baseUrl))
        else row.copy(baseUrl = PairingInput.origin(row.baseUrl), apiKey = "", keyEpoch = null,
            cfAccessClientId = null, cfAccessClientSecret = null)
    }

    private fun hasKey(server: Server) = server.apiKey.startsWith("aim_") && server.apiKey.length > 4
}
