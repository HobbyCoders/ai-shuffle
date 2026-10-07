package com.hobbycoders.aishuffle.ui

import com.hobbycoders.aishuffle.model.Server
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import okio.BufferedSource
import okio.buffer
import okio.source
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.security.cert.CertificateException
import java.util.concurrent.CancellationException
import javax.net.ssl.SSLException
import javax.net.ssl.SSLHandshakeException

@RunWith(RobolectricTestRunner::class)
class WorkerAssetLoaderTest {
    private val server = Server.create("Assets", "https://worker.example", "unused-device-key")
    private val assetPaths = listOf(
        "/api/generated-images/by-path?path=/work/image.png",
        "/api/generated-videos/by-path?path=/work/movie.mp4",
        "/api/generated-audio/by-path?path=/work/sound.wav",
        "/api/files/by-path?path=%2Fwork%2Fchip.png",
        "/api/v1/notes/n/attachments/a/download",
        "/api/v1/workspace/files/download?path=report.pdf",
    )

    @Test fun rootDeepLinksReachTheNavigationClassifierWithoutBecomingDownloadable() {
        val target = server.baseUrl + "/?session=parent"
        val admitted = WorkerAssetLoader.workerUrl(server.baseUrl, target)
        assertEquals(target, admitted)
        assertFalse(ChatDownloadPolicy.url(server.baseUrl, target))
        assertEquals(ChatDownloadPolicy.Navigation.Session(server.baseUrl + "/mobile/?session=parent"),
            ChatDownloadPolicy.navigation(server.baseUrl, admitted!!, true, true))
    }

    @Test fun everyAssetConsumerResolvesOnlyToPairedWorker() {
        for (path in assetPaths) {
            assertEquals(server.baseUrl + path, WorkerAssetLoader.workerUrl(server.baseUrl, server.baseUrl + path))
        }
        // The phone UI is same-origin with the worker now, so the old bundled-asset origin is just another site.
        for (url in listOf("https://outside.example/api/file", "http://worker.example/a", "https://user@worker.example/a",
            "https://worker.example:8443/a", "https://appassets.androidplatform.net/api/generated-images/by-path?path=/work/image.png")) {
            assertNull(url, WorkerAssetLoader.workerUrl(server.baseUrl, url))
        }
    }

