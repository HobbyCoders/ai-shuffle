package com.hobbycoders.aishuffle.presence

import com.hobbycoders.aishuffle.model.Server
import okhttp3.CookieJar
import okhttp3.OkHttpClient
import org.junit.Assert.*
import org.junit.Test
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import javax.net.ssl.SSLHandshakeException

class WorkerPresenceProbeTest {
    private val direct = Server("local-one", "Home", "https://worker.example", "aim_private", "phone-device")
    private val account = direct.copy(accountId = "account-one", hubUrl = "https://hub.example", hubServerId = "worker-one")
    private val ready = """{"ready":true,"protected_ingress":true,"server_id":"worker-one"}"""

    @Test fun publicProbeNeverSendsWorkerAccountOrCookieCredentials() {
        val request = WorkerPresenceProbe.request(account)
        assertEquals("https://worker.example/api/v1/mobile/hub/endpoint", request.url.toString())
        assertNull(request.header("Authorization")); assertNull(request.header("Cookie"))
        assertEquals("no-cache, no-store", request.header("Cache-Control"))
        val field = WorkerPresenceProbe::class.java.getDeclaredField("client").apply { isAccessible = true }
        val client = field.get(null) as OkHttpClient
        assertFalse(client.followRedirects); assertFalse(client.followSslRedirects)
        assertSame(CookieJar.NO_COOKIES, client.cookieJar); assertNull(client.cache)
        assertEquals(6_000, client.callTimeoutMillis)
    }
    @Test fun onlyConfiguredAccessHeadersGoToExactHttpsOrigin() {
        val request = WorkerPresenceProbe.request(direct.copy(cfAccessClientId = "access-id", cfAccessClientSecret = "access-secret"))
        assertEquals("https://worker.example/api/v1/health", request.url.toString())
        assertEquals("access-secret", request.header("CF-Access-Client-Secret")); assertNull(request.header("Authorization"))
        for (origin in listOf("http://worker.example", "https://user@worker.example", "https://worker.example/path"))
            assertThrows(IllegalArgumentException::class.java) { WorkerPresenceProbe.request(direct.copy(baseUrl = origin)) }
    }
    @Test fun managedProbeVerifiesActualWorkerNotViewingPhoneOrHubListing() {
        assertEquals(ProbeResult.ONLINE, WorkerPresenceProbe.parse(account, 200, ready))
        for (body in listOf(ready.replace("worker-one", "phone-device"), ready.replace("worker-one", "worker-two"),
            ready.replace("true", "\"true\""), ready.replace("true", "false"), """{"active":true,"id":"worker-one"}"""))
            assertEquals(ProbeResult.UNAVAILABLE, WorkerPresenceProbe.parse(account, 200, body))
    }
    @Test fun directProbeRequiresCompleteStrictHealthShape() {
        val healthy = """{"status":"healthy","service":"Shuffle","version":"fixture","authenticated":false}"""
        assertEquals(ProbeResult.ONLINE, WorkerPresenceProbe.parse(direct, 200, healthy))
        assertEquals(ProbeResult.ONLINE, WorkerPresenceProbe.parse(direct, 200, healthy.replace("false", "true")))
        for (body in listOf("{}", healthy.replace("false", "\"false\""), healthy.replace("fixture", ""), "<html>Sign in</html>"))
            assertEquals(ProbeResult.UNAVAILABLE, WorkerPresenceProbe.parse(direct, 200, body))
    }
    @Test fun securityServiceMalformedAndRedirectResponsesStayUnavailable() {
        for (code in listOf(301, 302, 307, 401, 403, 404, 500, 503))
            assertEquals(ProbeResult.UNAVAILABLE, WorkerPresenceProbe.parse(account, code, ready))
        for (code in listOf(502, 504, 521, 522, 523, 524, 530))
            assertEquals(ProbeResult.UNREACHABLE, WorkerPresenceProbe.parse(account, code, ""))
        assertEquals(ProbeResult.UNAVAILABLE, WorkerPresenceProbe.parse(account, 200, " ".repeat(8_193)))
        assertEquals(ProbeResult.UNAVAILABLE, WorkerPresenceProbe.failure(UnknownHostException()))
        assertEquals(ProbeResult.UNAVAILABLE, WorkerPresenceProbe.failure(SSLHandshakeException("fixture")))
        assertEquals(ProbeResult.UNREACHABLE, WorkerPresenceProbe.failure(ConnectException()))
        assertEquals(ProbeResult.UNREACHABLE, WorkerPresenceProbe.failure(SocketTimeoutException()))
    }
}
