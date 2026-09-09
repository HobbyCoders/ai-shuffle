package com.hobbycoders.aishuffle.account

import android.content.Context
import android.content.Intent
import android.webkit.CookieManager
import android.webkit.WebStorage
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.voice.VoiceCallService
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONObject
import kotlin.coroutines.resume

class AccountRepository(private val context: Context) {
    val store = AccountStore(context)
    private val servers = ServerStore.get(context)
    suspend fun clearLocal() {
        store.clear()
        com.hobbycoders.aishuffle.tools.DeviceToolsRuntime.stop()
        servers.removeAccountServers()
        VoiceCallService.hangup(context)
        context.stopService(Intent(context, VoiceCallService::class.java))
        context.sendBroadcast(Intent(ACTION_CLEARED).setPackage(context.packageName))
        WebStorage.getInstance().deleteAllData()
        suspendCancellableCoroutine<Unit> { c -> CookieManager.getInstance().removeAllCookies { CookieManager.getInstance().flush(); if (c.isActive) c.resume(Unit) } }
    }
    suspend fun accept(hub: String, result: JSONObject) {
        val session = AccountSession.parse(hub, result)
        require(session.deviceId == store.installId) { "The hub returned authorization for another device. Pair this phone again." }
        clearLocal()
        store.save(session)
    }
    suspend fun sync(onlyServerId: String? = null) {
        val session = store.session() ?: throw IllegalStateException("Sign in to sync your servers.")
        val list = HubClient.call(session.hub, "/v1/servers", token = session.token).getJSONArray("servers")
        check(store.matches(session)) { "Account changed. Sign in again." }
        val saved = servers.getAll()
        val listed = (0 until list.length()).map { i ->
            val item = list.getJSONObject(i)
            val id = HubClient.segment(item.getString("id"))
            val url = PairingInput.origin(item.getString("url"))
            AccountWorkerCache.listed(saved, session, id, item.getString("name"), url)
        }
        check(store.matches(session)) { "Account changed. Sign in again." }
        // The authenticated registry proves membership, independently of whether
        // an offline/reconnecting worker can issue this phone a new key right now.
        servers.replaceAccountServers(listed)
        val candidates = listed.filter { onlyServerId == null || it.hubServerId == onlyServerId }
        AccountWorkerSync.authorizePending(candidates, { store.matches(session) }, { servers.update(it) }) { pending ->
            val id = requireNotNull(pending.hubServerId)
            val ticket = HubClient.call(session.hub, "/v1/servers/$id/tickets", "POST", token = session.token).getString("ticket")
            // This request has NO account Authorization header. Only a one-use ticket crosses origins.
            val enrolled = WorkerEnrollment.request(pending.name) {
                HubClient.call(pending.baseUrl, "/api/v1/mobile/hub/enroll", "POST", JSONObject().put("ticket", ticket).put("device_info", HubClient.device(session.deviceId).put("capabilities", JSONObject().put("supports_push", false))))
            }
            require(enrolled.getString("device_id") == session.deviceId) { "Server returned the wrong device identity. Contact its administrator." }
            require(enrolled.getString("api_key").startsWith("aim_")) { "Server returned an unsupported device key." }
            pending.copy(apiKey = enrolled.getString("api_key"))
        }
    }
    suspend fun link(server: Server) {
        val session = store.session() ?: throw IllegalStateException("Sign in before sharing a server.")
        val registered = store.pendingLink(server.id, session)?.getJSONObject("registration")
            ?: HubClient.call(session.hub, "/v1/servers", "POST", JSONObject().put("name", server.name).put("url", server.baseUrl), session.token).also {
                check(store.matches(session)) { "Account changed. Sign in again." }
                store.savePendingLink(server.id, session, it)
            }
        val id = HubClient.segment(registered.getString("id"))
        HubClient.call(server.baseUrl, "/api/v1/mobile/hub/link", "POST", JSONObject().put("hub_url", session.hub).put("server_id", id).put("server_secret", registered.getString("server_secret")), server.apiKey)
        store.clearPendingLink(server.id)
    }
    suspend fun lookupWorker(code: String): JSONObject {
        val session = store.session() ?: throw IllegalStateException("Sign in to add this worker to your account.")
        return HubClient.call(session.hub, "/v1/worker-links/lookup", "POST",
            JSONObject().put("code", PairingInput.workerCode(code)), session.token).also {
            check(store.matches(session)) { "Account changed. Sign in again." }
        }
    }
    suspend fun approveWorker(code: String): String {
        val session = store.session() ?: throw IllegalStateException("Sign in to add this worker to your account.")
        val result = HubClient.call(session.hub, "/v1/worker-links/approve", "POST",
            JSONObject().put("code", PairingInput.workerCode(code)), session.token)
        check(store.matches(session)) { "Account changed. Sign in again." }
        return HubClient.segment(result.getString("id"))
    }
    suspend fun workerAvailable(id: String): Boolean {
        val session = store.session() ?: throw IllegalStateException("Sign in again.")
        val list = HubClient.call(session.hub, "/v1/servers", token = session.token).getJSONArray("servers")
        check(store.matches(session)) { "Account changed. Sign in again." }
        return (0 until list.length()).any { list.getJSONObject(it).getString("id") == id }
    }
    companion object { const val ACTION_CLEARED = "com.hobbycoders.aishuffle.ACCOUNT_CLEARED" }
}
