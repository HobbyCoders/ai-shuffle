package com.hobbycoders.aishuffle.net

import com.hobbycoders.aishuffle.model.Server
import com.hobbycoders.aishuffle.ui.WebSessionBootstrap
import com.hobbycoders.aishuffle.ui.ChatSessionOwner
import kotlinx.coroutines.*
import okio.Buffer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class WebSessionReservationTest {
    private val token = "wsm_" + "a".repeat(43)
    private fun body() = JSONObject().put("reservation", token).put("expires_at", "2000-01-01T00:00:00Z").put("expires_in", 120)
    private fun parse(json: JSONObject) = ApiClient.parseWebReservation(200, "application/json", json.toString(), 10_000, 11_000)

    @Test fun reservationUsesExactWorkerCredentialAndWebCommitHeaderOnly() {
        val server = Server.create("Fixture", "https://worker.example", "fixture-key").copy(cfAccessClientId = "fixture-cf", cfAccessClientSecret = "fixture-secret")
        val reserve = ApiClient.webReservationRequest(server)
        assertEquals("https://worker.example/api/v1/mobile/web-session/reservations", reserve.url.toString())
        assertEquals("POST", reserve.method)
        assertEquals("Bearer fixture-key", reserve.header("Authorization"))
        assertEquals("fixture-cf", reserve.header("CF-Access-Client-Id"))
        assertEquals("fixture-secret", reserve.header("CF-Access-Client-Secret"))
        assertNull(reserve.header("Cookie"))
        val bytes = Buffer(); reserve.body!!.writeTo(bytes); assertEquals("{}", bytes.readUtf8())
        val commit = ApiClient.webSessionRequest(server, ApiClient.SessionPurpose.WEB, token)
        assertEquals(token, commit.header(ApiClient.WEB_RESERVATION_HEADER))
        val native = ApiClient.webSessionRequest(server, ApiClient.SessionPurpose.NATIVE)
        assertNull(native.header(ApiClient.WEB_RESERVATION_HEADER))
        assertThrows(IllegalArgumentException::class.java) { ApiClient.webSessionRequest(server, ApiClient.SessionPurpose.NATIVE, token) }
        assertThrows(IllegalArgumentException::class.java) { ApiClient.webReservationRequest(server.copy(baseUrl = "http://worker.example")) }
        assertThrows(IllegalArgumentException::class.java) { ApiClient.webReservationRequest(server.copy(baseUrl = "https://worker.example/elsewhere")) }
    }

    @Test fun lifetimeUsesMonotonicRequestStartAndToleratesPhoneClockSkew() {
        val ready = (parse(body()) as ApiResult.Success).data as WebSessionReservation.Ready
        assertEquals(130_000L, ready.deadlineMillis)
        assertEquals(token, ready.token)
        assertTrue(ApiClient.parseWebReservation(200, "application/json", body().toString(), 10_000, 130_000) is ApiResult.Failure)
        assertTrue(parse(body().put("expires_at", "2200-01-01T00:00:00Z")) is ApiResult.Success)
    }

    @Test fun malformedReservationNeverPermitsLegacyDowngrade() {
        for ((field, value) in listOf("reservation" to "bad", "reservation" to 12, "expires_at" to "tomorrow",
            "expires_in" to "120", "expires_in" to 0, "expires_in" to 121, "expires_in" to 1.5)) {
            assertTrue("Rejected $field type/value", parse(body().put(field, value)) is ApiResult.Failure)
        }
        for (field in listOf("reservation", "expires_at", "expires_in")) {
            assertTrue(parse(body().apply { remove(field) }) is ApiResult.Failure)
        }
        assertTrue(ApiClient.parseWebReservation(200, "text/html", body().toString()) is ApiResult.Failure)
        assertTrue(ApiClient.parseWebReservation(200, "application/json", " ".repeat(8193)) is ApiResult.Failure)
    }

    @Test fun fallbackRequiresPositiveUnsupportedJsonResponse() {
        for ((code, detail) in listOf(404 to "Not Found", 405 to "Method Not Allowed")) {
            val result = ApiClient.parseWebReservation(code, "application/json; charset=utf-8", JSONObject().put("detail", detail).toString())
            assertSame(WebSessionReservation.Unsupported, (result as ApiResult.Success).data)
        }
        for (code in listOf(301, 302, 401, 403, 409, 410, 500, 503))
            assertTrue(ApiClient.parseWebReservation(code, "application/json", "{\"detail\":\"Not Found\"}") is ApiResult.Failure)
        for (raw in listOf("<html>Not Found</html>", "{}", "{\"detail\":\"Missing device\"}"))
            assertTrue(ApiClient.parseWebReservation(404, "application/json", raw) is ApiResult.Failure)
        assertTrue(ApiClient.parseWebReservation(404, "text/html", "{\"detail\":\"Not Found\"}") is ApiResult.Failure)
    }

    @Test fun newerOriginAttemptAndOldCancellationDoNotInvalidateEachOther() {
        val first = WebSessionBootstrap.begin("https://fence.example")
        val other = WebSessionBootstrap.begin("https://other-fence.example")
        val second = WebSessionBootstrap.begin("https://fence.example")
        assertFalse(WebSessionBootstrap.current(first))
        WebSessionBootstrap.cancel(first)
        assertTrue(WebSessionBootstrap.current(second))
        assertTrue(WebSessionBootstrap.current(other))
        WebSessionBootstrap.cancel(second); WebSessionBootstrap.cancel(other)
    }

    @Test fun metadataRefreshPreservesChatButEveryAuthorizationChangeInvalidatesIt() {
        val server = Server.create("Fixture", "https://metadata-fixture.example", "fixture-key").copy(accountId = "account",
            hubUrl = "https://fixture-hub.example", hubServerId = "worker", cfAccessClientId = "fixture-cf", cfAccessClientSecret = "fixture-secret")
        assertTrue(ChatSessionOwner.sameAuthorization(server, server.copy(name = "Renamed worker", unifiedPushEndpoint = "https://push.example/new")))
        assertFalse(ChatSessionOwner.sameAuthorization(server, null))
        for (changed in listOf(server.copy(id = "other-id"), server.copy(baseUrl = "https://other.example"), server.copy(apiKey = "other-key"),
            server.copy(deviceId = "other-device"), server.copy(accountId = "other-account"), server.copy(hubUrl = "https://other-hub.example"),
            server.copy(hubServerId = "other-worker"), server.copy(cfAccessClientId = "other-cf"), server.copy(cfAccessClientSecret = "other-secret")))
            assertFalse(ChatSessionOwner.sameAuthorization(server, changed))
    }

    @Test fun canceledCookieWriteDrainsBeforeReplacementAndCannotPublishSuccess() = runBlocking {
        val first = WebSessionBootstrap.begin("https://cookie-fence.example")
        val written = mutableListOf<String>()
        val waiting = CompletableDeferred<(Boolean) -> Unit>()
        var firstPublished = false
        val a = launch {
            WebSessionBootstrap.installCookie(first, { true }) { done -> written.add("A"); waiting.complete(done) }
            firstPublished = true
        }
        val completeA = waiting.await()
        val second = WebSessionBootstrap.begin("https://cookie-fence.example")
        a.cancel()
        val b = async {
            WebSessionBootstrap.installCookie(second, { true }) { done -> written.add("B"); done(true) }
        }
        yield()
        assertEquals(listOf("A"), written)
        completeA(true)
        a.join(); assertTrue(b.await())
        assertEquals(listOf("A", "B"), written)
        assertFalse(firstPublished)
        WebSessionBootstrap.cancel(second)
    }
}
