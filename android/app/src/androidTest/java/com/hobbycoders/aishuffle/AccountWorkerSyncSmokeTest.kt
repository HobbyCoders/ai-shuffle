package com.hobbycoders.aishuffle

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.hobbycoders.aishuffle.account.AccountRepository
import com.hobbycoders.aishuffle.account.AccountSession
import com.hobbycoders.aishuffle.account.AccountStore
import com.hobbycoders.aishuffle.account.HubClient
import com.hobbycoders.aishuffle.data.ServerStore
import com.hobbycoders.aishuffle.model.Server
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.time.Instant
import java.util.concurrent.CopyOnWriteArrayList

@RunWith(AndroidJUnit4::class)
class AccountWorkerSyncSmokeTest {
    @Test fun authenticatedRegistryTunnelChangeDoesNotRequestAnotherEnrollment() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val store = AccountStore(context)
        val session = AccountSession("https://hub-fixture.example", "fixture-token", Instant.now().plusSeconds(3600).toString(), "fixture-device", "fixture-account", "tester")
        val servers = ServerStore.get(context)
        servers.getAll().forEach { servers.remove(it.id) }
        store.save(session)
        val cached = Server("account:worker-one", "Worker", "https://old-tunnel.example", "aim_fixture-key", session.deviceId,
            accountId = session.accountId, hubUrl = session.hub, hubServerId = "worker-one")
        servers.add(cached)
        val requests = CopyOnWriteArrayList<String>()
        val clientField = HubClient::class.java.getDeclaredField("client").apply { isAccessible = true }
        val oldClient = clientField.get(null)
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request(); requests.add(request.url.toString())
            check(request.url.toString() == "${session.hub}/v1/servers" && request.method == "GET") { "Existing device must not re-enroll" }
            check(request.header("Authorization") == "Bearer fixture-token")
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("Fixture")
                .body("""{"servers":[{"id":"worker-one","name":"Renamed worker","url":"https://new-tunnel.example"}]}""".toResponseBody("application/json".toMediaType())).build()
        }.build()
        clientField.set(null, client)
        try {
            AccountRepository(context).sync()
            assertEquals(1, requests.size)
            val current = servers.getById(cached.id)!!
            assertEquals("https://new-tunnel.example", current.baseUrl)
            assertEquals(cached.apiKey, current.apiKey)
            assertEquals(cached.deviceId, current.deviceId)
        } finally { clientField.set(null, oldClient); store.clear(); servers.remove(cached.id) }
    }

    @Test fun accountRemovalDeletesRegistryBeforeCacheAndSurvivesRefresh() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val store = AccountStore(context)
        val session = AccountSession("https://hub-fixture.example", "fixture-token", Instant.now().plusSeconds(3600).toString(), "fixture-device", "fixture-account", "tester")
        val servers = ServerStore.get(context)
        servers.getAll().forEach { servers.remove(it.id) }
        store.save(session)
        val cached = Server("account:worker-one", "Worker", "https://worker.example", "aim_fixture-key", session.deviceId,
            accountId = session.accountId, hubUrl = session.hub, hubServerId = "worker-one")
        val direct = Server("direct", "Direct worker", "https://direct.example", "aim_direct-key", "direct-phone")
        servers.add(cached); servers.add(direct)
        servers.markUsed(cached.id); servers.selectedToolsServerId = cached.id
        val requests = CopyOnWriteArrayList<String>()
        val clientField = HubClient::class.java.getDeclaredField("client").apply { isAccessible = true }
        val oldClient = clientField.get(null)
        var revoked = false
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            requests.add("${request.method} ${request.url.encodedPath}")
            check(request.header("Authorization") == "Bearer fixture-token")
            check(request.url.host == "hub-fixture.example")
            val body = when (request.method) {
                "DELETE" -> {
                    check(request.url.encodedPath == "/v1/servers/worker-one")
                    check(servers.getById(cached.id) != null) { "Must not forget before the registry accepts removal" }
                    revoked = true
                    "{\"ok\":true}"
                }
                "GET" -> { check(revoked); "{\"servers\":[]}" }
                else -> error("Unexpected enrollment request after removal")
            }
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("Fixture")
                .body(body.toResponseBody("application/json".toMediaType())).build()
        }.build()
        clientField.set(null, client)
        try {
            val repo = AccountRepository(context)
            assertFalse(repo.remove(cached))
            assertNull(servers.getById(cached.id))
            assertNull(servers.lastUsedServerId)
            assertNull(servers.selectedToolsServerId)
            repo.sync()
            assertEquals(listOf(direct), servers.getAll())
            assertEquals(listOf("DELETE /v1/servers/worker-one", "GET /v1/servers"), requests.toList())
            repo.remove(direct)
            assertTrue(servers.getAll().isEmpty())
            assertEquals(2, requests.size)
        } finally {
            clientField.set(null, oldClient); store.clear()
            servers.getAll().forEach { servers.remove(it.id) }
        }
    }
}
