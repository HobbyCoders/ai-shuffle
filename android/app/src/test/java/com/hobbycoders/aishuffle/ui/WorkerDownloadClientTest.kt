package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.model.Server
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.util.concurrent.CancellationException

class WorkerDownloadClientTest {
    private val server = Server.create("Fixture", "https://worker.example", "test-key")
        .copy(cfAccessClientId = "test-id", cfAccessClientSecret = "test-secret")
    private fun response(request: Request, code: Int = 200, location: String? = null) = Response.Builder()
        .request(request).protocol(Protocol.HTTP_1_1).code(code).message("Fixture")
        .body("small benign file".toResponseBody("text/plain".toMediaType()))
        .apply { if (location != null) header("Location", location) }.build()

    @Test fun sameOriginRedirectStreamsFileAndPreservesRequiredAuthentication() {
        val seen = mutableListOf<Request>()
        val client = WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request(); seen.add(request)
            if (seen.size == 1) response(request, 302, "/download/final.txt") else response(request)
        }.build())
        val sink = ByteArrayOutputStream()
        client.save(server, "${server.baseUrl}/file", "session=test-cookie", "test-agent", sink, { true }, {})
        assertEquals("small benign file", sink.toString("UTF-8"))
        assertEquals(2, seen.size)
        seen.forEach { assertEquals("session=test-cookie", it.header("Cookie")); assertEquals("test-id", it.header("CF-Access-Client-Id")); assertEquals("test-secret", it.header("CF-Access-Client-Secret")) }
    }
    @Test fun crossOriginAndHttpRedirectsNeverReceiveCookiesOrAccessSecrets() {
        for (target in listOf("https://other.example/file", "http://worker.example/file", "https://user@worker.example/file", "https://worker.example:8443/file")) {
            var requests = 0
            val client = WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain -> requests++; response(chain.request(), 302, target) }.build())
            assertThrows(IllegalArgumentException::class.java) { client.save(server, "${server.baseUrl}/file", "session=test-cookie", "test-agent", ByteArrayOutputStream(), { true }, {}) }
            assertEquals(1, requests)
        }
    }
    @Test fun invalidInitialOriginAndRevokedAccountNeverMakeRequest() {
        var requests = 0
        val client = WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain -> requests++; response(chain.request()) }.build())
        assertThrows(IllegalArgumentException::class.java) { client.save(server, "https://other.example/file", "session=test", "test", ByteArrayOutputStream(), { true }, {}) }
        assertThrows(CancellationException::class.java) { client.save(server, "${server.baseUrl}/file", "session=test", "test", ByteArrayOutputStream(), { false }, {}) }
        assertEquals(0, requests)
    }
    @Test fun authorizationFailureIsNotSavedAsDownloadedFile() {
        val client = WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain -> response(chain.request(), 401) }.build())
        val sink = ByteArrayOutputStream()
        val error = assertThrows(IllegalArgumentException::class.java) { client.save(server, "${server.baseUrl}/file", "session=test", "test", sink, { true }, {}) }
        assertTrue(error.message!!.contains("Refresh chat"))
        assertEquals(0, sink.size())
    }
}
