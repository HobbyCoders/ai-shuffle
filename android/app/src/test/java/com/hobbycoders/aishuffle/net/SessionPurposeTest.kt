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
            val status = java.util.concurrent.atomic.AtomicInteger(307)
            val source = com.sun.net.httpserver.HttpServer.create(java.net.InetSocketAddress("127.0.0.1", 0), 0)
            val destination = com.sun.net.httpserver.HttpServer.create(java.net.InetSocketAddress("127.0.0.1", 0), 0)
            fun leaf(exchange: com.sun.net.httpserver.HttpExchange) {
                redirected.incrementAndGet()
                exchange.sendResponseHeaders(200, -1)
                exchange.close()
            }
            source.createContext("/leak", ::leaf)
            destination.createContext("/leak", ::leaf)
            val sourceOrigin = "http://127.0.0.1:${source.address.port}"
            val foreignOrigin = "http://127.0.0.1:${destination.address.port}"
            source.createContext("/start") { exchange ->
                val target = if (exchange.requestURI.query == "cross") foreignOrigin else sourceOrigin
                exchange.responseHeaders.add("Location", "$target/leak")
                exchange.sendResponseHeaders(status.get(), -1)
                exchange.close()
            }
            source.start(); destination.start()
            try {
                for (code in listOf(301, 302, 303, 307, 308)) for (kind in listOf("same", "cross")) {
                    status.set(code)
                    val request = okhttp3.Request.Builder().url("$sourceOrigin/start?$kind")
                        .header("Authorization", "Bearer shc_public-test-fixture")
                        .header("Cookie", "session=shc_public-test-fixture").build()
                    http.newCall(request).execute().use { assertEquals(code, it.code) }
                }
                assertEquals(0, redirected.get())
            } finally {
                source.stop(0); destination.stop(0)
            }
        }
    }
}