    @Test fun webResponseStreamsRangeAndMetadataAndClosesWithWebView() {
        var closed = false
        val source = Buffer().writeUtf8("abc")
        val body = object : ResponseBody() {
            override fun contentType() = "video/mp4".toMediaType()
            override fun contentLength() = 3L
            override fun source(): BufferedSource = source
            override fun close() { closed = true; super.close() }
        }
        val loader = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            assertEquals("Bearer session-fixture", chain.request().header("Authorization"))
            assertEquals("bytes=2-4", chain.request().header("Range"))
            assertEquals("identity", chain.request().header("Accept-Encoding"))
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_2).code(206).message("")
                .header("Content-Type", "video/mp4").header("Content-Length", "3")
                .header("Content-Range", "bytes 2-4/12").header("Accept-Ranges", "bytes")
                .header("Content-Disposition", "inline; filename=clip.mp4").header("ETag", "fixture")
                .header("Set-Cookie", "session=must-not-install").body(body).build()
        }.build()))
        val result = loader.load(server, server.baseUrl + assetPaths[1], "GET", mapOf("Range" to "bytes=2-4"),
            "session-fixture", null, "fixture-agent", { true }, { fail(it) })
        assertFalse(closed)
        assertEquals(3L, source.size) // Loader did not buffer or consume a single payload byte.
        assertEquals(206, result.statusCode)
        assertEquals("video/mp4", result.mimeType)
        assertEquals("Worker response", result.reasonPhrase)
        // Chromium synthesizes Content-Length from available()/Range, so do not duplicate it.
        assertNull(result.responseHeaders["Content-Length"])
        assertEquals("bytes 2-4/12", result.responseHeaders["Content-Range"])
        assertEquals("bytes", result.responseHeaders["Accept-Ranges"])
        assertEquals(server.baseUrl, result.responseHeaders["Access-Control-Allow-Origin"])
        assertEquals("no-store", result.responseHeaders["Cache-Control"])
        assertNull(result.responseHeaders["Set-Cookie"])
        result.data.use {
            // Reproduce WebView InputStreamReader::Seek: full-size bounds, then skip the
            // original offset even though upstream already returned a 206 window.
            assertEquals(12, it.available())
            assertEquals(2L, it.skip(2))
            assertEquals("abc", it.bufferedReader().readText())
        }
        assertTrue(closed)
    }

    @Test fun suffixWindowCreditsMultipleInitialSkipsWithoutDiscardingPayload() {
        val loader = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(206).message("Partial")
                .header("Content-Range", "bytes 9-11/12").body("xyz".toResponseBody("video/mp4".toMediaType())).build()
        }.build()))
        val result = loader.load(server, server.baseUrl + assetPaths[1], "GET", mapOf("Range" to "bytes=-3"),
            "token", null, "fixture", { true }, { fail(it) })
        result.data.use {
            assertEquals(12, it.available())
            assertEquals(4L, it.skip(4))
            assertEquals(5L, it.skip(5))
            assertEquals("xyz", it.bufferedReader().readText())
        }
    }

    @Test fun fullResourceSizeComesFromMetadataNotCurrentlyBufferedNetworkBytes() {
        val source = java.io.ByteArrayInputStream("abcdefghijkl".toByteArray()).source().buffer()
        val body = object : ResponseBody() {
            override fun contentType() = "image/png".toMediaType()
            override fun contentLength() = 12L
            override fun source(): BufferedSource = source
        }
        assertEquals(0, body.byteStream().available())
        val loader = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(200).message("OK").body(body).build()
        }.build()))
        val result = loader.load(server, server.baseUrl + assetPaths[0], "GET", emptyMap(),
            "token", null, "fixture", { true }, { fail(it) })
        result.data.use {
            assertEquals(12, it.available())
            assertEquals("abcdefghijkl", it.bufferedReader().readText())
        }
    }

    @Test fun headRetainsResourceLengthEvenWithAnEmptyBody() {
        val loader = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            assertEquals("HEAD", chain.request().method)
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(200).message("OK")
                .header("Content-Length", "12").body("".toResponseBody("image/png".toMediaType())).build()
        }.build()))
        val result = loader.load(server, server.baseUrl + assetPaths[0], "HEAD", emptyMap(),
            "token", null, "fixture", { true }, { fail(it) })
        result.data.use {
            assertEquals(12, it.available())
            assertEquals(-1, it.read())
        }
    }

    @Test fun malformedPartialResponseIsRejected() {
        var reported = false
        val loader = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(206).message("Partial")
                .header("Content-Range", "bytes 9-4/12").body("abc".toResponseBody("video/mp4".toMediaType())).build()
        }.build()))
        val result = loader.load(server, server.baseUrl + assetPaths[1], "GET", mapOf("Range" to "bytes=2-4"),
            "token", null, "fixture", { true }, { assertEquals(WorkerAssetLoader.INVALID_MESSAGE, it); reported = true })
        assertEquals(502, result.statusCode)
        assertTrue(reported)
        result.data.close()
    }

    private fun loaderFailing(error: () -> Nothing) = WorkerAssetLoader(WorkerDownloadClient(
        OkHttpClient.Builder().addInterceptor { error() }.build()))

    // #1946: a worker restart is a timeout or a dropped connection, not a Cloudflare Access problem.
    @Test fun timeoutOrDroppedConnectionSaysTheWorkerMayBeRestarting() {
        for (error in listOf(SocketTimeoutException("timeout"), ConnectException("refused"), IOException("unexpected end of stream"))) {
            val statuses = mutableListOf<String>()
            var loaded = false
            val result = loaderFailing { throw error }.load(server, server.baseUrl + assetPaths[0], "GET", emptyMap(),
                "token", null, "fixture", { true }, { statuses.add(it) }, { loaded = true })
            assertEquals(502, result.statusCode)
            assertEquals(listOf(WorkerAssetLoader.RESTARTING_MESSAGE), statuses)
            assertFalse(loaded)
        }
    }

    @Test fun messageNamesTheCauseAndCloudflareAccessOnlyForAnAccessRedirect() {
        fun message(error: Throwable, cfAccess: Boolean = false) = WorkerAssetLoader.failureMessage(error, cfAccess)
        assertEquals(WorkerAssetLoader.RESTARTING_MESSAGE, message(SocketTimeoutException("read timed out")))
        // A TLS reset mid-handshake is a dropped connection; a rejected certificate is not.
        assertEquals(WorkerAssetLoader.RESTARTING_MESSAGE, message(SSLException("Connection reset by peer")))
        assertEquals(WorkerAssetLoader.HTTPS_MESSAGE,
            message(SSLHandshakeException("bad cert").apply { initCause(CertificateException("untrusted")) }))
        assertEquals(WorkerAssetLoader.ACCESS_MESSAGE, message(ExternalRedirectException(), cfAccess = true))
        assertEquals(WorkerAssetLoader.INVALID_MESSAGE, message(ExternalRedirectException(), cfAccess = false))
        assertEquals(WorkerAssetLoader.INVALID_MESSAGE, message(IllegalArgumentException("Invalid partial file bounds")))
        assertEquals(WorkerAssetLoader.FAILED_MESSAGE, message(RuntimeException("unexpected")))
        assertNull(message(CancellationException("Download cancelled")))
        for (text in listOf(WorkerAssetLoader.RESTARTING_MESSAGE, WorkerAssetLoader.INVALID_MESSAGE,
            WorkerAssetLoader.HTTPS_MESSAGE, WorkerAssetLoader.FAILED_MESSAGE)) {
            assertFalse(text, text.contains("Cloudflare"))
        }
        for (cfAccess in listOf(false, true)) {
            assertEquals(WorkerAssetLoader.RESTARTING_MESSAGE, message(SocketTimeoutException("timeout"), cfAccess))
        }
    }

    @Test fun accessSignInRedirectNamesCloudflareAccessOnlyOnAnAccessWorker() {
        val loader = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(302).message("Found")
                .header("Location", "https://team.cloudflareaccess.com/cdn-cgi/access/login/worker.example")
                .body("".toResponseBody()).build()
        }.build()))
        val access = server.copy(cfAccessClientId = "client-id-fixture", cfAccessClientSecret = "client-secret-fixture")
        for ((paired, expected) in listOf(access to WorkerAssetLoader.ACCESS_MESSAGE, server to WorkerAssetLoader.INVALID_MESSAGE)) {
            val statuses = mutableListOf<String>()
            loader.load(paired, paired.baseUrl + assetPaths[0], "GET", emptyMap(), "token", null, "fixture", { true },
                { statuses.add(it) }).data.close()
            assertEquals(listOf(expected), statuses)
        }
    }

    @Test fun aSuccessfulLoadReportsLoadedSoAnEarlierNoticeClears() {
        var loaded = 0
        val loader = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(200).message("OK")
                .body("png".toResponseBody("image/png".toMediaType())).build()
        }.build()))
        loader.load(server, server.baseUrl + assetPaths[0], "GET", emptyMap(), "token", null, "fixture", { true },
            { fail(it) }, { loaded++ }).data.close()
        assertEquals(1, loaded)
    }

    @Test fun notModifiedAndCancelledLoadsStaySilent() {
        var loaded = false
        val notModified = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(304).message("Not Modified")
                .body("".toResponseBody()).build()
        }.build()))
        val stale = notModified.load(server, server.baseUrl + assetPaths[0], "GET", mapOf("If-None-Match" to "etag"),
            "token", null, "fixture", { true }, { fail(it) }, { loaded = true })
        assertEquals(502, stale.statusCode)
        stale.data.close()
        assertFalse(loaded)
        // Navigation, Refresh chat and close cancel in-flight loads (cancel()): OkHttp reports "Canceled".
        val cancelled = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            chain.call().cancel(); throw IOException("Canceled")
        }.build()))
        val result = cancelled.load(server, server.baseUrl + assetPaths[0], "GET", emptyMap(),
            "token", null, "fixture", { true }, { fail(it) }, { loaded = true })
        assertEquals(502, result.statusCode)
        assertFalse(loaded)
    }

    @Test fun rejectsMissingAuthMainOriginSpoofsAndReportsOldWorkerAuthFailures() {
        var requests = 0
        val loader = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            requests++
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(401).message("Unauthorized")
                .body("Update worker".toResponseBody("text/plain".toMediaType())).build()
        }.build()))
        fun load(url: String = server.baseUrl + assetPaths[0], token: String? = "token", method: String = "GET", status: (String) -> Unit = {}) =
            loader.load(server, url, method, emptyMap(), token, null, "fixture", { true }, status)
        assertEquals(401, load(token = null).statusCode)
        assertEquals(403, load(url = "https://elsewhere.example/api/a").statusCode)
        assertEquals(405, load(method = "POST").statusCode)
        assertEquals(0, requests)
        val statuses = mutableListOf<String>()
        val rejected = load(status = { statuses.add(it) })
        assertEquals(401, rejected.statusCode)
        rejected.data.close()
        assertEquals(listOf(WorkerAssetLoader.AUTH_MESSAGE), statuses)
    }

    @Test fun signOutStopsAlreadyOpenedMediaBeforeMoreBytesAreRead() {
        var allowed = true
        val loader = WorkerAssetLoader(WorkerDownloadClient(OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(200).message("OK")
                .body("private".toResponseBody("audio/wav".toMediaType())).build()
        }.build()))
        val result = loader.load(server, server.baseUrl + assetPaths[2], "GET", emptyMap(), "token", null, "fixture", { allowed }, { fail(it) })
        allowed = false
        assertThrows(IOException::class.java) { result.data.available() }
        assertThrows(IOException::class.java) { result.data.skip(1) }
        assertThrows(IOException::class.java) { result.data.read() }
    }
}
