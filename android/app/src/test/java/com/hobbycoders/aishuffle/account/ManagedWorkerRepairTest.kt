package com.hobbycoders.aishuffle.account

import com.hobbycoders.aishuffle.model.Server
import okhttp3.CookieJar
import okhttp3.OkHttpClient
import okio.Buffer
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant

class ManagedWorkerRepairTest {
    private val worker = "11111111-1111-4111-8111-111111111111"
    private val phone = "22222222-2222-4222-8222-222222222222"
    private val server = Server("account:$worker", "Fixture", "https://worker.example", "aim_" + "a".repeat(43), phone,
        accountId = "account-one", hubUrl = "https://hub.example", hubServerId = worker)
    private val scoped = "7bff8b2f-d077-57e0-a71c-7dada5be517f" // Python uuid.uuid5(NAMESPACE_URL, ...) vector.
    private fun support() = JSONObject().put("server_id", worker).put("ready", true).put("protected_ingress", true).put("account_repair", "scoped-v1")
    private fun key() = JSONObject().put("api_key", "aim_" + "b".repeat(43)).put("device_id", scoped)
        .put("expires_at", "2099-01-01T00:00:00Z").put("scopes", JSONArray().put("web:session").put("controller"))

    @Test fun uuidV5MatchesBackendAndSeparatesWorkersSharingOnePhone() {
        assertEquals(scoped, ManagedWorkerRepair.scopedDeviceId(server))
        assertNotEquals(scoped, ManagedWorkerRepair.scopedDeviceId(server.copy(hubServerId = "33333333-3333-4333-8333-333333333333")))
        assertNotEquals(scoped, ManagedWorkerRepair.scopedDeviceId(server.copy(deviceId = "33333333-3333-4333-8333-333333333333")))
        assertThrows(IllegalArgumentException::class.java) { ManagedWorkerRepair.scopedDeviceId(server.copy(hubServerId = "worker-one")) }
    }
    @Test fun scopedRepairRequestCannotFallBackToDirectEnrollmentOrSendAccountCredentials() {
        val request = ManagedWorkerRepair.request(server, "ABCDEF234567")
        assertEquals("https://worker.example/api/v1/mobile/register", request.url.toString())
        assertNull(request.header("Authorization")); assertNull(request.header("Cookie"))
        val buffer = Buffer(); request.body!!.writeTo(buffer)
        val raw = buffer.readUtf8(); val json = JSONObject(raw)
        assertEquals("scoped-v1", json.getString("account_repair"))
        assertEquals(scoped, json.getJSONObject("device_info").getString("device_id"))
        assertFalse(raw.contains(server.apiKey)); assertFalse(raw.contains(phone)); assertFalse(raw.contains(server.accountId!!))
        assertThrows(IllegalArgumentException::class.java) { ManagedWorkerRepair.request(server.copy(accountId = null), "ABCDEF234567") }
    }
    @Test fun accessHeadersStayOnExactHttpsOriginWithNoRedirectsCookiesOrCache() {
        val configured = server.copy(cfAccessClientId = "fixture-id", cfAccessClientSecret = "fixture-secret")
        assertEquals("fixture-secret", ManagedWorkerRepair.request(configured).header("CF-Access-Client-Secret"))
        for (url in listOf("http://worker.example", "https://user@worker.example", "https://worker.example/path"))
            assertThrows(IllegalArgumentException::class.java) { ManagedWorkerRepair.request(configured.copy(baseUrl = url)) }
        val field = ManagedWorkerRepair::class.java.getDeclaredField("client").apply { isAccessible = true }
        val client = field.get(null) as OkHttpClient
        assertFalse(client.followRedirects); assertFalse(client.followSslRedirects)
        assertSame(CookieJar.NO_COOKIES, client.cookieJar); assertNull(client.cache)
    }
    @Test fun onlyExactVerifiedWorkerCapabilityAllowsRepair() {
        ManagedWorkerRepair.checkSupport(server, support())
        val oldWorker = support().apply { remove("account_repair") }
        assertEquals(ManagedWorkerRepair.UPDATE_GUIDANCE, assertThrows(WorkerRepairFailure::class.java) { ManagedWorkerRepair.checkSupport(server, oldWorker) }.message)
        for (json in listOf(support().put("account_repair", "v1"), support().put("ready", "true"),
            support().put("protected_ingress", false), support().put("server_id", phone)))
            assertThrows(WorkerRepairFailure::class.java) { ManagedWorkerRepair.checkSupport(server, json) }
    }
    @Test fun strictResponseRequiresReturnedScopedIdentityAndNewCredential() {
        assertEquals("aim_" + "b".repeat(43), ManagedWorkerRepair.parseKey(server, key()).value)
        for (json in listOf(key().apply { remove("device_id") }, key().put("device_id", phone),
            key().put("api_key", server.apiKey), key().put("api_key", "aim_short"), key().put("api_key", "aim_" + "b".repeat(43) + "\n")))
            assertThrows(WorkerRepairFailure::class.java) { ManagedWorkerRepair.parseKey(server, json) }
    }
    @Test fun expiredMissingOrMalformedExpiryAndScopesCannotReplaceCredential() {
        for (json in listOf(key().apply { remove("expires_at") }, key().put("expires_at", "2000-01-01T00:00:00Z"),
            key().put("expires_at", "never"), key().put("scopes", JSONArray().put(true)), key().put("scopes", JSONArray().put("controller"))))
            assertThrows(WorkerRepairFailure::class.java) { ManagedWorkerRepair.parseKey(server, json, Instant.parse("2026-09-07T00:00:00Z")) }
    }
}
