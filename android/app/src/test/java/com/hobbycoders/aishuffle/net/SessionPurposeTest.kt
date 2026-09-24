package com.hobbycoders.aishuffle.net

import com.hobbycoders.aishuffle.model.Server
import okio.Buffer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class SessionPurposeTest {
    @Test fun webAndNativeVoiceMintIndependentSessionPurposes() {
        val server = Server.create("Fixture", "https://worker.example", "test-only")
        for (purpose in ApiClient.SessionPurpose.values()) {
            val request = ApiClient.webSessionRequest(server, purpose)
            val buffer = Buffer(); request.body!!.writeTo(buffer)
            assertEquals(purpose.value, JSONObject(buffer.readUtf8()).getString("purpose"))
            assertEquals("Bearer test-only", request.header("Authorization"))
        }
        val default = ApiClient.webSessionRequest(server)
        val buffer = Buffer(); default.body!!.writeTo(buffer)
        assertEquals("web", JSONObject(buffer.readUtf8()).getString("purpose"))
    }

    @Test fun sessionHttpClientsNeverFollowRedirectsWithBearerOrCookie() {
        fun client(type: Class<*>): okhttp3.OkHttpClient {
            val field = type.getDeclaredField("client\$delegate").apply { isAccessible = true }
            return (field.get(null) as Lazy<*>).value as okhttp3.OkHttpClient
        }
        for (http in listOf(client(ApiClient::class.java), client(VoiceApi::class.java))) {
            assertFalse(http.followRedirects)
            assertFalse(http.followSslRedirects)
            val redirected = java.util.concurrent.atomic.AtomicInteger()
            val source = okhttp3.mockwebserver.MockWebServer()
            val destination = okhttp3.mockwebserver.MockWebServer()
            source.start(java.net.InetAddress.getByName("127.0.0.1"), 0)
            destination.start(java.net.InetAddress.getByName("127.0.0.1"), 0)
            val dispatcher = object : okhttp3.mockwebserver.Dispatcher() {
                override fun dispatch(request: okhttp3.mockwebserver.RecordedRequest): okhttp3.mockwebserver.MockResponse {
                    val url = requireNotNull(request.requestUrl)
                    if (url.encodedPath == "/start") {
                        val target = if (url.queryParameter("kind") == "cross") destination else source
                        return okhttp3.mockwebserver.MockResponse()
                            .setResponseCode(requireNotNull(url.queryParameter("code")).toInt())
                            .setHeader("Location", target.url("/leak")).setBody("")
                    }
                    redirected.incrementAndGet()
                    return okhttp3.mockwebserver.MockResponse().setResponseCode(200).setBody("")
                }
            }
            source.dispatcher = dispatcher
            destination.dispatcher = dispatcher
            try {
                for (code in listOf(301, 302, 303, 307, 308)) for (kind in listOf("same", "cross")) {
                    val request = okhttp3.Request.Builder().url(source.url("/start?code=$code&kind=$kind"))
                        .header("Authorization", "Bearer shc_public-test-fixture")
                        .header("Cookie", "session=shc_public-test-fixture").build()
                    http.newCall(request).execute().use { assertEquals(code, it.code) }
                }
                assertEquals(0, redirected.get())
                assertEquals(0, destination.requestCount)
            } finally {
                source.shutdown(); destination.shutdown()
            }
        }
    }
}
