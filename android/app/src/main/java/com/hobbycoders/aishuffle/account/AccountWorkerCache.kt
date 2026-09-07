package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server

/** Called only with the current account's authenticated hub registry response. */
internal object AccountWorkerCache {
    fun updated(saved: List<Server>, session: AccountSession, serverId: String, name: String, rawUrl: String): Server? {
        val url = PairingInput.origin(rawUrl)
        val id = HubClient.segment(serverId)
        val cached = saved.firstOrNull { it.accountId == session.accountId && it.hubUrl == session.hub &&
            it.hubServerId == id && it.deviceId == session.deviceId && it.apiKey.startsWith("aim_") && it.apiKey.length > 4 } ?: return null
        return cached.copy(name = name, baseUrl = url,
            // Access service tokens are configured for an origin, unlike the verified
            // worker/device key. A new tunnel must not inherit those unrelated headers.
            cfAccessClientId = cached.cfAccessClientId.takeIf { cached.baseUrl == url },
            cfAccessClientSecret = cached.cfAccessClientSecret.takeIf { cached.baseUrl == url })
    }
}
